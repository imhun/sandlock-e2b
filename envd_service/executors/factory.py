"""Executor selection: sandlock on capable Linux hosts, local otherwise."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from envd_service.config import Settings
from envd_service.executors.base import Executor
from envd_service.executors.local import LocalExecutor
from envd_service.runtime.image_resolver import resolve_image_rootfs

logger = logging.getLogger(__name__)


def _sandlock_available() -> bool:
    if sys.platform != "linux":
        return False
    try:
        import sandlock  # noqa: F401

        return True
    except Exception:
        return False


def _landlock_ok(min_abi: int = 6) -> bool:
    try:
        import sandlock

        return sandlock.landlock_abi_version() >= min_abi
    except Exception:
        return False


def create_executor(
    settings: Settings,
    *,
    workspace_dir: str,
    base_image: str | None,
    memory_mb: int,
    cpu_percent: int,
    disk_mb: int,
    max_processes: int,
    max_open_files: int,
    allow_internet_access: bool,
    network: dict | None = None,
    extra_fs_writable: list[str] | None = None,
    fs_mounts: dict[str, str] | None = None,
) -> Executor:
    """Pick the executor honoring ``E2B_EXECUTOR`` (``auto``|``local``|``sandlock``)."""
    mode = settings.executor
    image_rootfs: Path | None = None

    if mode == "sandlock" or (mode == "auto" and _sandlock_available()):
        if not _landlock_ok():
            if mode == "sandlock":
                raise RuntimeError(
                    "E2B_EXECUTOR=sandlock requires Landlock ABI >= 6"
                )
            logger.warning("Landlock ABI < 6 on Linux; falling back to local executor")
        else:
            from envd_service.executors.sandlock import SandlockExecutor

            if base_image:
                image_rootfs = resolve_image_rootfs(
                    base_image,
                    settings.image_cache_dir,
                    registry_username=settings.image_registry_username,
                    registry_password=settings.image_registry_password,
                )
            return SandlockExecutor(
                workspace_dir=workspace_dir,
                base_image=base_image,
                image_rootfs=image_rootfs,
                memory_mb=memory_mb,
                cpu_percent=cpu_percent,
                disk_mb=disk_mb,
                max_processes=max_processes,
                max_open_files=max_open_files,
                allow_internet_access=allow_internet_access,
                enable_network=settings.enable_network,
                network=network,
                extra_fs_writable=extra_fs_writable,
                fs_mounts=fs_mounts,
            )

    logger.info("using local executor for sandbox %s", workspace_dir)
    return LocalExecutor()
