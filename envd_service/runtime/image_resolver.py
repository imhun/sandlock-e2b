"""Resolve a base image to an extracted rootfs for Sandlock chroot.

Uses the OCI Distribution API directly (no Docker daemon): resolve the
platform manifest, download the layer blobs, and assemble the filesystem
with OCI whiteout semantics. The extracted rootfs is cached under
``E2B_IMAGE_CACHE_DIR`` keyed by the platform manifest digest, so a refreshed
tag self-invalidates the cache and a warm image resolves instantly.

The cache directory is a *shared* resource in production (every worker and the
control plane point ``E2B_IMAGE_CACHE_DIR`` at one directory on the sandbox
volume), so two things hold beyond the in-process lock:

* extraction runs under ``flock(2)`` on ``<cache>/<image-slug>.lock`` and is
  published with ``os.replace`` of a fully extracted staging tree, so a second
  process either reuses the finished entry or finds it already complete --
  never a half-written rootfs (Z-F7);
* the cache is bounded: completed entries beyond
  ``E2B_IMAGE_CACHE_MAX_BYTES`` are evicted oldest-first by
  :func:`prune_image_cache`, which is also what keeps an unlimited-project
  shared directory from quietly filling the volume.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import tarfile
from contextlib import contextmanager, suppress
from typing import Callable, Iterable, Iterator

from envd_service.runtime.oci_registry import (
    RegistryClient,
    RegistryError,
    extract_layer,
    fetch_platform_manifest,
    parse_image_ref,
)

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


class ImageResolutionError(RuntimeError):
    pass


def _image_cache_name(image: str) -> str:
    return _SAFE.sub("_", image)[:128] or "image"


def _digest_suffix(digest: str) -> str:
    return _SAFE.sub("_", digest.split(":", 1)[-1])[:40]


def _cache_lock(name: str) -> threading.Lock:
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            _CACHE_LOCKS[name] = lock
        return lock


def _lock_path(cache_dir: Path, image: str) -> Path:
    """``<cache>/<image-slug>.lock``: the *cross-process* lock for one image.

    It sits next to the cache entries (same volume), and it is deliberately
    never unlinked: deleting the file would let the next two processes lock two
    different inodes of the same name and extract the entry concurrently, which
    is exactly the shape this lock exists to prevent.
    """
    return Path(cache_dir) / f"{_image_cache_name(image)}.lock"


@contextmanager
def _locked_cache_entry(cache_dir: Path, image: str) -> Iterator[None]:
    """Serialize "check ``.complete`` → extract → publish" across processes.

    ``flock(2)`` covers other processes (another worker, the control plane);
    the in-process lock stays because a second ``open`` in the *same* process
    would block on its own file description, and a threaded caller should
    queue on the cheap lock instead.
    """
    lock_path = _lock_path(cache_dir, image)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(_image_cache_name(image)):
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _entry_name(image: str, digest: str) -> str:
    """Cache directory name for one ``(image, platform digest)`` pair."""
    return f"{_image_cache_name(image)}-{_digest_suffix(digest)}"


def _stage_entry(cache_dir: Path, entry_name: str) -> Path:
    """A private staging tree for ``entry_name``, on the cache's filesystem.

    Same directory on purpose: ``os.replace`` is only atomic within one
    filesystem, and the shared volume is the one the entry has to land on.

    ``mkdtemp`` creates it ``0700``, but the mode is *published* with the
    entry: the sandbox uid (a per-sandbox host uid under route B, uid 1000
    under the test harness) has to traverse ``<entry>/rootfs`` to chroot into
    it. ``os.replace`` keeps the directory's own mode, so widen it here to what
    the pre-Z-F7 ``mkdir`` would have produced (0755 under the usual umask);
    the staging tree is only reachable through the cache directory.
    """
    staging = Path(tempfile.mkdtemp(prefix=f".{entry_name}.tmp-", dir=cache_dir))
    os.chmod(staging, 0o755)
    return staging


def _entry_complete(entry: Path) -> bool:
    """Whether ``entry`` is published (``rootfs/.complete`` present)."""
    return (entry / "rootfs" / ".complete").is_file()


def _remove_path(path: Path) -> None:
    """Remove a file, symlink or tree, ignoring anything already gone."""
    if path.is_symlink() or path.is_file():
        with suppress(OSError):
            path.unlink()
        return
    shutil.rmtree(path, ignore_errors=True)


def _publish_staged_entry(staging: Path, entry: Path) -> None:
    """Publish a fully extracted entry in one ``os.replace``.

    ``.complete`` is written inside the staging tree *before* this call, so the
    entry becomes visible already marked complete: a reader can never observe a
    partially written rootfs, even on storage where the lock file itself is not
    honoured (NFS mounted ``nolock``, for one).

    A completed entry always wins: if another process got there first (possible
    exactly when the lock is not honoured), this process throws away its own
    staging tree instead of replacing or deleting a finished entry, so a reader
    is never left with a hole where a completed rootfs used to be. Only a name
    held by *incomplete* leftover garbage (a crashed pre-Z-F7 worker is the only
    producer of that shape) is removed.
    """
    if _entry_complete(entry):
        _remove_path(staging)
        return
    try:
        os.replace(staging, entry)
        return
    except OSError:
        # ``os.replace`` refuses a non-empty directory target, which is what a
        # concurrent publisher -- or leftover garbage -- looks like here.
        pass
    if _entry_complete(entry):
        _remove_path(staging)
        return
    _remove_path(entry)
    try:
        os.replace(staging, entry)
    except OSError:
        _remove_path(staging)
        if _entry_complete(entry):
            return
        raise


def _extract_layers(image: str, rootfs: Path, blobs: Iterable[bytes]) -> None:
    """Unpack ``blobs`` into ``rootfs`` and mark it complete."""
    rootfs.mkdir(parents=True, exist_ok=True)
    # Deterministic, traversable mode for the sandbox uid (see _stage_entry).
    os.chmod(rootfs, 0o755)
    for blob in blobs:
        extract_layer(blob, rootfs)
    if not (rootfs / "bin").is_dir() and not (rootfs / "usr" / "bin").is_dir():
        raise ImageResolutionError(f"image {image} produced an empty rootfs")
    rootfs.joinpath(".complete").write_text("ok", encoding="utf-8")


def _materialize_entry(
    cache_dir: Path,
    image: str,
    entry_name: str,
    blobs: Callable[[], Iterable[bytes]],
) -> Path:
    """Return ``image``'s completed rootfs, extracting it at most once.

    ``blobs`` is called at most once, from inside the lock, and only by the
    process that ends up doing the work.
    """
    cache_dir = Path(cache_dir)
    entry = cache_dir / entry_name
    rootfs = entry / "rootfs"
    marker = rootfs / ".complete"
    if marker.is_file():
        return rootfs
    with _locked_cache_entry(cache_dir, image):
        if marker.is_file():
            return rootfs
        staging = _stage_entry(cache_dir, entry_name)
        try:
            _extract_layers(image, staging / "rootfs", blobs())
            _publish_staged_entry(staging, entry)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    _maybe_prune_cache(cache_dir)
    return rootfs


def _client_for(
    image: str,
    *,
    registry_username: str | None,
    registry_password: str | None,
    scheme: str | None = None,
    credential_host: str | None = None,
) -> tuple[ImageRef, RegistryClient]:
    ref = parse_image_ref(image)
    client = RegistryClient(
        ref,
        username=registry_username,
        password=registry_password,
        scheme=scheme,
        credential_host=credential_host,
    )
    return ref, client


# Manifest lookups are per create, and a node serving one template can ask
# for the same tag hundreds of times a minute. Docker Hub (and most registries)
# answer that with 429 TOOMANYREQUESTS for anonymous pulls, which then looks
# like a sandbox failure. Cache the resolved digest briefly, per process.
_DIGEST_CACHE: dict[str, tuple[float, str]] = {}
_DIGEST_CACHE_LOCK = threading.Lock()
_DEFAULT_MANIFEST_TTL_S = 60.0


def _manifest_ttl_s() -> float:
    raw = os.environ.get("E2B_IMAGE_MANIFEST_TTL_S", "")
    if not raw:
        return _DEFAULT_MANIFEST_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "ignoring invalid E2B_IMAGE_MANIFEST_TTL_S=%r (expected seconds)", raw
        )
        return _DEFAULT_MANIFEST_TTL_S


def _platform_digest(
    image: str,
    *,
    registry_username: str | None,
    registry_password: str | None,
    scheme: str | None = None,
    credential_host: str | None = None,
) -> str:
    ttl = _manifest_ttl_s()
    # Credentials and scheme are part of the key: re-authenticating or
    # switching registry endpoint must not be answered from the old lookup.
    key = f"{image}|{scheme or ''}|{registry_username or ''}|{credential_host or ''}"
    now = time.monotonic()
    if ttl > 0:
        with _DIGEST_CACHE_LOCK:
            hit = _DIGEST_CACHE.get(key)
            if hit and now - hit[0] <= ttl:
                return hit[1]
    _ref, client = _client_for(
        image,
        registry_username=registry_username,
        registry_password=registry_password,
        scheme=scheme,
        credential_host=credential_host,
    )
    _manifest, digest = fetch_platform_manifest(client)
    if ttl > 0:
        with _DIGEST_CACHE_LOCK:
            _DIGEST_CACHE[key] = (now, digest)
    return digest


def _cache_rootfs(cache_dir: Path, image: str, digest: str) -> Path:
    return Path(cache_dir) / _entry_name(image, digest) / "rootfs"


# --- bounding the shared cache ----------------------------------------------
# The cache lives on the sandbox volume, in the volume's *unlimited* project
# (it is not a sandbox tree, see ``gateway_common.paths``), so nothing else
# stops it from filling the disk. Bound it here, at the only place it grows:
# after publishing a fresh entry.
_CACHE_MAX_BYTES_ENV = "E2B_IMAGE_CACHE_MAX_BYTES"
_DEFAULT_CACHE_MAX_BYTES = 8 * 1024**3
_CACHE_MIN_AGE_ENV = "E2B_IMAGE_CACHE_EVICT_MIN_AGE_S"
_DEFAULT_CACHE_MIN_AGE_S = 300.0
#: The cap only matters on the scale of minutes, and the walk is O(cache): do
#: it at most this often per process.
_PRUNE_INTERVAL_S = 60.0
_last_prune_monotonic = 0.0


def _cache_max_bytes() -> int:
    """``E2B_IMAGE_CACHE_MAX_BYTES`` (bytes); ``0`` disables eviction."""
    raw = os.environ.get(_CACHE_MAX_BYTES_ENV, "").strip()
    if not raw:
        return _DEFAULT_CACHE_MAX_BYTES
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r (expected bytes, 0 = unbounded)",
            _CACHE_MAX_BYTES_ENV,
            raw,
        )
        return _DEFAULT_CACHE_MAX_BYTES


def _cache_evict_min_age_s() -> float:
    """``E2B_IMAGE_CACHE_EVICT_MIN_AGE_S``: freshness floor for eviction."""
    raw = os.environ.get(_CACHE_MIN_AGE_ENV, "").strip()
    if not raw:
        return _DEFAULT_CACHE_MIN_AGE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r (expected seconds)",
            _CACHE_MIN_AGE_ENV,
            raw,
        )
        return _DEFAULT_CACHE_MIN_AGE_S


def _tree_bytes(path: Path) -> int:
    """Allocated bytes at and under ``path`` (``du`` semantics)."""
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda _exc: None):
        for name in dirs + files:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            total += info.st_blocks * 512 if info.st_blocks else info.st_size
    try:
        info = os.lstat(path)
    except OSError:
        return total
    return total + (info.st_blocks * 512 if info.st_blocks else info.st_size)


def _oci_tar_bytes(cache: Path) -> int:
    """Bytes held by the locally built OCI layout tars (``_oci/``)."""
    total = 0
    for tar_path in (cache / LOCAL_OCI_DIRNAME).glob("*.oci.tar"):
        with suppress(OSError):
            total += tar_path.stat().st_size
    return total


def prune_image_cache(
    cache_dir: str | Path,
    *,
    max_bytes: int | None = None,
    min_age_s: float | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Evict the oldest *completed* entries until the cache fits the cap.

    Only entries carrying ``rootfs/.complete`` are candidates: entries are
    published by ``os.replace``, so one that is complete is never being
    written, and an in-flight extraction only ever exists as a dot-prefixed
    staging tree (never a candidate). ``min_age_s`` keeps the newest results
    (and anything a create may be about to use) out of reach.

    The ``_oci`` layout tars are counted -- they live on the same volume -- but
    never evicted: with no registry configured a tar is the only copy of a
    locally built image, so dropping it would break that template instead of
    merely forcing a re-pull.
    """
    cache = Path(cache_dir)
    cap = _cache_max_bytes() if max_bytes is None else int(max_bytes)
    min_age = _cache_evict_min_age_s() if min_age_s is None else float(min_age_s)
    reference = time.time() if now is None else float(now)
    candidates: list[tuple[float, Path, int]] = []
    if cache.is_dir():
        for entry in sorted(cache.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name.startswith("."):  # staging tree, not an entry
                continue
            marker = entry / "rootfs" / ".complete"
            if not marker.is_file():
                continue
            candidates.append((marker.stat().st_mtime, entry, _tree_bytes(entry)))
    oci_bytes = _oci_tar_bytes(cache)
    kept_bytes = sum(size for _mtime, _entry, size in candidates)
    total = kept_bytes + oci_bytes
    evicted = 0
    freed = 0
    skipped_fresh = 0
    if cap > 0:
        for mtime, entry, size in sorted(candidates, key=lambda item: item[0]):
            if total <= cap:
                break
            if min_age > 0 and reference - mtime < min_age:
                skipped_fresh += 1
                continue
            shutil.rmtree(entry, ignore_errors=True)
            total -= size
            kept_bytes -= size
            freed += size
            evicted += 1
    return {
        "entries": len(candidates),
        "evicted": evicted,
        "freed_bytes": freed,
        "kept_bytes": kept_bytes,
        "oci_bytes": oci_bytes,
        "skipped_fresh": skipped_fresh,
        "max_bytes": cap,
    }


def _maybe_prune_cache(cache_dir: Path) -> None:
    """Enforce the cache cap after a cold resolve (throttled per process)."""
    global _last_prune_monotonic
    cap = _cache_max_bytes()
    if cap <= 0:
        return
    now = time.monotonic()
    if now - _last_prune_monotonic < _PRUNE_INTERVAL_S:
        return
    _last_prune_monotonic = now
    stats = prune_image_cache(cache_dir, max_bytes=cap)
    if stats["evicted"]:
        logger.warning(
            "image cache %s over %d bytes: evicted %d completed entries, freed %d bytes",
            cache_dir,
            cap,
            stats["evicted"],
            stats["freed_bytes"],
        )
    elif stats["kept_bytes"] + stats["oci_bytes"] > cap:
        logger.warning(
            "image cache %s still over %d bytes after eviction (kept=%d oci=%d; "
            "the freshness floor is %ss and the OCI layout tars are never evicted)",
            cache_dir,
            cap,
            stats["kept_bytes"],
            stats["oci_bytes"],
            _cache_evict_min_age_s(),
        )


def _shared_cache_dir() -> Path | None:
    """The node's configured image cache (``E2B_IMAGE_CACHE_DIR`` or the
    config default), when it differs from the cache a caller passed in.

    Image-rootfs resolution is normally called with the sandbox's own cache
    root, but locally built images (e.g. the MCP-capable ``python-mcp:3.14``
    staged via the local-OCI sidecar) live in the node cache. Falling back to
    that shared link lets every caller resolve a locally provisioned image
    without a registry round-trip (which would 403 for a tag the mirror does
    not carry).
    """
    raw = os.environ.get("E2B_IMAGE_CACHE_DIR", "tmp/sandboxes/_images")
    if not raw:
        return None
    return Path(raw)


def _shared_cache_rootfs(image: str, cache: Path) -> Path | None:
    """Completed rootfs for ``image`` from the node's shared image cache."""
    shared = _shared_cache_dir()
    if shared is None:
        return None
    shared = shared.resolve()
    if shared == Path(cache).resolve():
        return None
    link = local_oci_paths(shared, image)[1]
    return _rootfs_from_local_link(link)


# --- locally built images (no registry configured) --------------------------
# ``Template.build`` without ``E2B_IMAGE_REGISTRY`` has no registry to push
# to, so the control plane exports the build as an OCI layout tar into the
# image cache. The node that owns that cache (the single-node shape: control
# plane and worker share ``E2B_IMAGE_CACHE_DIR``) resolves the image from the
# tar instead of a registry round-trip; every other node keeps failing with
# the usual "cannot resolve image" error, which is correct — the image was
# never distributed.
LOCAL_OCI_DIRNAME = "_oci"
_OCI_LINK_SUFFIX = ".link"


def local_oci_paths(cache_dir: str | Path, image: str) -> tuple[Path, Path]:
    """``(oci layout tar, sidecar link file)`` for one locally built image."""
    root = Path(cache_dir) / LOCAL_OCI_DIRNAME
    slug = _image_cache_name(image)
    return root / f"{slug}.oci.tar", root / f"{slug}{_OCI_LINK_SUFFIX}"


def _link_digest(link: Path) -> str | None:
    """The manifest digest recorded next to the rootfs path in the sidecar."""
    with suppress(OSError):
        digest, _, _rest = link.read_text(encoding="utf-8").partition("\n")
        return digest or None
    return None


def _rootfs_from_local_link(link: Path) -> Path | None:
    """The completed rootfs recorded by an earlier local-OCI resolve."""
    if not link.is_file():
        return None
    with suppress(OSError, ValueError):
        _digest, _, recorded = link.read_text(encoding="utf-8").partition("\n")
        rootfs = Path(recorded.strip())
        if rootfs.joinpath(".complete").is_file():
            return rootfs
    return None


def _read_oci_manifest(tar: tarfile.TarFile) -> tuple[bytes, str]:
    """Return ``(manifest_bytes, manifest_digest)`` from an OCI layout tar."""
    index_member = None
    with suppress(KeyError):
        index_member = tar.getmember("index.json")
    if index_member is None:
        raise ImageResolutionError("oci layout has no index.json")
    index = json.loads(tar.extractfile(index_member).read())
    manifests = index.get("manifests") or []
    if not manifests:
        raise ImageResolutionError("oci layout index.json lists no manifests")
    descriptor = manifests[0]
    digest = str(descriptor.get("digest") or "")
    if ":" not in digest:
        raise ImageResolutionError(f"oci layout manifest has bad digest {digest!r}")
    algo, _, hexpart = digest.partition(":")
    member_name = f"blobs/{algo}/{hexpart}"
    try:
        member = tar.getmember(member_name)
    except KeyError as e:
        raise ImageResolutionError(f"oci layout is missing {member_name}") from e
    blob = tar.extractfile(member).read()
    return blob, digest


def _extract_local_oci(image: str, cache: Path, tar_path: Path) -> tuple[Path, str]:
    """Assemble the rootfs for ``image`` from its OCI layout tar."""
    with tarfile.open(tar_path, mode="r:*") as tar:
        manifest_bytes, digest = _read_oci_manifest(tar)
        manifest = json.loads(manifest_bytes)

        def layer_blobs() -> Iterable[bytes]:
            for layer in manifest.get("layers", []):
                layer_digest = str(layer.get("digest") or "")
                if ":" not in layer_digest:
                    raise ImageResolutionError(
                        f"image {image} manifest layer missing digest"
                    )
                algo, _, hexpart = layer_digest.partition(":")
                try:
                    member = tar.getmember(f"blobs/{algo}/{hexpart}")
                except KeyError as e:
                    raise ImageResolutionError(
                        f"oci layout is missing blobs/{algo}/{hexpart}"
                    ) from e
                yield tar.extractfile(member).read()

        rootfs = _materialize_entry(
            Path(cache), image, _entry_name(image, digest), layer_blobs
        )
    return rootfs, digest


def _resolve_local_oci(image: str, cache: Path, tar_path: Path, link: Path) -> Path:
    """Extract the local OCI tar once and remember the result in the sidecar."""
    rootfs, digest = _extract_local_oci(image, cache, tar_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text(f"{digest}\n{rootfs}", encoding="utf-8")
    logger.info("resolved locally built image %s to rootfs %s", image, rootfs)
    return rootfs


def peek_image_warm(
    image: str,
    cache_dir: str | Path,
    *,
    registry_username: str | None = None,
    registry_password: str | None = None,
    scheme: str | None = None,
    credential_host: str | None = None,
) -> dict[str, object]:
    """Return ``{"cached": bool, "digest": str | None}`` without extracting.

    ``cached`` is true when the current platform manifest digest already has
    a completed rootfs in the cache. Never raises: failures degrade to
    ``{"cached": False, "digest": None}`` so callers can decide policy.
    """
    if not image:
        return {"cached": False, "digest": None}
    rootfs = _rootfs_from_local_link(local_oci_paths(Path(cache_dir), image)[1])
    if rootfs is not None:
        return {"cached": True, "digest": _link_digest(local_oci_paths(Path(cache_dir), image)[1])}
    if local_oci_paths(Path(cache_dir), image)[0].is_file():
        # Built on this node but not extracted yet: a create that may warm it.
        return {"cached": False, "digest": None}
    try:
        digest = _platform_digest(
            image,
            registry_username=registry_username,
            registry_password=registry_password,
            scheme=scheme,
            credential_host=credential_host,
        )
        rootfs = _cache_rootfs(Path(cache_dir), image, digest)
        return {"cached": (rootfs / ".complete").is_file(), "digest": digest}
    except Exception as e:
        logger.warning("warm peek failed for %s: %s", image, e)
        return {"cached": False, "digest": None}


def resolve_image_rootfs(
    image: str,
    cache_dir: str | Path,
    *,
    registry_username: str | None = None,
    registry_password: str | None = None,
    scheme: str | None = None,
    credential_host: str | None = None,
) -> Path:
    """Return the extracted rootfs path for ``image``, creating it if needed."""
    if not image:
        raise ImageResolutionError("no base image configured")

    cache = Path(cache_dir)
    tar_path, link = local_oci_paths(cache, image)
    cached_local = _rootfs_from_local_link(link)
    if cached_local is not None:
        return cached_local
    if tar_path.is_file():
        return _resolve_local_oci(image, cache, tar_path, link)
    shared_rootfs = _shared_cache_rootfs(image, cache)
    if shared_rootfs is not None:
        return shared_rootfs
    _ref, client = _client_for(
        image,
        registry_username=registry_username,
        registry_password=registry_password,
        scheme=scheme,
        credential_host=credential_host,
    )
    try:
        manifest, digest = fetch_platform_manifest(client)
    except RegistryError as e:
        raise ImageResolutionError(f"failed to resolve image {image}: {e}") from e

    rootfs = _cache_rootfs(cache, image, digest)
    marker = rootfs / ".complete"
    if marker.is_file():
        return rootfs

    def layer_blobs() -> Iterable[bytes]:
        for layer in manifest.get("layers", []):
            digest_ = layer.get("digest")
            if not digest_:
                raise ImageResolutionError(
                    f"image {image} manifest layer missing digest"
                )
            yield client.blob(digest_)

    try:
        rootfs = _materialize_entry(
            cache, image, _entry_name(image, digest), layer_blobs
        )
    except ImageResolutionError:
        raise
    except Exception as e:
        raise ImageResolutionError(f"failed to extract image {image}: {e}") from e
    logger.info("resolved base image %s to rootfs %s", image, rootfs)
    return rootfs
