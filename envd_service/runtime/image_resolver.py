"""Resolve a base image to an extracted rootfs for Sandlock chroot.

Uses the OCI Distribution API directly (no Docker daemon): resolve the
platform manifest, download the layer blobs, and assemble the filesystem
with OCI whiteout semantics. The extracted rootfs is cached under
``E2B_IMAGE_CACHE_DIR`` keyed by the platform manifest digest, so a refreshed
tag self-invalidates the cache and a warm image resolves instantly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

import tarfile
from contextlib import suppress

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
    cache_name = f"{_image_cache_name(image)}-{_digest_suffix(digest)}"
    return cache_dir / cache_name / "rootfs"


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
        rootfs = _cache_rootfs(cache, image, digest)
        if (rootfs / ".complete").is_file():
            return rootfs, digest
        with _cache_lock(_image_cache_name(image)):
            if not (rootfs / ".complete").is_file():
                rootfs.mkdir(parents=True, exist_ok=True)
                try:
                    for layer in manifest.get("layers", []):
                        layer_digest = str(layer.get("digest") or "")
                        if ":" not in layer_digest:
                            raise ImageResolutionError(
                                f"image {image} manifest layer missing digest"
                            )
                        algo, _, hexpart = layer_digest.partition(":")
                        member = tar.getmember(f"blobs/{algo}/{hexpart}")
                        extract_layer(tar.extractfile(member).read(), rootfs)
                    if not (rootfs / "bin").is_dir() and not (
                        rootfs / "usr" / "bin"
                    ).is_dir():
                        raise ImageResolutionError(
                            f"image {image} produced an empty rootfs"
                        )
                except Exception:
                    shutil.rmtree(rootfs.parent, ignore_errors=True)
                    raise
                rootfs.joinpath(".complete").write_text("ok", encoding="utf-8")
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

    with _cache_lock(_image_cache_name(image)):
        if marker.is_file():
            return rootfs
        rootfs.mkdir(parents=True, exist_ok=True)
        try:
            for layer in manifest.get("layers", []):
                digest_ = layer.get("digest")
                if not digest_:
                    raise ImageResolutionError(
                        f"image {image} manifest layer missing digest"
                    )
                blob = client.blob(digest_)
                extract_layer(blob, rootfs)
        except Exception as e:
            import shutil

            shutil.rmtree(rootfs.parent, ignore_errors=True)
            raise ImageResolutionError(f"failed to extract image {image}: {e}") from e

        if not (rootfs / "bin").is_dir() and not (rootfs / "usr" / "bin").is_dir():
            import shutil

            shutil.rmtree(rootfs.parent, ignore_errors=True)
            raise ImageResolutionError(f"image {image} produced an empty rootfs")
        marker.write_text("ok", encoding="utf-8")
        logger.info("resolved base image %s to rootfs %s", image, rootfs)
    return rootfs
