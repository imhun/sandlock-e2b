"""Resolve a base image to an extracted rootfs for Sandlock chroot.

Uses the OCI Distribution API directly (no Docker daemon): resolve the
platform manifest, download the layer blobs, and assemble the filesystem
with OCI whiteout semantics. The extracted rootfs is cached under
``E2B_IMAGE_CACHE_DIR`` keyed by the platform manifest digest, so a refreshed
tag self-invalidates the cache and a warm image resolves instantly.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

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
) -> tuple[ImageRef, RegistryClient]:
    ref = parse_image_ref(image)
    client = RegistryClient(
        ref,
        username=registry_username,
        password=registry_password,
        scheme=scheme,
    )
    return ref, client


def _platform_digest(
    image: str,
    *,
    registry_username: str | None,
    registry_password: str | None,
    scheme: str | None = None,
) -> str:
    _ref, client = _client_for(
        image,
        registry_username=registry_username,
        registry_password=registry_password,
        scheme=scheme,
    )
    _manifest, digest = fetch_platform_manifest(client)
    return digest


def _cache_rootfs(cache_dir: Path, image: str, digest: str) -> Path:
    cache_name = f"{_image_cache_name(image)}-{_digest_suffix(digest)}"
    return cache_dir / cache_name / "rootfs"


def peek_image_warm(
    image: str,
    cache_dir: str | Path,
    *,
    registry_username: str | None = None,
    registry_password: str | None = None,
    scheme: str | None = None,
) -> dict[str, object]:
    """Return ``{"cached": bool, "digest": str | None}`` without extracting.

    ``cached`` is true when the current platform manifest digest already has
    a completed rootfs in the cache. Never raises: failures degrade to
    ``{"cached": False, "digest": None}`` so callers can decide policy.
    """
    if not image:
        return {"cached": False, "digest": None}
    try:
        digest = _platform_digest(
            image,
            registry_username=registry_username,
            registry_password=registry_password,
            scheme=scheme,
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
) -> Path:
    """Return the extracted rootfs path for ``image``, creating it if needed."""
    if not image:
        raise ImageResolutionError("no base image configured")

    cache = Path(cache_dir)
    _ref, client = _client_for(
        image,
        registry_username=registry_username,
        registry_password=registry_password,
        scheme=scheme,
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
