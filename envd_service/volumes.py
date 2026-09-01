"""Per-sandbox volume mount provisioning (E2.5).

When a volume is created with ``per_sandbox_quota_mb > 0``, every sandbox
mounting it gets its own slice of the volume::

    <volume_path>/<sandbox_id>/

The slice is a separate XFS project (own projid + ``bhard`` limit, reuse of
the E2.2/E2.4 ``xfs_quota`` capability) and the sandbox's mount view points
at the slice, never at the volume root. Landlock ``fs_writable`` only admits
the slice as a second-layer fallback (the sandbox cannot reach other
sandboxes' slices through the shared volume root).

Backward compatibility: a volume without a per-sandbox quota
(``per_sandbox_quota_mb <= 0``) keeps the pre-E2.5 behavior — the sandbox
mounts the volume root directly, no subdirectory and no project.

Quota degradation mirrors the workspace path: when the filesystem does not
support XFS project quota (or provisioning fails), the mount falls back to
the volume root with a warning — the sandbox still works, just without a
per-sandbox limit.

Lifecycle: sandbox deletion calls :func:`cleanup_volume_projects`, which
releases the project state and removes only the sandbox's own slice. The
volume root and other sandboxes' slices are never touched (reference
counting lives on the volume side). Migration stop/rollback must NOT call
it for shared volumes: the target node re-provisions the same slice, so a
``keepVolumeSlices`` destroy only releases the runtime (the slice follows
the sandbox until the sandbox itself is deleted).

NFS form: ``via_agent=True`` delegates every ``xfs_quota`` operation to
quota-agent server-side (E2.6 wiring); unconfigured agent ops raise
:class:`ProjectQuotaError`, which degrades to the volume-root mount.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from envd_service.xfs_quota import (
    ProjectQuotaError,
    containing_mount_point,
    provision_project,
    release_project,
    xfs_project_supported,
)
from gateway_common.paths import validate_sandbox_id

logger = logging.getLogger(__name__)


def volume_projid_key(sandbox_id: str, volume_id: str, mount_path: str) -> str:
    """Stable projid seed for one (sandbox, volume, mount) quota domain.

    Distinct from the workspace project seed (plain ``sandbox_id``) and
    unique per mount, so two sandboxes on the same volume never share a
    project id and a sandbox mounting the same volume twice gets two
    independent limits.
    """
    return f"{sandbox_id}:{volume_id}:{mount_path}"


def _volume_fs_mount(path: Path, fallback: str | Path) -> Path:
    """Mount point of the filesystem containing ``path`` (fallback provided)."""
    mount = containing_mount_point(path)
    return Path(mount) if mount else Path(fallback)


def provision_sandbox_volume_mount(
    *,
    sandbox_id: str,
    volume_id: str,
    mount_path: str,
    volume_path: str | Path,
    per_sandbox_quota_mb: int,
    fallback_mount_point: str | Path,
    via_agent: bool,
    existing_projid: int | None = None,
) -> tuple[Path, int | None]:
    """Return the sandbox's mount view plus its volume projid (or None).

    ``per_sandbox_quota_mb <= 0`` -> ``(volume_path, None)`` (unchanged
    mount). Otherwise the view is ``volume_path/<sandbox_id>/`` with its own
    project + hard limit. Unsupported filesystems and provisioning failures
    degrade to the volume root with a warning, matching the workspace quota
    degradation contract.
    """
    volume_root = Path(volume_path)
    if per_sandbox_quota_mb <= 0:
        return volume_root, None
    fs_mount = _volume_fs_mount(volume_root, fallback_mount_point)
    supported, _reason = xfs_project_supported(fs_mount, via_agent=via_agent)
    if not supported:
        return volume_root, None
    sandbox_dir = volume_root / sandbox_id
    try:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        projid = provision_project(
            sandbox_id=volume_projid_key(sandbox_id, volume_id, mount_path),
            project_dir=sandbox_dir,
            mount_point=fs_mount,
            disk_mb=per_sandbox_quota_mb,
            via_agent=via_agent,
            project_id=existing_projid,
        )
    except ProjectQuotaError as exc:
        logger.warning(
            "volume quota setup failed for %s volume %s mount %s: %s",
            sandbox_id,
            volume_id,
            mount_path,
            exc,
        )
        try:
            # Only ever remove the empty directory we just created; a
            # pre-existing slice with data must survive for manual review.
            sandbox_dir.rmdir()
        except OSError:
            pass
        return volume_root, None
    return sandbox_dir, projid


def _existing_projid(
    existing_volume_projects: list[dict[str, Any]],
    volume_id: str,
    mount_path: str,
) -> int | None:
    """Reuse the persisted projid for (volume, mount) on re-provision."""
    for entry in existing_volume_projects:
        if entry.get("volume_id") == volume_id and entry.get("mount_path") == mount_path:
            projid = entry.get("projid")
            if isinstance(projid, int) and projid > 0:
                return projid
    return None


def build_volume_mounts(
    *,
    sandbox_id: str,
    volume_mounts: list[dict[str, Any]],
    shared_volume_root: str | Path | None,
    workspace_dir: str | Path,
    fallback_mount_point: str | Path,
    via_agent: bool,
    existing_volume_projects: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Materialize the sandbox-visible volume mounts and their quota state.

    Input entries mirror the agent payload: ``{"name": volume_id, "path":
    <mount path>, "hostPath": <volume root>, "perSandboxQuotaMb": int}``.
    Returns ``(mount_paths, volume_projects)`` where ``mount_paths`` entries
    are ``{"path", "hostPath"}`` with ``hostPath`` pointing at the sandbox's
    view, and ``volume_projects`` entries carry the quota lifecycle metadata.

    Raises :class:`ValueError` on invalid mount configs (mirrors the worker
    agent contract); each caller maps it to its own error response.
    """
    existing = list(existing_volume_projects or [])
    mount_paths: list[dict[str, str]] = []
    volume_projects: list[dict[str, Any]] = []
    workspace = Path(workspace_dir)
    shared_root = Path(shared_volume_root).resolve() if shared_volume_root else None
    for mount in volume_mounts:
        if not isinstance(mount, dict):
            raise ValueError("volumeMounts entries must be objects")
        host = mount.get("hostPath")
        rel = str(mount.get("path", "")).lstrip("/")
        volume_id = mount.get("name") or ""
        if not host or not rel:
            raise ValueError("volumeMounts need hostPath and path")
        if not isinstance(volume_id, str):
            raise ValueError("volumeMounts name must be a string")
        host_path = Path(host).resolve()
        if shared_root is not None and not host_path.is_relative_to(shared_root):
            raise ValueError("volume hostPath is outside the shared volume root")
        quota_raw = mount.get("perSandboxQuotaMb", 0)
        if (
            not isinstance(quota_raw, int)
            or isinstance(quota_raw, bool)
            or quota_raw < 0
        ):
            raise ValueError("volumeMounts perSandboxQuotaMb must be a non-negative integer")
        view, projid = provision_sandbox_volume_mount(
            sandbox_id=sandbox_id,
            volume_id=volume_id,
            mount_path=rel,
            volume_path=host_path,
            per_sandbox_quota_mb=quota_raw,
            fallback_mount_point=fallback_mount_point,
            via_agent=via_agent,
            existing_projid=_existing_projid(existing, volume_id, rel),
        )
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            if target.is_dir():
                raise ValueError(f"Mount path {mount['path']} already exists")
            target.unlink()
        target.symlink_to(view, target_is_directory=True)
        mount_paths.append({"path": rel, "hostPath": str(view)})
        if projid is not None:
            volume_projects.append(
                {
                    "volume_id": volume_id,
                    "sandbox_id": sandbox_id,
                    "mount_path": rel,
                    "sandbox_dir": str(view),
                    "projid": projid,
                }
            )
    return mount_paths, volume_projects


def cleanup_volume_projects(
    *,
    volume_projects: list[dict[str, Any]],
    fallback_mount_point: str | Path,
    via_agent: bool,
) -> None:
    """Release every per-sandbox volume project and remove its slice.

    Best-effort: a failed ``project -C`` is logged and the slice is still
    removed (deleting the files drops the usage accounting; the zero-usage
    quota entry is left for the E2.4 orphan reconciliation). Only the
    sandbox's own slice is ever touched — the volume root and other
    sandboxes' slices are never deleted.
    """
    for entry in volume_projects:
        if not isinstance(entry, dict):
            continue
        sandbox_dir = entry.get("sandbox_dir")
        sandbox_id = entry.get("sandbox_id")
        projid = entry.get("projid")
        # Defensive: only ever remove a slice whose name matches the sandbox
        # id that owns it (a corrupted record must not delete arbitrary
        # directories under the volume root).
        if not isinstance(sandbox_dir, (str, Path)) or not isinstance(
            sandbox_id, str
        ) or not validate_sandbox_id(sandbox_id):
            logger.warning(
                "skipping volume cleanup for invalid record: %r", entry
            )
            continue
        sandbox_dir = Path(sandbox_dir)
        if sandbox_dir.name != sandbox_id:
            logger.warning(
                "skipping volume cleanup: %s does not match sandbox %s",
                sandbox_dir,
                sandbox_id,
            )
            continue
        fs_mount = _volume_fs_mount(sandbox_dir, fallback_mount_point)
        if isinstance(projid, int) and projid > 0:
            try:
                release_project(
                    project_dir=sandbox_dir,
                    mount_point=fs_mount,
                    projid=projid,
                    via_agent=via_agent,
                )
            except ProjectQuotaError as exc:
                logger.warning(
                    "volume project cleanup failed for %s (projid %s): %s",
                    sandbox_dir,
                    projid,
                    exc,
                )
        shutil.rmtree(sandbox_dir, ignore_errors=True)
