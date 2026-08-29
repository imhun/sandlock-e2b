"""Resolve a Docker base image to an extracted rootfs for Sandlock chroot.

Uses the Docker daemon API (``docker create`` + ``docker export``) exactly
once per image, caching the extracted rootfs under ``E2B_IMAGE_CACHE_DIR``.
Only meaningful on Linux inside the test runner / deployment host.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class ImageResolutionError(RuntimeError):
    pass


def _image_cache_name(image: str) -> str:
    return _SAFE.sub("_", image)[:128] or "image"


def _image_registry_host(image: str) -> str | None:
    """Return the registry host when ``image`` carries one, else ``None``."""
    first = image.split("/")[0]
    if first == "localhost" or "." in first or ":" in first:
        return first
    return None


def _ensure_registry_login(
    registry: str, username: str | None, password: str | None
) -> None:
    """Log the daemon in when credentials are configured (stdin, not argv)."""
    if not username or not password:
        return
    result = subprocess.run(
        ["docker", "login", registry, "-u", username, "--password-stdin"],
        input=password,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ImageResolutionError(
            f"docker login to {registry} failed: {result.stderr.strip()}"
        )


def _ensure_image_pulled(image: str) -> None:
    """Pull the image when the local Docker daemon does not have it yet.

    Worker nodes resolve base images (including registry-hosted template
    images) on their local daemon; pulling here lets a worker that never saw
    the image build the rootfs cache without manual distribution.
    """
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    if inspect.returncode == 0:
        return
    pull = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
    )
    if pull.returncode != 0:
        raise ImageResolutionError(
            f"failed to pull image {image}: {pull.stderr.strip()}"
        )


def _image_digest(image: str) -> str:
    """Return a stable cache key suffix for the currently pulled image.

    Uses the first repo digest (``name@sha256:...``) when available, falling
    back to the image ID. Because the digest changes when the tag points at a
    newer image, caching under it makes stale rootfs caches self-invalidate:
    a refreshed tag simply resolves to a fresh rootfs directory.
    """
    for fmt in (
        "{{index .RepoDigests 0}}",
        "{{.Id}}",
    ):
        inspect = subprocess.run(
            ["docker", "image", "inspect", image, "--format", fmt],
            capture_output=True,
            text=True,
        )
        if inspect.returncode == 0 and inspect.stdout.strip():
            value = inspect.stdout.strip().split("@", 1)[-1]
            return _SAFE.sub("_", value)[:40]
    return "unknown"


def resolve_image_rootfs(
    image: str,
    cache_dir: str | Path,
    *,
    registry_username: str | None = None,
    registry_password: str | None = None,
) -> Path:
    """Return the extracted rootfs path for ``image``, creating it if needed."""
    if not image:
        raise ImageResolutionError("no base image configured")

    if shutil.which("docker") is None:
        raise ImageResolutionError(
            "docker CLI is required to resolve base image rootfs"
        )

    cache = Path(cache_dir)
    registry = _image_registry_host(image)
    if registry is not None:
        _ensure_registry_login(registry, registry_username, registry_password)
    _ensure_image_pulled(image)
    digest = _image_digest(image)
    cache_name = f"{_image_cache_name(image)}-{digest}"
    rootfs = cache / cache_name / "rootfs"
    marker = rootfs / ".complete"
    if marker.is_file():
        return rootfs

    rootfs.mkdir(parents=True, exist_ok=True)
    container = f"e2b-sandlock-{cache_name}"
    try:
        subprocess.run(
            ["docker", "rm", "-f", container],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "create", "--name", container, image],
            check=True,
            capture_output=True,
            text=True,
        )
        with subprocess.Popen(
            ["docker", "export", container],
            stdout=subprocess.PIPE,
        ) as exporter:
            assert exporter.stdout is not None
            with subprocess.Popen(
                ["tar", "-x", "-C", str(rootfs)],
                stdin=exporter.stdout,
            ) as untar:
                exporter.stdout.close()
                untar.wait()
            exporter.wait()
        subprocess.run(
            ["docker", "rm", "-f", container],
            check=False,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        shutil.rmtree(rootfs, ignore_errors=True)
        raise ImageResolutionError(f"failed to extract image {image}: {e.stderr}") from e

    if not (rootfs / "bin").is_dir() and not (rootfs / "usr" / "bin").is_dir():
        shutil.rmtree(rootfs, ignore_errors=True)
        raise ImageResolutionError(f"image {image} produced an empty rootfs")
    marker.write_text("ok", encoding="utf-8")
    logger.info("resolved base image %s to rootfs %s", image, rootfs)
    return rootfs
