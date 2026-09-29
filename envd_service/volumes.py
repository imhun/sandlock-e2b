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
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from envd_service.agent_fileops import AgentFileOpsError
from envd_service.xfs_quota import (
    ProjectQuotaError,
    clear_project_limits,
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


def _ensure_traversable(path: Path) -> None:
    """Give tenant uids a way *through* every ancestor of a volume view.

    The mediator opens the volume host path as the sandbox's own uid (route-B
    slot / RunAs), so DAC needs o+x on each ancestor. Traverse-only (0111)
    where the tenant must not list, otherwise keep what is there.

    Best-effort: a path that cannot be canonicalized (a symlink loop, an
    unreadable component) falls back to its literal parent chain and only
    warns -- provisioning must not fail over a permission *widening* step.
    """
    try:
        chain = [path, *path.resolve().parents]
    except OSError as exc:
        logger.warning(
            "cannot resolve %s to widen its ancestors for tenant uids: %s",
            path,
            exc,
        )
        chain = [path, *path.parents]
    for candidate in chain:
        if candidate == Path("/"):
            break
        try:
            mode = stat.S_IMODE(candidate.stat().st_mode)
        except OSError:
            continue
        wanted = mode | 0o011
        if wanted != mode:
            try:
                os.chmod(candidate, wanted)
            except OSError as exc:
                logger.warning(
                    "cannot make %s traversable for tenant uids: %s",
                    candidate,
                    exc,
                )


def _volume_root_needs_handover(st) -> bool:
    """Whether the shared volume root still belongs to root and must move (N3).

    A seam as much as a rule: the hand-over only runs for a root-owned root
    (the legacy layout the control-plane API used to create), and a test that
    wants the branch has to force the predicate rather than depend on who owns
    its fixture.
    """
    return st.st_uid == 0


def _ensure_shared_volume_root(
    volume_root: Path,
    host_uid: int,
    *,
    sandbox_id: str | None = None,
    volume: str | None = None,
) -> None:
    """Apply the E3.2 shared-volume permission model to the volume root.

    A volume is shared across sandboxes with different host uids, so the root
    must be world-readable/writable for every mounting sandbox (S1.2:
    single-entry userns has no supplementary groups, ``0770`` + common group
    is impossible). ``1777`` (sticky) additionally prevents a sandbox from
    deleting files it does not own. The root owner is set to the uid of the
    first mounting sandbox ("creator"), unless it is already owned by a
    non-root identity — that owner is never stolen. Best-effort: a volume on
    a filesystem that refuses the change degrades to whatever the platform
    allows, with a warning.

    The root is reachable only if every directory above it is too: the
    mediator opens this path as the mounting sandbox's uid, so a 0700 ancestor
    turns an absolute volume path into EACCES (A5). ``_ensure_traversable``
    widens that chain to o+x, leaving the per-sandbox slices at ``0770``
    (fix round 1 / c1: sandbox uid owns, worker gid is the group).

    Order matters on a non-root worker: the handover below is done through the
    ``e2b-maint`` broker, and once it has run the worker is neither the owner
    nor in possession of ``CAP_FOWNER``, so a *trailing* chmod is EPERM — the
    deployed stack logged exactly that warning once per volume per mount
    (measured 2026-09-12; the c1 workspace fix has the same rule). The chmod
    therefore comes first, is skipped when the mode is already what we want
    (the control-plane API creates every volume root as ``0o1777``), and only
    a wrong *end* state is worth a warning.
    """
    try:
        st = volume_root.stat()
    except OSError as exc:
        logger.warning(
            "cannot stat volume root %s for shared perms: %s", volume_root, exc
        )
        return
    _ensure_traversable(volume_root)
    wanted = 0o1777
    try:
        if stat.S_IMODE(st.st_mode) != wanted:
            os.chmod(volume_root, wanted)
    except OSError as exc:
        # Best effort; the end-state check below decides whether to warn.
        logger.debug(
            "cannot chmod volume root %s to %o: %s", volume_root, wanted, exc
        )
    try:
        if _volume_root_needs_handover(st):
            _chown_path(
                volume_root,
                host_uid,
                sandbox_id=sandbox_id,
                volume=volume,
                volume_root=True,
            )
    except OSError as exc:
        logger.warning(
            "cannot hand volume root %s to uid %s: %s",
            volume_root,
            host_uid,
            exc,
        )
    except AgentFileOpsError as exc:
        # C3 Task 4 review, N3: the agent shape raises a ``RuntimeError`` (an
        # ``AgentFileOpsError``), which the ``OSError`` arm above does not
        # catch -- so a *documented best-effort* step would have aborted
        # ``build_volume_mounts`` and therefore the sandbox create. Best-effort
        # is kept, deliberately and for the same reason the OS arm keeps it:
        # only a *root-owned* root is moved (a legacy-layout migration), and the
        # sandbox's own mount view is its ``0770`` slice, which is handed over
        # separately and still fails closed. So a refusal here degrades the
        # shared root, not the mount -- named, never silent.
        logger.warning(
            "cannot hand volume root %s to uid %s through the agent: %s",
            volume_root,
            host_uid,
            exc,
        )
    try:
        end_mode = stat.S_IMODE(volume_root.stat().st_mode)
    except OSError:
        return
    if end_mode != wanted:
        logger.warning(
            "volume root %s is mode %o, expected %o: mounting sandboxes may "
            "not be able to share it",
            volume_root,
            end_mode,
            wanted,
        )


def _chown_path(
    path: Path,
    host_uid: int,
    *,
    sandbox_id: str | None = None,
    volume: str | None = None,
    volume_root: bool = False,
) -> None:
    """chown one directory to a sandbox uid (broker-first on non-root workers).

    A non-root worker has no CAP_CHOWN of its own; the maintenance broker is
    what makes the E3.2 ownership model hold there. Outside the broker's
    whitelist (or with no brokers) this falls back to the in-process call.

    The group is the worker's effective gid (fix round 1 / c1): the worker is
    the data-plane owner of the tree it manages.

    C3 Task 4: in the agent shape the step is the agent's
    (``chown-volume-root`` / ``chown-volume-slice``), asked for as
    ``{sandbox_id, op, volume}`` -- the control plane resolves the volume name
    against its own registry, which is the only place that knows the host path.
    """
    from envd_service import agent_fileops, priv_helpers

    client = agent_fileops.active()
    if client is not None:
        if sandbox_id is None or volume is None:
            raise priv_helpers.PrivHelperError(
                "the C3 agent shape needs the sandbox id and the volume name "
                f"to hand {path} over: one of them was not named by the caller"
            )
        if volume_root:
            client.chown_volume_root(sandbox_id, volume)
        else:
            client.chown_volume_slice(sandbox_id, volume)
        return
    if priv_helpers.helpers_cover(path):
        priv_helpers.broker_chown(
            host_uid, path, recursive=False, gid=os.getegid()
        )
        return
    os.chown(path, host_uid, os.getegid())


def _can_manage_sandbox_uid() -> bool:
    """Whether this worker can put a path under a sandbox's own host uid.

    ``file_steps_available`` and not ``active_helpers``: C3's agent shape
    installs no ``PrivHelpers`` -- its chowns travel to the agent -- so the
    broker-only predicate answered "no" there and the whole volume ownership
    model (the shared ``1777`` root and the slice's ``0770``) was skipped
    silently (review Task 4 slice A, Important 2).
    """
    from envd_service import priv_helpers

    return os.geteuid() == 0 or priv_helpers.file_steps_available()


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
    host_uid: int | None = None,
) -> tuple[Path, int | None]:
    """Return the sandbox's mount view plus its volume projid (or None).

    ``per_sandbox_quota_mb <= 0`` -> ``(volume_path, None)`` (unchanged
    mount). Otherwise the view is ``volume_path/<sandbox_id>/`` with its own
    project + hard limit. Unsupported filesystems and provisioning failures
    degrade to the volume root with a warning, matching the workspace quota
    degradation contract.

    ``host_uid`` (the sandbox's allocated host uid, E3.2): the volume root is
    made ``1777`` for cross-sandbox sharing and, when a per-sandbox slice
    exists, the slice is ``0770 <host_uid>:<worker gid>`` (fix round 1 / c1)
    so the slice is kernel-isolated from other sandboxes while the worker --
    the data-plane owner -- can still reach it. ``None`` keeps the legacy
    single-uid ownership model (worker identity).
    """
    volume_root = Path(volume_path)
    if host_uid is not None and _can_manage_sandbox_uid():
        _ensure_shared_volume_root(
            volume_root, host_uid, sandbox_id=sandbox_id, volume=volume_id
        )
    if per_sandbox_quota_mb <= 0:
        return volume_root, None
    fs_mount = _volume_fs_mount(volume_root, fallback_mount_point)
    supported, _reason = xfs_project_supported(fs_mount, via_agent=via_agent)
    if not supported:
        return volume_root, None
    sandbox_dir = volume_root / sandbox_id
    try:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        if host_uid is not None and _can_manage_sandbox_uid():
            from envd_service import priv_helpers

            mode = priv_helpers.WORKSPACE_MODE
            group = os.getegid()
            from envd_service import agent_fileops

            agent_client = agent_fileops.active()
            if agent_client is not None:
                # C3 Task 4: mode first, while the worker still owns the slice
                # (after the hand-over it would be EPERM), then the agent's
                # chown -- asked for as ``{sandbox_id, op, volume}``.
                try:
                    os.chmod(sandbox_dir, mode)
                except OSError as exc:
                    logger.debug(
                        "cannot set volume slice %s to %04o before chown: %s",
                        sandbox_dir,
                        mode,
                        exc,
                    )
                agent_client.chown_volume_slice(
                    sandbox_id, volume_id, recursive=True
                )
            elif priv_helpers.helpers_cover(sandbox_dir):
                # chmod *before* chown: after the chown the worker is no longer
                # the owner and chmod would be EPERM (the broker carries no
                # CAP_FOWNER); the root shape hides the ordering, the non-root
                # one does not.
                try:
                    os.chmod(sandbox_dir, mode)
                except OSError as exc:
                    logger.debug(
                        "cannot set volume slice %s to %04o before chown: %s",
                        sandbox_dir,
                        mode,
                        exc,
                    )
                priv_helpers.broker_chown(
                    host_uid, sandbox_dir, recursive=True, gid=group
                )
            else:
                try:
                    os.chmod(sandbox_dir, mode)
                except OSError as exc:
                    logger.debug(
                        "cannot set volume slice %s to %04o before chown: %s",
                        sandbox_dir,
                        mode,
                        exc,
                    )
                os.chown(sandbox_dir, host_uid, group)
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
    host_uid: int | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Materialize the sandbox-visible volume mounts and their quota state.

    Input entries mirror the agent payload: ``{"name": volume_id, "path":
    <mount path>, "hostPath": <volume root>, "perSandboxQuotaMb": int}``.
    Returns ``(mount_paths, volume_projects)`` where ``mount_paths`` entries
    are ``{"path", "hostPath"}`` with ``hostPath`` pointing at the sandbox's
    view, and ``volume_projects`` entries carry the quota lifecycle metadata.
    ``host_uid`` is passed through to the mount provisioning for the E3.2
    volume permission model (shared 1777 root + per-uid slices).

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
            host_uid=host_uid,
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
        # ``perSandboxQuotaMb`` rides along so a reader (the single-file
        # RLIMIT_FSIZE ceiling, N28/C) can see that a mount may legitimately
        # hold a file larger than the sandbox tree's own budget. 0 = the mount
        # is unlimited.
        mount_paths.append(
            {
                "path": rel,
                "hostPath": str(view),
                "perSandboxQuotaMb": quota_raw,
            }
        )
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
        volume_id = entry.get("volume_id")
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
        # Fix round 1 / c1: the slice is `0770 <sandbox uid>:<worker gid>`, so
        # the worker's own group access can remove it in-process; e2b-maint is
        # the fallback for trees that access cannot reach.
        from envd_service import agent_fileops, priv_helpers

        agent_client = agent_fileops.active()
        if agent_client is not None:
            # C3 Task 4: the agent removes the slice (``remove-volume-slice``);
            # the volume name is what the control plane resolves against its own
            # registry, and this call site has it from the mount payload.
            if not isinstance(volume_id, str) or not volume_id:
                # No name, no derivation: the control plane may not be handed a
                # path instead (hard rule 3), so this is a refusal by name
                # rather than a privileged removal this worker cannot do.
                raise priv_helpers.PrivHelperError(
                    f"the C3 agent shape cannot remove the slice of sandbox "
                    f"{sandbox_id}: its volume name is not in the record "
                    f"({entry!r})"
                )
            agent_client.remove_volume_slice(sandbox_id, volume_id)
        else:
            priv_helpers.remove_tree(sandbox_dir)
        if isinstance(projid, int) and projid > 0 and not sandbox_dir.exists():
            # N12, same shape as the workspace project: with the slice gone the
            # accounting is zero, so resetting the limits is what drops the row
            # now rather than at the next reconciliation. Guarded on the slice
            # actually being gone -- resetting first would leave a live volume
            # unbounded.
            try:
                clear_project_limits(
                    mount_point=fs_mount, projid=projid, via_agent=via_agent
                )
            except ProjectQuotaError as exc:
                logger.warning(
                    "volume project row cleanup failed for %s (projid %s): %s",
                    sandbox_dir,
                    projid,
                    exc,
                )
