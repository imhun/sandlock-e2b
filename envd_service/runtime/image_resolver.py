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

The directory is *shared by processes running as different uids* in the shipped
manifests (the control plane runs as root, the workers as 65534), so everything
the resolver creates belongs to the cache owner (the worker uid, see
:func:`_cache_owner_ids`) with owner-only write bits: the worker writes as the
owner, a root-run peer writes through ``CAP_DAC_OVERRIDE``, and a sandbox uid
can only read and traverse. It is never world-writable -- the cache holds the
rootfs every sandbox chroots into, so a writable cache entry is a cross-tenant
poisoning vector.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

import tarfile
from contextlib import contextmanager, suppress
from typing import Any, Callable, Iterable, Iterator

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

#: Staging trees this process is *currently* working in. The pid in a staging
#: name is not enough to prove a tree is dead: another thread of this same
#: process may be extracting into one right now. GC therefore only reclaims a
#: tree carrying our pid when it is not in here.
_ACTIVE_STAGING: set[str] = set()
_ACTIVE_STAGING_GUARD = threading.Lock()

#: Modes for everything the resolver creates inside the shared cache. The
#: directory is shared by two *different* production identities (the control
#: plane runs as root, the workers as 65534) and read/traversed by the sandbox
#: uids, so it is owner-writable only: 0755 directories, 0644 files. Widening
#: it to 0777 would let a sandbox rewrite the rootfs another sandbox chroots
#: into -- a cache-poisoning vector, not an option.
_SHARED_DIR_MODE = 0o755
_SHARED_FILE_MODE = 0o644
_CACHE_OWNER_UID_ENV = "E2B_IMAGE_CACHE_OWNER_UID"
_CACHE_OWNER_GID_ENV = "E2B_IMAGE_CACHE_OWNER_GID"
_CACHE_LOCK_TIMEOUT_ENV = "E2B_IMAGE_CACHE_LOCK_TIMEOUT_S"
_DEFAULT_CACHE_LOCK_TIMEOUT_S = 300.0
#: ``flock`` has no timed wait, so the lock is polled; this is the granularity
#: at which a waiter notices the holder died or its own deadline passed.
_LOCK_POLL_S = 0.2
#: How long the publish path retries when the entry name is held by another
#: publisher (only reachable on storage whose locks are not honoured across
#: clients), and how often it looks again.
_PUBLISH_RETRY_S = 10.0
_PUBLISH_POLL_S = 0.05


class ImageResolutionError(RuntimeError):
    pass


class CacheLockTimeout(ImageResolutionError):
    """The cross-process cache lock stayed held past the configured deadline.

    Carries the fields so a caller can tell a genuine timeout from a real
    extraction failure; ``resolve_image_rootfs`` lets it through with the same
    class (it is an :class:`ImageResolutionError`), only after re-checking
    whether the process that held the lock published the entry meanwhile.
    """

    def __init__(self, lock_path: Path, timeout: float, image: str) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self.image = image
        super().__init__(
            f"timed out after {timeout:g}s waiting for another process to finish "
            f"resolving image {image} (lock {self.lock_path}; raise "
            f"{_CACHE_LOCK_TIMEOUT_ENV}, or set it to 0 to wait forever)"
        )


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


def _register_staging(path: Path) -> None:
    with _ACTIVE_STAGING_GUARD:
        _ACTIVE_STAGING.add(path.name)


def _release_staging(path: Path) -> None:
    with _ACTIVE_STAGING_GUARD:
        _ACTIVE_STAGING.discard(path.name)


def _is_active_staging(name: str) -> bool:
    with _ACTIVE_STAGING_GUARD:
        return name in _ACTIVE_STAGING


def _cache_owner_ids(path: Path) -> tuple[int, int] | None:
    """``(uid, gid)`` the shared cache belongs to, or ``None`` when unknown.

    The cache is written by two production identities -- the worker (65534 in
    both shipped manifests) and a root-run control plane -- so every directory
    and file the resolver creates is *given to the cache owner*: the worker
    then writes it as the owner, root writes it through ``CAP_DAC_OVERRIDE``,
    and a sandbox uid (10000+) only gets the read/traverse bits.

    Resolution order:

    1. ``E2B_IMAGE_CACHE_OWNER_UID`` / ``E2B_IMAGE_CACHE_OWNER_GID`` (the
       manifests set the uid explicitly, so the intent is not a guess);
    2. the owner of the nearest existing ancestor that is not root: the volume
       root ``/var/lib/e2b-sandboxes`` belongs to the worker uid by design (the
       worker image pre-creates it and ``deploy/scripts/upgrade.sh`` chowns an
       older volume once), so the cache inherits the same owner;
    3. ``None`` (local development, everything runs as one uid): no chown.
    """
    raw_uid = os.environ.get(_CACHE_OWNER_UID_ENV, "").strip()
    if raw_uid:
        try:
            uid = int(raw_uid)
        except ValueError:
            logger.warning(
                "ignoring invalid %s=%r (expected a uid)", _CACHE_OWNER_UID_ENV, raw_uid
            )
        else:
            raw_gid = os.environ.get(_CACHE_OWNER_GID_ENV, "").strip()
            try:
                gid = int(raw_gid) if raw_gid else uid
            except ValueError:
                logger.warning(
                    "ignoring invalid %s=%r (expected a gid)",
                    _CACHE_OWNER_GID_ENV,
                    raw_gid,
                )
                gid = uid
            return uid, gid
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.stat()
        except OSError:
            continue
        if info.st_uid != 0:
            return info.st_uid, info.st_gid
    return None


def _ensure_shared_dir(path: Path) -> None:
    """``mkdir -p`` a cache directory the shared-cache contract allows.

    Never world-writable (see ``_SHARED_DIR_MODE``). Owning the directory is
    what makes the *other* production identity able to write inside it, so
    when this process runs as root the directory is handed to the cache owner;
    the mode is re-asserted every time, which also tightens a directory a
    previous deployment left at 0777.
    """
    try:
        path.mkdir(mode=_SHARED_DIR_MODE, parents=True, exist_ok=True)
    except OSError as e:
        owner = _cache_owner_ids(path)
        hint = (
            f"chown -R {owner[0]}:{owner[1]} {path}"
            if owner is not None
            else f"chown -R <worker-uid>:<worker-gid> {path}"
        )
        raise ImageResolutionError(
            f"shared image cache directory {path} is not usable by uid "
            f"{os.geteuid()}: {e} (it belongs to the worker uid that owns the "
            f"volume; fix it once with `{hint}` as root)"
        ) from e
    if os.geteuid() == 0:
        owner = _cache_owner_ids(path)
        if owner is not None:
            with suppress(OSError):
                os.chown(path, owner[0], owner[1])
    with suppress(OSError):
        os.chmod(path, _SHARED_DIR_MODE)


def _adopt_tree(path: Path, owner: tuple[int, int] | None) -> None:
    """Give ``path`` (recursively) to the cache owner when running as root.

    A root-run resolver publishes entries the workers have to keep *writing*
    inside (``sandlock.py`` creates the mount points under ``<rootfs>`` and the
    MITM CA file), and the worker cannot write a root-owned tree. Only called
    on the cold-publish path and only in the root shape -- the production
    publisher is the worker itself, where this is a no-op. ``lchown`` (not
    ``chown``) so symlinks inside an image are never followed onto the host.
    """
    if owner is None or os.geteuid() != 0 or owner[0] == 0:
        return
    uid, gid = owner
    with suppress(OSError):
        os.lchown(path, uid, gid)
    for root, dirs, files in os.walk(path, onerror=lambda _exc: None):
        with suppress(OSError):
            os.lchown(root, uid, gid)
        for name in dirs + files:
            with suppress(OSError):
                os.lchown(os.path.join(root, name), uid, gid)


def _lock_path(cache_dir: Path, image: str) -> Path:
    """``<cache>/<image-slug>.lock``: the *cross-process* lock for one image.

    It sits next to the cache entries (same volume), and it is deliberately
    never unlinked: deleting the file would let the next two processes lock two
    different inodes of the same name and extract the entry concurrently, which
    is exactly the shape this lock exists to prevent.
    """
    return Path(cache_dir) / f"{_image_cache_name(image)}.lock"


def _open_lock_file(lock_path: Path) -> int:
    """Open (creating when absent) the cross-process lock file.

    Created ``0644`` and owned by the cache owner, so both production
    identities can take it: the owner writes it, a root-run peer opens it
    through ``CAP_DAC_OVERRIDE``, and any *other* uid can still open it
    read-only -- ``flock`` needs no write access, which is the retry below. That
    retry is also what keeps a lock file an older resolver left ``0600`` for one
    uid from locking the other uid out of the whole image.
    """
    owner = _cache_owner_ids(lock_path.parent)
    try:
        fd = os.open(
            lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, _SHARED_FILE_MODE
        )
    except PermissionError as e:
        if not lock_path.exists():
            raise ImageResolutionError(
                f"shared image cache directory {lock_path.parent} is not "
                f"writable by uid {os.geteuid()}: {e}"
            ) from e
        try:
            fd = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as read_only_error:
            descriptor = (
                f"{owner[0]}:{owner[1]}"
                if owner is not None
                else "<worker-uid>:<worker-gid>"
            )
            raise ImageResolutionError(
                f"cannot open the image cache lock {lock_path}: "
                f"{read_only_error} (uid {os.geteuid()} can neither write nor "
                f"read it; fix it once with `chown -R {descriptor} "
                f"{lock_path.parent}` as root)"
            ) from read_only_error
    except OSError as e:
        raise ImageResolutionError(
            f"cannot create the image cache lock {lock_path}: {e}"
        ) from e
    with suppress(OSError):
        os.fchmod(fd, _SHARED_FILE_MODE)
    if os.geteuid() == 0 and owner is not None:
        with suppress(OSError):
            os.fchown(fd, owner[0], owner[1])
    return fd


@contextmanager
def _locked_cache_entry(
    cache_dir: Path, image: str, *, timeout: float | None = None
) -> Iterator[None]:
    """Serialize "check ``.complete`` → extract → publish" across processes.

    ``flock(2)`` covers other processes (another worker, the control plane);
    the in-process lock stays because a second ``open`` in the *same* process
    would block on its own file description, and a threaded caller should
    queue on the cheap lock instead.

    Both waits are bounded by ``E2B_IMAGE_CACHE_LOCK_TIMEOUT_S`` (default
    300s; ``0`` waits forever): a worker wedged inside an extraction must not
    block every other worker that resolves the same image for an unbounded
    time. A timeout raises :class:`CacheLockTimeout` *without* having touched
    anything, so the caller can still use an entry that was published while it
    waited.
    """
    limit = _cache_lock_timeout_s() if timeout is None else float(timeout)
    lock_path = _lock_path(Path(cache_dir), image)
    _ensure_shared_dir(lock_path.parent)
    deadline = None if limit <= 0 else time.monotonic() + limit
    lock = _cache_lock(_image_cache_name(image))
    fd = _open_lock_file(lock_path)
    try:
        waiting = -1.0 if deadline is None else max(0.0, deadline - time.monotonic())
        if not lock.acquire(timeout=waiting):
            raise CacheLockTimeout(lock_path, limit, image)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if deadline is not None and time.monotonic() >= deadline:
                        raise CacheLockTimeout(lock_path, limit, image) from None
                    time.sleep(_LOCK_POLL_S)
            yield
        finally:
            lock.release()
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
    it, and the cache owner has to be able to write it. The rename keeps the
    directory's own mode, so it is set to the shared-cache contract here (0755,
    owner = cache owner); the staging tree is only reachable through the cache
    directory.

    The name carries this process's pid, so a tree left behind by a ``SIGKILL``
    can be attributed to its creator: only a tree carrying *our* pid -- or one
    that is older than the staleness threshold -- is ever reclaimed.
    """
    staging = Path(
        tempfile.mkdtemp(prefix=f".{entry_name}.tmp-{os.getpid()}-", dir=cache_dir)
    )
    _ensure_shared_dir(staging)
    _register_staging(staging)
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
    """Publish a fully extracted entry with one atomic rename.

    ``.complete`` is written inside the staging tree *before* this call, so the
    entry becomes visible already marked complete: a reader can never observe a
    partially written rootfs, even on storage where the lock file itself is not
    honoured (NFS mounted ``nolock``, for one).

    A completed entry always wins: if another process got there first (possible
    exactly when the lock is not honoured), this process throws away its own
    staging tree instead of replacing or deleting a finished entry, so a reader
    is never left with a hole where a completed rootfs used to be.

    A name held by *incomplete* leftover garbage (a crashed pre-Z-F7 worker is
    the only producer of that shape) is removed through
    :func:`_claim_incomplete_entry`: the leftover is first taken by an atomic
    rename and re-verified to still be the incomplete tree this process
    inspected. The removal target is therefore never a *published* entry, which
    the old code could delete when the lock did not hold across clients.
    """
    if _entry_complete(entry):
        _remove_path(staging)
        return
    deadline = time.monotonic() + _PUBLISH_RETRY_S
    while True:
        try:
            # ``os.rename`` (not ``os.replace``) refuses a non-empty target, so
            # a leftover name is never silently clobbered here.
            os.rename(staging, entry)
            return
        except OSError:
            pass
        if _entry_complete(entry):
            _remove_path(staging)
            return
        claimed = _claim_incomplete_entry(entry)
        if claimed is not None:
            # A name held by *incomplete* leftover garbage: it is ours now, and
            # the claim verified that it is still that same incomplete tree.
            _remove_path(claimed)
            continue
        if _entry_complete(entry):
            # Somebody published the entry while we were looking at the name.
            _remove_path(staging)
            return
        if time.monotonic() >= deadline:
            _remove_path(staging)
            raise ImageResolutionError(
                f"could not publish the image cache entry {entry}: another "
                f"process holds the name"
            )
        # Another publisher is mid-publish (only reachable when the lock is not
        # honoured across clients): give it a moment and look again.
        time.sleep(_PUBLISH_POLL_S)


def _claim_incomplete_entry(entry: Path) -> Path | None:
    """Take an *incomplete* leftover entry out of the way, atomically.

    ``os.rename`` is atomic, so exactly one process can hold a given name at a
    time. The tree this process now holds is then checked again -- it must
    still be incomplete, and it must be the very inode the caller inspected --
    before anything is removed. A tree that became a *published* entry in that
    window is renamed straight back and ``None`` is returned, so a completed
    rootfs is never destroyed, not even on storage whose locks are not honoured
    across clients.
    """
    try:
        observed = os.stat(entry)
    except OSError:
        return None
    if _entry_complete(entry):
        return None
    claim = entry.with_name(f".{entry.name}.garbage-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        os.rename(entry, claim)
    except OSError:
        return None
    try:
        held = os.stat(claim)
    except OSError:
        return None
    if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino) or _entry_complete(
        claim
    ):
        # The name changed hands between the check and the claim: whatever we
        # grabbed is not the leftover we inspected, so give it back untouched.
        with suppress(OSError):
            os.rename(claim, entry)
        return None
    return claim


def _extract_layers(image: str, rootfs: Path, blobs: Iterable[bytes]) -> None:
    """Unpack ``blobs`` into ``rootfs`` and mark it complete."""
    rootfs.mkdir(parents=True, exist_ok=True)
    # Deterministic, traversable mode for the sandbox uid (see _stage_entry).
    _ensure_shared_dir(rootfs)
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

    The entry that was just published is passed to the GC as *protected*, so
    the bound this call enforces can never evict the very rootfs it is about to
    hand back (with ``E2B_IMAGE_CACHE_EVICT_MIN_AGE_S=0`` the old code deleted
    it and returned a path that no longer existed).
    """
    cache_dir = Path(cache_dir)
    entry = cache_dir / entry_name
    rootfs = entry / "rootfs"
    marker = rootfs / ".complete"
    if marker.is_file():
        return rootfs
    try:
        with _locked_cache_entry(cache_dir, image):
            if marker.is_file():
                return rootfs
            owner = _cache_owner_ids(cache_dir)
            staging = _stage_entry(cache_dir, entry_name)
            try:
                _extract_layers(image, staging / "rootfs", blobs())
                _adopt_tree(staging, owner)
                _publish_staged_entry(staging, entry)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                _release_staging(staging)
    except CacheLockTimeout:
        # Somebody else is extracting this image and did not finish within the
        # deadline. If they published it in the meantime the cache hit is as
        # good as ours; otherwise the timeout is the error.
        if marker.is_file():
            return rootfs
        raise
    _maybe_prune_cache(cache_dir, protect=(entry_name,))
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
#: Unset means **no eviction**. Evicting is an operational decision with a real
#: failure mode (an entry evicted while a sandbox chroots into it breaks every
#: new command in every sandbox using that image), so the default is the
#: conservative one and the shipped manifests set the bound explicitly.
_DEFAULT_CACHE_MAX_BYTES = 0
_CACHE_MIN_AGE_ENV = "E2B_IMAGE_CACHE_EVICT_MIN_AGE_S"
_DEFAULT_CACHE_MIN_AGE_S = 300.0
#: Floor under ``E2B_IMAGE_CACHE_EVICT_MIN_AGE_S``: the eviction pass runs
#: right after a publish, so a configured ``0`` would let a process evict the
#: entry it just published (and hand back a path that no longer exists). 60s is
#: also the GC throttle, i.e. the shortest useful freshness window.
_MIN_EVICT_MIN_AGE_S = 60.0
#: ``E2B_IMAGE_CACHE_STAGING_STALE_S``: a staging tree (or a quarantine tree)
#: this old cannot belong to a live extraction any more, so GC may reclaim it.
#: ``0`` disables age-based reclamation (only this process's own leftovers go).
_CACHE_STAGING_STALE_ENV = "E2B_IMAGE_CACHE_STAGING_STALE_S"
_DEFAULT_STAGING_STALE_S = 3600.0
#: The cap only matters on the scale of minutes, and the walk is O(cache): do
#: it at most this often per process.
_PRUNE_INTERVAL_S = 60.0
_last_prune_monotonic = 0.0


def _cache_max_bytes() -> int:
    """``E2B_IMAGE_CACHE_MAX_BYTES`` (bytes); ``0`` (the default) disables it."""
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
    """``E2B_IMAGE_CACHE_EVICT_MIN_AGE_S``: freshness floor for eviction.

    Values below ``_MIN_EVICT_MIN_AGE_S`` are raised to it: a floor of zero
    would let the GC pass that follows a publish evict that same entry.
    """
    raw = os.environ.get(_CACHE_MIN_AGE_ENV, "").strip()
    if not raw:
        return _DEFAULT_CACHE_MIN_AGE_S
    try:
        value = max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r (expected seconds)",
            _CACHE_MIN_AGE_ENV,
            raw,
        )
        return _DEFAULT_CACHE_MIN_AGE_S
    if value < _MIN_EVICT_MIN_AGE_S:
        logger.warning(
            "%s=%s is below the %ss floor that keeps an eviction pass from "
            "taking a just-published entry; using %ss",
            _CACHE_MIN_AGE_ENV,
            raw,
            _MIN_EVICT_MIN_AGE_S,
            _MIN_EVICT_MIN_AGE_S,
        )
        return _MIN_EVICT_MIN_AGE_S
    return value


def _staging_stale_s() -> float:
    """``E2B_IMAGE_CACHE_STAGING_STALE_S``: age at which a leftover is junk."""
    raw = os.environ.get(_CACHE_STAGING_STALE_ENV, "").strip()
    if not raw:
        return _DEFAULT_STAGING_STALE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r (expected seconds, 0 = only our own)",
            _CACHE_STAGING_STALE_ENV,
            raw,
        )
        return _DEFAULT_STAGING_STALE_S


def _cache_lock_timeout_s() -> float:
    """``E2B_IMAGE_CACHE_LOCK_TIMEOUT_S``; ``0`` waits forever."""
    raw = os.environ.get(_CACHE_LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_CACHE_LOCK_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r (expected seconds, 0 = wait forever)",
            _CACHE_LOCK_TIMEOUT_ENV,
            raw,
        )
        return _DEFAULT_CACHE_LOCK_TIMEOUT_S


def _own_bytes(path: Path) -> int:
    """Allocated bytes of ``path`` itself (``du`` semantics)."""
    with suppress(OSError):
        info = os.lstat(path)
        return info.st_blocks * 512 if info.st_blocks else info.st_size
    return 0


def _mtime(path: Path) -> float:
    with suppress(OSError):
        return os.lstat(path).st_mtime
    return 0.0


def _tree_bytes(path: Path) -> int:
    """Allocated bytes at and under ``path`` (``du`` semantics)."""
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda _exc: None):
        for name in dirs + files:
            total += _own_bytes(Path(root) / name)
    return total + _own_bytes(path)


def _oci_tar_bytes(cache: Path) -> int:
    """Bytes held by the locally built OCI layout tars (``_oci/``)."""
    total = 0
    for tar_path in (cache / LOCAL_OCI_DIRNAME).glob("*.oci.tar"):
        with suppress(OSError):
            total += tar_path.stat().st_size
    return total


def _workspace_bases(cache_dir: Path) -> list[Path]:
    """Directories whose ``*/sandbox.json`` records reference cached images.

    ``<workspace-base>/_images`` is the production layout, so the cache's own
    parent is the first candidate; the workspace env vars are honoured too, for
    a deployment that points the cache somewhere else.
    """
    candidates = [Path(cache_dir).parent]
    for var in ("E2B_WORKSPACE_BASE", "E2B_SHARED_WORKSPACE_ROOT"):
        raw = os.environ.get(var, "").strip()
        if raw:
            candidates.append(Path(raw))
    seen: set[str] = set()
    bases: list[Path] = []
    for base in candidates:
        key = str(base.absolute())
        if key in seen or not base.is_dir():
            continue
        seen.add(key)
        bases.append(base)
    return bases


def _referenced_entry_pins(cache_dir: Path) -> tuple[set[str], set[str], int]:
    """``(pinned entry names, pinned image slugs, records read)``.

    Every ``sandbox.json`` under a workspace base is this node's own record of
    a sandbox that exists on the volume (the record survives unregister until
    the tree is torn down) and it names the base image that sandbox was created
    from. Entries for those images are *pinned*: eviction must never take the
    rootfs out from under a live sandbox's chroot, which breaks every new
    command in every sandbox using that image (Z-F7 C2).

    A record may also carry the resolved digest (``base_image_digest`` /
    ``image_digest``); then exactly that entry is pinned instead of every entry
    of the same image, so a tag that moved on can still be reclaimed.
    """
    pinned_entries: set[str] = set()
    pinned_slugs: set[str] = set()
    records = 0
    for base in _workspace_bases(cache_dir):
        for record in base.glob("*/sandbox.json"):
            try:
                payload = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            image = payload.get("base_image")
            if not isinstance(image, str) or not image:
                continue
            records += 1
            digest = payload.get("base_image_digest") or payload.get("image_digest")
            if isinstance(digest, str) and ":" in digest:
                pinned_entries.add(_entry_name(image, digest))
            else:
                pinned_slugs.add(_image_cache_name(image))
    return pinned_entries, pinned_slugs, records


def _cache_usage(cache: Path) -> dict[str, Any]:
    """Exact decomposition of the cache's real disk usage.

    The parts sum to ``_tree_bytes(cache)``: completed entries, entries with a
    name but no ``.complete`` (leftovers), staging/quarantine trees (including
    what a ``SIGKILL`` left behind), ``_oci/*.oci.tar`` and loose files (the
    lock files). Everything the resolver writes is therefore inside the bound
    reported by :func:`prune_image_cache`, not just the completed entries.
    """
    usage: dict[str, Any] = {
        "dir": _own_bytes(cache),
        "files": 0,
        "oci": 0,
        "complete": {},
        "staging": {},
        "incomplete": {},
    }
    if not cache.is_dir():
        return usage
    complete: dict[str, tuple[int, float]] = {}
    staging: dict[str, tuple[int, float]] = {}
    incomplete: dict[str, tuple[int, float]] = {}
    for child in sorted(cache.iterdir()):
        if child.name.startswith("."):
            # Staging tree, quarantine tree or the aside-written sidecar; the
            # resolver only ever creates dot-prefixed names for the first two
            # and calls the third one a leftover as well.
            size = _own_bytes(child) if not child.is_dir() else _tree_bytes(child)
            staging[child.name] = (size, _mtime(child))
            continue
        if child.is_symlink() or not child.is_dir():
            usage["files"] = int(usage["files"]) + _own_bytes(child)
            continue
        if child.name == LOCAL_OCI_DIRNAME:
            usage["oci"] = int(usage["oci"]) + _tree_bytes(child)
            continue
        marker = child / "rootfs" / ".complete"
        if marker.is_file():
            complete[child.name] = (_tree_bytes(child), _mtime(marker))
        else:
            incomplete[child.name] = (_tree_bytes(child), _mtime(child))
    usage["complete"] = complete
    usage["staging"] = staging
    usage["incomplete"] = incomplete
    return usage


def _usage_total(usage: dict[str, object]) -> int:
    total = int(usage["dir"]) + int(usage["files"]) + int(usage["oci"])
    for key in ("complete", "staging", "incomplete"):
        total += sum(size for size, _mtime_ in usage[key].values())
    return total


def _is_own_staging(name: str) -> bool:
    """Whether a leftover name carries *this* process's pid (see GC)."""
    return name.startswith(".") and f".tmp-{os.getpid()}-" in name


def prune_image_cache(
    cache_dir: str | Path,
    *,
    max_bytes: int | None = None,
    min_age_s: float | None = None,
    now: float | None = None,
    protect: Iterable[str] = (),
) -> dict[str, int]:
    """Reclaim leftovers and evict the oldest *unreferenced* completed entries.

    Only entries carrying ``rootfs/.complete`` are candidates: entries are
    published by ``os.replace``, so one that is complete is never being
    written, and an in-flight extraction only ever exists as a dot-prefixed
    staging tree (never a candidate).

    Two sets are out of reach no matter how far over the cap the cache is:

    * entries a ``sandbox.json`` on the volume references (``base_image``, or
      exactly the recorded digest when it carries one) plus the entries in
      ``protect`` -- the one the caller just published. Evicting a rootfs a
      live sandbox chroots into breaks every new command in every sandbox
      using that image;
    * anything newer than ``min_age_s`` (``E2B_IMAGE_CACHE_EVICT_MIN_AGE_S``,
      floored at ``_MIN_EVICT_MIN_AGE_S``), which keeps the newest results --
      and whatever a create may be about to use -- out of reach.

    Leftovers are reclaimed first, and only when they are provably junk: a
    staging tree carrying this process's pid, or one older than
    ``E2B_IMAGE_CACHE_STAGING_STALE_S``. Everything (staging trees, incomplete
    entries, ``_oci`` tars, lock files) is counted, so ``total_bytes`` is the
    cache's real ``du`` (``st_blocks * 512``) and can be checked against the
    disk after a prune.

    The ``_oci`` layout tars are counted -- they live on the same volume -- but
    never evicted: with no registry configured a tar is the only copy of a
    locally built image, so dropping it would break that template instead of
    merely forcing a re-pull.
    """
    cache = Path(cache_dir)
    cap = _cache_max_bytes() if max_bytes is None else int(max_bytes)
    min_age = _cache_evict_min_age_s() if min_age_s is None else max(0.0, float(min_age_s))
    stale_after = _staging_stale_s()
    reference = time.time() if now is None else float(now)
    bases = _workspace_bases(cache)
    pinned_entries, pinned_slugs, pinned_records = _referenced_entry_pins(cache)
    protected = set(protect)

    usage = _cache_usage(cache)
    total = _usage_total(usage)

    # 1) Reclaim provable leftovers. Only a tree that carries our own pid and
    #    is not being worked in right now (this process knows its own active
    #    staging trees), or one untouched for the whole staleness window, is
    #    removed -- a foreign tree may belong to a live extraction elsewhere.
    stale_removed = 0
    stale_freed = 0
    for name, (size, mtime) in sorted(usage["staging"].items()):
        if _is_active_staging(name):
            continue
        over_age = stale_after > 0 and reference - mtime > stale_after
        if not (_is_own_staging(name) or over_age):
            continue
        _remove_path(cache / name)
        total -= size
        stale_freed += size
        stale_removed += 1

    evicted = 0
    freed = 0
    skipped_fresh = 0
    skipped_pinned = 0
    if cap > 0 and bases:
        candidates = sorted(
            usage["complete"].items(), key=lambda item: item[1][1]
        )
        for name, (size, mtime) in candidates:
            if total <= cap:
                break
            if name in protected or name in pinned_entries:
                skipped_pinned += 1
                continue
            if any(name.startswith(f"{slug}-") for slug in pinned_slugs):
                skipped_pinned += 1
                continue
            if min_age > 0 and reference - mtime < min_age:
                skipped_fresh += 1
                continue
            shutil.rmtree(cache / name, ignore_errors=True)
            total -= size
            freed += size
            evicted += 1
    elif cap > 0 and not bases:
        logger.warning(
            "image cache %s is over its %d byte cap but no workspace base was "
            "found to enumerate the sandboxes using it; refusing to evict",
            cache,
            cap,
        )

    after = _cache_usage(cache)
    kept_bytes = sum(
        size for size, _mtime_ in after["complete"].values()
    )
    return {
        "entries": len(usage["complete"]),
        "evicted": evicted,
        "freed_bytes": freed,
        "kept_bytes": kept_bytes,
        "oci_bytes": int(after["oci"]),
        "staging_bytes": sum(
            size for size, _mtime_ in after["staging"].values()
        ),
        "incomplete_bytes": sum(
            size for size, _mtime_ in after["incomplete"].values()
        ),
        "loose_bytes": int(after["files"]),
        "total_bytes": _usage_total(after),
        "stale_removed": stale_removed,
        "stale_freed_bytes": stale_freed,
        "skipped_fresh": skipped_fresh,
        "skipped_pinned": skipped_pinned,
        "max_bytes": cap,
    }


def _maybe_prune_cache(cache_dir: Path, *, protect: Iterable[str] = ()) -> None:
    """Enforce the cache bound after a cold resolve (throttled per process).

    Runs even when the cap is ``0``: an unbounded cache still reclaims its own
    and over-age leftovers, which is the only thing that stops a crash loop
    from growing the shared volume without bound.
    """
    global _last_prune_monotonic
    cap = _cache_max_bytes()
    now = time.monotonic()
    if now - _last_prune_monotonic < _PRUNE_INTERVAL_S:
        return
    _last_prune_monotonic = now
    stats = prune_image_cache(cache_dir, max_bytes=cap, protect=protect)
    if stats["evicted"] or stats["stale_removed"]:
        logger.warning(
            "image cache %s (cap %d bytes): evicted %d completed entries, freed "
            "%d bytes; reclaimed %d leftover staging trees, freed %d bytes; "
            "%d bytes used (%d kept, %d oci, %d staging, %d incomplete, %d loose), "
            "%d entries pinned by workspace records",
            cache_dir,
            cap,
            stats["evicted"],
            stats["freed_bytes"],
            stats["stale_removed"],
            stats["stale_freed_bytes"],
            stats["total_bytes"],
            stats["kept_bytes"],
            stats["oci_bytes"],
            stats["staging_bytes"],
            stats["incomplete_bytes"],
            stats["loose_bytes"],
            stats["skipped_pinned"],
        )
    elif cap > 0 and stats["total_bytes"] > cap:
        logger.warning(
            "image cache %s still over %d bytes after eviction (kept=%d oci=%d; "
            "staging=%d incomplete=%d loose=%d; %d entries are pinned by "
            "workspace records, the freshness floor is %ss and the OCI layout "
            "tars are never evicted)",
            cache_dir,
            cap,
            stats["kept_bytes"],
            stats["oci_bytes"],
            stats["staging_bytes"],
            stats["incomplete_bytes"],
            stats["loose_bytes"],
            stats["skipped_pinned"],
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


def ensure_shared_cache_dir(cache_dir: str | Path) -> Path:
    """Prepare a *configured* shared cache for both production identities.

    The resolver prepares every directory it creates itself, but the control
    plane (root, no registry configured) exports a template's OCI layout tar
    into ``_images/_oci/`` through ``control_plane/api/templates.py`` without
    going through the resolver -- and a ``root:root 0755`` ``_images`` locks the
    65534 workers out of the whole cache. Callers that own a cache directory
    (``Settings.image_cache_dir`` when ``E2B_IMAGE_CACHE_DIR`` is set) call this
    first, so the directory and its ``_oci`` subdirectory already belong to the
    worker uid (0755, never world-writable) before either uid writes.
    """
    cache = Path(cache_dir)
    _ensure_shared_dir(cache)
    _ensure_shared_dir(cache / LOCAL_OCI_DIRNAME)
    if os.geteuid() == 0:
        # The lock files live directly in the cache and are deliberately never
        # deleted; give them the shared contract too, so a file an older
        # resolver created ``0600`` for root alone cannot keep a worker out of
        # the image forever.
        owner = _cache_owner_ids(cache)
        if owner is not None:
            for child in sorted(cache.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    continue
                with suppress(OSError):
                    os.chown(child, owner[0], owner[1])
                with suppress(OSError):
                    os.chmod(child, _SHARED_FILE_MODE)
    return cache


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


def _write_shared_file(path: Path, text: str) -> None:
    """Write a small file inside the shared cache under the shared contract.

    Written aside and renamed into place: the sidecar may already belong to the
    *other* production uid (a root-run peer resolved it first), and replacing
    the name only needs write access to the directory -- which the cache owner
    has. The mode and owner are the same ones every other cache file gets.
    """
    _ensure_shared_dir(path.parent)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    _register_staging(tmp)
    try:
        tmp.write_text(text, encoding="utf-8")
        with suppress(OSError):
            os.chmod(tmp, _SHARED_FILE_MODE)
        if os.geteuid() == 0:
            owner = _cache_owner_ids(path.parent)
            if owner is not None:
                with suppress(OSError):
                    os.chown(tmp, owner[0], owner[1])
        os.replace(tmp, path)
    finally:
        _release_staging(tmp)


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
    _write_shared_file(link, f"{digest}\n{rootfs}")
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
