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


def _import_sandlock() -> BaseException | None:
    """Import the sandlock package, returning the failure instead of raising.

    ``ModuleNotFoundError`` means "not installed at all" -- the documented
    auto-mode fallback to :class:`LocalExecutor` is fine there.  **Every other
    failure means the package is installed but broken** (a wheel built against
    a different ``libsandlock_ffi.so``, a partially upgraded image, a missing
    export): treating that as "sandlock is unavailable" would silently run the
    sandbox with no confinement at all.  Callers must be loud about it.
    """
    try:
        import sandlock  # noqa: F401

        return None
    except Exception as exc:  # noqa: BLE001 - classified by the callers
        return exc


def _sandlock_available() -> bool:
    if sys.platform != "linux":
        return False
    return _import_sandlock() is None


def _broken_sandlock_detail() -> str | None:
    """Why the installed sandlock package is unusable, or None when it is fine.

    A *missing* package is not a broken one: it returns None so the documented
    auto-mode fallback still applies.
    """
    if sys.platform != "linux":
        return None
    failure = _import_sandlock()
    if failure is None or isinstance(failure, ModuleNotFoundError):
        return None
    return f"{type(failure).__name__}: {failure}"


def sandlock_unusable_error(mode: str, detail: str) -> RuntimeError:
    """The fail-closed error for an installed-but-unusable sandlock package.

    Shared by the executor factory (`sandlock` and `auto`) and the image
    probes in `envd_service.agent` / `control_plane.api.sandboxes` so the
    judgment and the words cannot drift (B1 fix round 2).
    """
    return RuntimeError(
        f"E2B_EXECUTOR={mode} cannot run: the sandlock package is installed but "
        f"unusable ({detail}); refusing to fall back to the LOCAL executor, "
        "which applies no sandbox confinement. Reinstall the matching sandlock "
        "wheel (or rebuild libsandlock_ffi.so) and restage the worker image."
    )


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
    host_uid: int | None = None,
    per_sandbox_uid: bool = False,
    memory_mb: int,
    cpu_percent: int,
    disk_mb: int,
    max_processes: int,
    max_open_files: int,
    allow_internet_access: bool,
    network: dict | None = None,
    iam_tokens: dict[str, dict[str, str]] | None = None,
    egress_lib_dir: str | Path | None = None,
    extra_fs_writable: list[str] | None = None,
    fs_mounts: dict[str, str] | None = None,
    sandbox_id: str | None = None,
) -> Executor:
    """Pick the executor honoring ``E2B_EXECUTOR`` (``auto``|``local``|``sandlock``)."""
    mode = settings.executor
    image_rootfs: Path | None = None

    # B1 review fix round 2 (security): "sandlock is installed but broken" must
    # never be mistaken for "sandlock is unavailable" -- the fallback below is
    # LocalExecutor, which applies NO sandbox confinement. Only a *missing*
    # package (ModuleNotFoundError) may fall back in auto mode; an unusable one
    # fails the sandbox creation with the reason, for `auto` and `sandlock`
    # alike. `local` is the operator's explicit choice and is left untouched
    # (everything below is skipped, exactly as before B1).
    if mode != "local":
        broken = _broken_sandlock_detail()
        if broken is not None:
            raise sandlock_unusable_error(mode, broken)

    if mode == "sandlock" or (mode == "auto" and _sandlock_available()):
        if not _landlock_ok():
            if mode == "sandlock":
                raise RuntimeError(
                    "E2B_EXECUTOR=sandlock requires Landlock ABI >= 6"
                )
            logger.warning("Landlock ABI < 6 on Linux; falling back to local executor")
        else:
            from envd_service.executors.sandlock import SandlockExecutor
            from envd_service.route_b import RouteBConfig

            if base_image:
                image_rootfs = resolve_image_rootfs(
                    base_image,
                    settings.image_cache_dir,
                    registry_username=settings.image_registry_username,
                    registry_password=settings.image_registry_password,
                    credential_host=settings.image_registry_host,
                )
            return SandlockExecutor(
                workspace_dir=workspace_dir,
                base_image=base_image,
                image_rootfs=image_rootfs,
                host_uid=host_uid,
                per_sandbox_uid=per_sandbox_uid,
                memory_mb=memory_mb,
                cpu_percent=cpu_percent,
                disk_mb=disk_mb,
                max_processes=max_processes,
                max_open_files=max_open_files,
                allow_internet_access=allow_internet_access,
                enable_network=settings.enable_network,
                enable_netns=settings.enable_netns,
                enable_net_isolation=settings.enable_net_isolation,
                fd_inject_connect=settings.fd_inject_connect,
                port_mappings=settings.port_mappings,
                network=network,
                network_deny_cidrs=settings.network_deny_cidrs,
                notify_rate_limit=settings.sandbox_notify_rate_limit,
                iam_tokens=iam_tokens,
                iam_signing_key=settings.iam_signing_key,
                secrets_dir=settings.image_cache_dir / "secrets",
                extra_fs_writable=extra_fs_writable,
                fs_mounts=fs_mounts,
                sandbox_id=sandbox_id,
                route_b=RouteBConfig.from_settings(settings),
            )

    logger.info("using local executor for sandbox %s", workspace_dir)
    return LocalExecutor()
