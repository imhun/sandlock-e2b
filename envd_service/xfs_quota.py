"""XFS project quota capability detection and project management (E2.1/E2.2).

Two quota domains:

- local (``via_agent=False``): inspect the worker's own mount of
  ``mount_point``, and run ``xfs_quota`` directly on the worker. This is the
  dev/legacy form: ``xfs_quota -x`` is gated on effective CAP_SYS_ADMIN, so
  it only works for a root worker (or a worker that keeps SYS_ADMIN) on a
  local XFS mount with prjquota.
- quota-agent (``via_agent=True``): the deployment's supported quota source
  (E2.6, promoted to the production form in A6). The worker asks
  quota-agent for the server-side facts and for every project operation, so
  *no path on the agent form ever executes ``xfs_quota`` (or any other local
  quota tool) on the worker* -- the agent owns the privilege. The worker
  only needs ``E2B_QUOTA_AGENT_URL`` (see :class:`envd_service.config.Settings`);
  an unreachable or rejecting agent raises :class:`ProjectQuotaError` and the
  callers degrade (sandbox/volume mount succeeds, no per-sandbox limit, one
  WARNING).

Detection (E2.1) is strictly read-only: it never mounts, never enables
quotas and never writes files. Any unsupported result is logged as a warning
so callers can degrade (skip quota, sandbox still created).

Project management (E2.2):

- projid allocation: deterministic SHA-256 hash of the sandbox id into
  ``1..2^31``, linear-probed against the projects defined in the XFS project
  table (``report -p``). The stable hash survives worker restarts and needs
  no cross-worker coordination on shared storage; the probe avoids reusing a
  projid while another sandbox still uses it.
- create: ``project -s -p <dir> <projid>`` + ``limit -p bhard=<disk_mb>M``.
  On partial failure (project created, limit failed) the project state is
  best-effort cleared before raising so callers degrade with a warning.
- delete: ``project -C -p <dir> <projid>``; the caller removes the directory
  afterwards, which releases the quota accounting automatically.
- orphan reconciliation (E2.4): compare the project table (``report -p``)
  against the ``project_id`` persisted in every ``sandbox.json``; project ids
  no record references are orphaned and removed — directory project state is
  cleared when the directory carrying the id can be found (files are never
  deleted) and the block limits are reset to 0, which makes XFS drop the
  record as the accounting settles. The carrier search runs in two stages
  (review R1/R2): the shape rule's own trees, then the top-level directories
  the shape rule leaves out plus the parked ones — a *disk fact*, so a
  leftover whose name or lost record hides it from the first stage is still
  released, and a row no directory carries at all is reset instead of being
  skipped forever.
- monitoring (E2.4): ``project_quota_table`` returns used/soft/hard blocks per
  project so callers can detect over-limit and near-limit sandboxes.

The agent branch (E2.6) is the deployment's supported source: ``agent_ops``
maps op names to callables — ``provision(sandbox_id, project_dir, disk_mb,
mount_point,
project_id=None) -> int``, ``release(project_dir, projid, mount_point)
-> None``, ``report(mount_point) -> {"projects": {...}}`` and
``reconcile(workspace_base, mount_point) -> {"cleaned": [...], "skipped":
[...]}`` — installed via :func:`configure_agent_ops`. Unconfigured agent ops
raise :class:`ProjectQuotaError` so callers degrade.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from envd_service import xfs_quotactl
from gateway_common.paths import (
    UNTRUSTED_TREE_DIR,
    is_reserved_platform_namespace,
    is_sandbox_workspace_dir,
)

logger = logging.getLogger(__name__)

#: Backend selector: ``auto`` (fd backend when the xfs_quota tool cannot reach
#: the filesystem, e.g. a quota-agent container that only bind-mounts it),
#: ``subprocess`` (always ``xfs_quota -x``) or ``quotactl`` (always the
#: device-free fd backend).
XFS_QUOTA_BACKEND_ENV = "E2B_XFS_QUOTA_BACKEND"
#: Backend verdicts, keyed by mount. Only ``quotactl`` -- "this mount can be
#: asked through the fd backend" -- is ever remembered, because that is a
#: property of the mounted filesystem and holds for the life of the process.
#: A *failed* probe is deliberately not cached: its causes are transient (a
#: mount the container has not finished setting up, a mount point this
#: process cannot open at that instant) and a remembered "no" pins the whole
#: mount to the subprocess/lsattr fallback until the process restarts. The
#: probe costs one ``open`` plus one ioctl, so re-asking is cheap and the
#: recovery is bounded by the next call (follow-up 2).
_BACKEND_CACHE: dict[str, str] = {}
_BACKEND_LOCK = threading.Lock()

#: Read-backend cache, deliberately separate from ``_BACKEND_CACHE``: "can
#: this mount *administer* project quotas" and "can this mount be asked what
#: project id a directory carries" are different questions with different
#: answers (follow-up 1). Same caching rule as ``_BACKEND_CACHE``: only the
#: positive verdict is remembered, so a transient probe failure cannot pin a
#: mount to ``lsattr`` for the life of the process (follow-up 2).
_PROJID_READ_CACHE: dict[str, str] = {}


def _configured_backend() -> str:
    value = (os.getenv(XFS_QUOTA_BACKEND_ENV) or "auto").strip().lower()
    return value if value in ("auto", "subprocess", "quotactl") else "auto"


def _quotactl_available(mount_point: str | Path) -> bool:
    """``xfs_quotactl.available`` with "cannot even ask" resolved to False.

    The probe loads ``libc.so.6``, which does not exist on a non-glibc host
    (macOS, musl), and that is the same *answer* for this caller as "this mount
    has no fd backend": fall back to the ``xfs_quota`` subprocess path, which
    reports its own, clearer failure when that is missing too
    (``ProjectQuotaError: xfs_quota '…' failed: …``, which every caller of a
    quota operation already degrades on with a warning). Letting the load error
    escape instead made a plain sandbox *delete* raise an untyped ``OSError``
    out of a backend-selection probe -- reachable whenever a record carried a
    project id on such a host.

    ``xfs_quotactl.available`` keeps raising on purpose (a capability question
    and a diagnosability question are not the same one); the fold happens here,
    at the only place that needs a binary choice.
    """
    try:
        return xfs_quotactl.available(mount_point)
    except OSError:
        return False


def _use_quotactl(mount_point: str | Path) -> bool:
    """Whether this mount's quota operations go through the fd backend.

    ``auto`` prefers the fd backend whenever the kernel allows it on this
    mount: that is the shape where ``xfs_quota -x`` cannot reach the
    filesystem at all (a container that only bind-mounts it), and where the
    capability is available it is equivalent for our operations. A host/root
    shape whose kernel says no keeps the historical subprocess path, so no
    existing command sequence changes.
    """
    mode = _configured_backend()
    if mode == "quotactl":
        return True
    if mode == "subprocess":
        return False
    key = str(mount_point)
    with _BACKEND_LOCK:
        cached = _BACKEND_CACHE.get(key)
    if cached is not None:
        return cached == "quotactl"
    choice = (
        "quotactl" if _quotactl_available(mount_point) else "subprocess"
    )
    if choice == "quotactl":
        logger.info(
            "using the device-free quotactl_fd backend for %s (xfs_quota "
            "cannot administer this mount without its device)",
            mount_point,
        )
        with _BACKEND_LOCK:
            _BACKEND_CACHE[key] = choice
    return choice == "quotactl"


def _use_quotactl_read(mount_point: str | Path) -> bool:
    """Whether a project id is read through the fd backend on this mount.

    Two deliberate differences from :func:`_use_quotactl`, which answers
    whether project quotas can be *administered* and stays the gate for the
    operations that need it (``set_limit``, ``release``, the orphan scan):

    * the question here is only whether ``FS_IOC_FSGETXATTR`` works, so a
      mount without ``prjquota`` still reads its directories' project ids
      instead of degrading to ``lsattr`` (follow-up 1, RED-2);
    * the argument is the *mount point*, never the directory being read. The
      probe opens what it is given, and asking it about a slice the control
      plane already deleted turns "this slice is gone" into "this backend
      does not work" -- the shape behind the 12 production WARNINGs. Callers
      go through :func:`_use_quotactl_for_read`, which adds the directory
      back as the fallback question and never caches a failure.
    """
    mode = _configured_backend()
    if mode == "quotactl":
        return True
    if mode == "subprocess":
        return False
    key = str(mount_point)
    with _BACKEND_LOCK:
        cached = _PROJID_READ_CACHE.get(key)
    if cached is not None:
        return cached == "quotactl"
    choice = "quotactl" if xfs_quotactl.can_read_projid(key) else "subprocess"
    if choice == "quotactl":
        with _BACKEND_LOCK:
            _PROJID_READ_CACHE[key] = choice
    return choice == "quotactl"


def _projid_read_mount(directory: Path) -> Path:
    """The mount a project-id read is gated on -- never ``directory`` itself.

    ``containing_mount_point`` answers from ``/proc/mounts``, so it still
    resolves for a directory that no longer exists; a host without
    ``/proc/mounts`` (a macOS dev box) has no fd backend to key either, so
    the directory itself is the harmless last resort.
    """
    try:
        mount = containing_mount_point(directory)
    except OSError:  # pragma: no cover - defensive: unresolvable path
        mount = None
    return Path(mount) if mount else directory


def _use_quotactl_for_read(directory: Path) -> bool:
    """Whether *this directory's* project id is read through the fd backend.

    The containing mount answers first and that answer is cached per mount:
    it is the cheap representative of the capability the read needs, and
    keying it on the mount is what keeps one probe from being spent per tree.

    A mount that cannot answer must not condemn the read. The fd read opens
    *the directory* (``FS_IOC_FSGETXATTR`` on its own fd) and the mount probe
    opens the mount point, so a workspace base that sits under a mount root
    this worker may not open -- ``/var/lib`` at ``0711`` with
    ``/var/lib/e2b-sandboxes`` readable inside it -- has a readable directory
    that the mount-level question would have called unreadable. When the
    mount says no, the directory gets the last word.

    The directory-level verdict is never cached: it says nothing about the
    next directory, and remembering a "no" here is the same stickiness this
    follow-up removes from the mount-level cache.
    """
    mount = _projid_read_mount(directory)
    if _use_quotactl_read(mount):
        return True
    if mount == directory:
        return False
    return xfs_quotactl.can_read_projid(directory)


#: Non-root workers (E5.1) without effective CAP_SYS_ADMIN cannot run
#: ``xfs_quota -x`` directly: the kernel gates quota administration on
#: CAP_SYS_ADMIN (not on euid), and every call fails with EPERM, which
#: callers would silently skip. Detection reports this exact guidance so
#: the degraded per-sandbox disk-hard-limit control is disclosed at startup
#: instead of failing per sandbox. The guard only applies on XFS mounts
#: (non-XFS hosts keep their real filesystem reason, E5.1 review Minor-13),
#: and non-root with effective SYS_ADMIN (k8s runAsUser + SYS_ADMIN) keeps
#: the direct path (E5.1 review Important-2).
NONROOT_DIRECT_QUOTA_REASON = (
    "磁盘配额不可用：非 root 需启用 agent 形态"
    "（E2B_QUOTA_AGENT_URL 指向 quota-agent）"
)

#: E2.6 wires the quota-agent client here. Contract:
#: ``agent_query(mount_point: str) -> dict[str, Any]`` returning server-side
#: facts — ``fs_type``, ``projid32bit``, ``prjquota``, ``xfs_quota`` — or an
#: ``{"error": reason}`` dict when the server cannot answer.
agent_query: Callable[[str], dict[str, Any]] | None = None

#: E2.6 wires quota-agent project operations here. Contract:
#: ``agent_ops["provision"](sandbox_id, project_dir, disk_mb, mount_point,
#: project_id=None) -> int`` and ``agent_ops["release"](project_dir, projid,
#: mount_point) -> None``.
agent_ops: dict[str, Callable[..., Any]] | None = None

_XFS_INFO_TIMEOUT_SECONDS = 5
_XFS_QUOTA_TIMEOUT_SECONDS = 10
_PASS: tuple[bool, str] = (True, "")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
_PROJID32BIT = re.compile(r"\bprojid32bit=([01])\b")
_PROJECT_ID_LINE = re.compile(r"^\s*#?(\d+)(?:\s|$)", re.MULTILINE)
#: ``report -p`` rows: ``#<id> <used> <soft> <hard> <warn/grace> ...`` (1 KiB
#: blocks). Soft/hard 0 means "no limit".
_PROJECT_USAGE_LINE = re.compile(
    r"^\s*#?(\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s|$)", re.MULTILINE
)
#: ``lsattr -p -d`` rows: ``<projid> <attributes> <path>``.
_LSATTR_PROJID_LINE = re.compile(r"^\s*(\d+)\s+([A-Za-z_+-]+)\s+(\S.*)$")

#: Project id range for sandboxes (design doc §3.2: 1..2^31).
_PROJID_MIN = 1
_PROJID_MAX = 1 << 31


class ProjectQuotaError(RuntimeError):
    """A quota management operation failed; callers degrade with a warning."""


class ProjectDirectoryGone(ProjectQuotaError):
    """The directory is not on disk any more, so there is nothing to verify.

    The expected shape after the control plane deleted a volume:
    ``DELETE /volumes/<id>`` removes the volume root and the per-sandbox
    slices inside it, so a teardown that runs afterwards has no object to
    read and no project state it may release. Callers keep the fail-safe
    semantics (no release without a verified ``(directory, projid)`` pair)
    and leave the row to the reconcile, but must not report this as an
    anomaly -- it is what the ordering is supposed to look like from here.
    """


class ProjectDirectoryUnreadable(ProjectQuotaError):
    """The directory exists but this process may not read it (EACCES/EPERM)."""


@dataclass(frozen=True)
class ProjectQuotaUsage:
    """Usage snapshot of one project id from ``xfs_quota report -p``.

    Blocks are 1 KiB XFS blocks (``report -p`` default units); soft/hard of 0
    means "no limit" (unlimited).
    """

    projid: int
    used_blocks: int
    soft_blocks: int
    hard_blocks: int


def _hash_projid(sandbox_id: str) -> int:
    """Map a sandbox id to a stable projid in ``[_PROJID_MIN, _PROJID_MAX]``."""
    digest = hashlib.sha256(sandbox_id.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    return _PROJID_MIN + value % (_PROJID_MAX - _PROJID_MIN + 1)


def _parse_project_report(output: str) -> set[int]:
    """Extract the defined project ids from ``xfs_quota report -p`` output."""
    return {int(match.group(1)) for match in _PROJECT_ID_LINE.finditer(output)}


def _parse_project_usage(output: str) -> dict[int, ProjectQuotaUsage]:
    """Parse ``report -p`` rows into projid -> used/soft/hard block counts."""
    rows: dict[int, ProjectQuotaUsage] = {}
    for match in _PROJECT_USAGE_LINE.finditer(output):
        projid, used, soft, hard = (int(group) for group in match.groups())
        rows[projid] = ProjectQuotaUsage(
            projid=projid,
            used_blocks=used,
            soft_blocks=soft,
            hard_blocks=hard,
        )
    return rows


def _local_run_xfs_quota(mount_point: str | Path, command: str) -> str:
    """Run one ``xfs_quota -x -c`` command; return stdout or raise."""
    argv = ["xfs_quota", "-x", "-c", command, str(mount_point)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_XFS_QUOTA_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectQuotaError(f"xfs_quota '{command}' failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (
            proc.stderr.strip()
            or proc.stdout.strip()
            or f"exit code {proc.returncode}"
        )
        raise ProjectQuotaError(f"xfs_quota '{command}' failed: {detail}")
    return proc.stdout


def _local_in_use_projids(mount_point: str | Path) -> set[int]:
    """Return the project ids currently defined in the XFS project table."""
    if _use_quotactl(mount_point):
        return set(xfs_quotactl.project_table(mount_point))
    output = _local_run_xfs_quota(mount_point, "report -p")
    return _parse_project_report(output)


def _probe_free_projid(sandbox_id: str, in_use: set[int]) -> int:
    """Pick a free projid: stable sandbox-id hash, linear-probed on conflict.

    Shared by the local allocator (probe against the local ``report -p``
    output) and the quota-agent client (probe against the server-side
    project table fetched over HTTP, E2.6).
    """
    candidate = _hash_projid(sandbox_id)
    while candidate in in_use:
        candidate = _PROJID_MIN if candidate >= _PROJID_MAX else candidate + 1
    return candidate


def project_quota_table(
    mount_point: str | Path, via_agent: bool = False
) -> dict[int, ProjectQuotaUsage]:
    """Return the full project quota table: projid -> used/soft/hard blocks.

    Local (``via_agent=False``): runs ``xfs_quota -x -c "report -p"`` on the
    worker. NFS form (``via_agent=True``): asks quota-agent for the
    server-side report (E2.6 contract: ``agent_ops["report"](mount_point) ->
    {"projects": {projid: {"used_blocks", "soft_blocks", "hard_blocks"}}}``).
    """
    if via_agent:
        data = _agent_call("report", mount_point=str(mount_point))
        if not isinstance(data, dict) or "projects" not in data:
            raise ProjectQuotaError(
                f"quota-agent report returned invalid data: {data!r}"
            )
        try:
            return {
                int(projid): ProjectQuotaUsage(
                    projid=int(projid),
                    used_blocks=int(row["used_blocks"]),
                    soft_blocks=int(row["soft_blocks"]),
                    hard_blocks=int(row["hard_blocks"]),
                )
                for projid, row in data["projects"].items()
            }
        except (TypeError, ValueError, KeyError) as exc:
            raise ProjectQuotaError(
                f"quota-agent report returned invalid rows: {exc}"
            ) from exc
    if _use_quotactl(mount_point):
        return {
            projid: ProjectQuotaUsage(
                projid=projid,
                used_blocks=used,
                soft_blocks=soft,
                hard_blocks=hard,
            )
            for projid, (used, soft, hard) in xfs_quotactl.project_table(
                mount_point
            ).items()
        }
    output = _local_run_xfs_quota(mount_point, "report -p")
    return _parse_project_usage(output)


def allocate_project_id(sandbox_id: str, mount_point: str | Path) -> int:
    """Pick a free projid: stable sandbox-id hash, linear-probed on conflict."""
    return _probe_free_projid(sandbox_id, _local_in_use_projids(mount_point))


def _agent_call(op: str, **kwargs) -> Any:
    """Invoke a quota-agent project op, wrapping failures as ProjectQuotaError."""
    ops = agent_ops
    if ops is None:
        raise ProjectQuotaError("quota-agent not configured (E2.6)")
    fn = ops.get(op)
    if fn is None:
        raise ProjectQuotaError(f"quota-agent op '{op}' not configured (E2.6)")
    try:
        return fn(**kwargs)
    except Exception as exc:
        raise ProjectQuotaError(f"quota-agent {op} failed: {exc}") from exc


def configure_agent_ops(
    ops: dict[str, Callable[..., Any]] | None,
) -> None:
    """Wire quota-agent project operations; E2.6 replaces the default None."""
    global agent_ops
    agent_ops = ops


def provision_project(
    *,
    sandbox_id: str,
    project_dir: str | Path,
    mount_point: str | Path,
    disk_mb: int,
    via_agent: bool = False,
    project_id: int | None = None,
) -> int:
    """Assign projid + hard quota to ``project_dir`` and return the projid.

    With ``project_id`` set (existing sandbox re-provision, e.g. migration
    rollback) the id is reused instead of allocating a new one. On partial
    failure the project state is best-effort cleared before raising, so the
    caller can degrade (skip quota, keep the sandbox).
    """
    if via_agent:
        return _agent_call(
            "provision",
            sandbox_id=sandbox_id,
            project_dir=str(project_dir),
            mount_point=str(mount_point),
            disk_mb=disk_mb,
            project_id=project_id,
        )
    projid = (
        project_id
        if project_id is not None
        else allocate_project_id(sandbox_id, mount_point)
    )
    if _use_quotactl(mount_point):
        # Device-free backend: tag the directory (children inherit through
        # XFS_XFLAG_PROJINHERIT) and set the block limit through quotactl_fd.
        try:
            xfs_quotactl.assign_projid(project_dir, projid)
        except xfs_quotactl.QuotactlError as exc:
            raise ProjectQuotaError(
                f"project setup failed for {sandbox_id}: {exc}"
            ) from exc
        try:
            xfs_quotactl.set_limit(mount_point, projid, disk_mb)
        except xfs_quotactl.QuotactlError as exc:
            try:
                xfs_quotactl.clear_projid(project_dir)
            except xfs_quotactl.QuotactlError as cleanup_exc:
                logger.warning(
                    "project cleanup failed for %s (projid %s): %s",
                    sandbox_id,
                    projid,
                    cleanup_exc,
                )
            raise ProjectQuotaError(
                f"quota limit setup failed for {sandbox_id}: {exc}"
            ) from exc
        return projid
    quoted_dir = shlex.quote(str(project_dir))
    setup = f"project -s -p {quoted_dir} {projid}"
    try:
        _local_run_xfs_quota(mount_point, setup)
    except ProjectQuotaError as exc:
        raise ProjectQuotaError(
            f"project setup failed for {sandbox_id}: {exc}"
        ) from exc
    limit = f"limit -p bhard={disk_mb}M {projid}"
    try:
        _local_run_xfs_quota(mount_point, limit)
    except ProjectQuotaError as exc:
        clear = f"project -C -p {quoted_dir} {projid}"
        try:
            _local_run_xfs_quota(mount_point, clear)
        except ProjectQuotaError as cleanup_exc:
            logger.warning(
                "project cleanup failed for %s (projid %s): %s",
                sandbox_id,
                projid,
                cleanup_exc,
            )
        raise ProjectQuotaError(
            f"quota limit setup failed for {sandbox_id}: {exc}"
        ) from exc
    return projid


def release_project(
    *,
    project_dir: str | Path,
    mount_point: str | Path,
    projid: int,
    via_agent: bool = False,
) -> None:
    """Clear the project state on ``project_dir``; the caller removes the dir.

    Raises :class:`ProjectQuotaError` on failure; callers degrade with a
    warning (sandbox deletion still proceeds).
    """
    if via_agent:
        _agent_call(
            "release",
            project_dir=str(project_dir),
            mount_point=str(mount_point),
            projid=projid,
        )
        return
    if _use_quotactl(mount_point):
        try:
            xfs_quotactl.clear_projid(project_dir)
        except xfs_quotactl.QuotactlError as exc:
            raise ProjectQuotaError(
                f"project release failed for {project_dir}: {exc}"
            ) from exc
        return
    command = f"project -C -p {shlex.quote(str(project_dir))} {projid}"
    _local_run_xfs_quota(mount_point, command)


def _recorded_projids(workspace_base: str | Path) -> set[int]:
    """Project ids referenced by any ``sandbox.json`` under workspace_base.

    Both the workspace project (``project_id``) and every per-sandbox volume
    project (``volume_projects[].projid``, E2.5) are referenced, so the E2.4
    reconciliation never treats a live volume quota as an orphan.

    Raises :class:`ProjectQuotaError` when ``workspace_base`` is missing or
    unreadable: reconciliation must never read an unreadable base as "no
    recorded projects" and wipe live quotas (E2.6 review: fail-closed).
    """
    base = Path(workspace_base)
    recorded: set[int] = set()
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        raise ProjectQuotaError(
            f"reconcile workspace_base missing or unreadable: "
            f"{base} ({type(exc).__name__})"
        ) from exc
    for entry in entries:
        record_path = entry / "sandbox.json"
        if not record_path.is_file():
            continue
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        recorded |= projids_in_record(payload)
    return recorded


def projids_in_record(payload: dict[str, Any]) -> set[int]:
    """Project ids one persisted sandbox record references.

    The workspace project (``project_id``) plus every per-sandbox volume
    project (``volume_projects[].projid``, E2.5). One rule, shared by the
    quota-side scan over ``sandbox.json`` and the worker-side teardown that
    has to know which rows its orphan-tree GC makes reclaimable.
    """
    projids: set[int] = set()
    projid = payload.get("project_id")
    if isinstance(projid, int) and projid > 0:
        projids.add(projid)
    volume_projects = payload.get("volume_projects")
    if isinstance(volume_projects, list):
        for item in volume_projects:
            if not isinstance(item, dict):
                continue
            projid = item.get("projid")
            if isinstance(projid, int) and projid > 0:
                projids.add(projid)
    return projids


def _read_top_level_project_ids(
    base: Path, candidates: list[Path]
) -> tuple[dict[int, Path], bool]:
    """Ask the disk which of ``candidates`` carry a project id.

    Returns ``(projid -> directory, asked)``. ``asked`` says whether the disk
    answered for *all* of them: a host with neither the fd backend nor
    ``lsattr``, an ``lsattr`` run that failed, and a single directory whose
    project id could not be read all leave it False.

    The distinction is what the fail-safe reconcile hangs its "no directory
    carries this row" conclusion on, so a read that could not happen must
    never look like the answer "no directory here carries a project id"
    (review R1/R2). It is also why a partial read is reported as unasked
    rather than trusted: the directory that failed could be the carrier.
    """
    mapping: dict[int, Path] = {}
    if not candidates:
        return mapping, True
    if _use_quotactl(base):
        asked = True
        for candidate in sorted(candidates):
            try:
                projid = xfs_quotactl.projid_of(candidate)
            except xfs_quotactl.QuotactlError as exc:
                logger.warning("cannot read project id of %s: %s", candidate, exc)
                asked = False
                continue
            if projid:
                mapping[projid] = candidate
        return mapping, asked
    argv = ["lsattr", "-p", "-d", *(str(c) for c in sorted(candidates))]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_XFS_QUOTA_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Without the mapping a non-empty orphan can only be reported, never
        # cleaned -- say so loudly, because a node missing e2fsprogs otherwise
        # degrades silently and orphan projects pile up forever.
        logger.warning(
            "cannot read project ids with lsattr (%s): orphan reconciliation "
            "will report non-empty projects instead of cleaning them",
            exc,
        )
        return mapping, False
    if proc.returncode != 0:
        logger.warning(
            "lsattr project scan failed for %s: %s", base, proc.stderr.strip()
        )
        return mapping, False
    for line in proc.stdout.splitlines():
        match = _LSATTR_PROJID_LINE.match(line)
        if match is None:
            continue
        mapping[int(match.group(1))] = Path(match.group(3))
    return mapping, True


def _candidate_sandbox_trees(workspace_base: Path) -> list[Path]:
    """Top-level directories the shape rule calls sandbox work trees."""
    try:
        return [
            entry
            for entry in workspace_base.iterdir()
            if is_sandbox_workspace_dir(entry)
        ]
    except OSError:
        return []


def _scan_project_dirs_with_status(
    workspace_base: str | Path,
) -> tuple[dict[int, Path], bool]:
    """:func:`_scan_project_dirs` plus whether the disk answered at all."""
    base = Path(workspace_base)
    return _read_top_level_project_ids(base, _candidate_sandbox_trees(base))


def _scan_project_dirs(workspace_base: str | Path) -> dict[int, Path]:
    """Map projid -> sandbox directory via ``lsattr -p -d``.

    Only top-level sandbox workspace directories are considered (the shared
    :func:`gateway_common.paths.is_sandbox_workspace_dir` predicate), so
    ``_snapshots`` / ``_migrate`` / ``_cow`` / ``_volumes`` / ``_templates``
    and foreign trees are never touched. Returns {} on any failure; callers
    skip rather than risk mis-identifying a directory.

    This is the shape stage of the fail-safe reconcile's carrier search. It is
    deliberately still driven by the predicate (its candidate set is pinned by
    the orphan-tree GC contracts) and is *not* the whole story: the directory
    that carries an orphan row need not be a sandbox-shaped tree, which is what
    :func:`_scan_leftover_project_dirs` answers for.
    """
    return _scan_project_dirs_with_status(workspace_base)[0]


def _scan_leftover_project_dirs(
    workspace_base: str | Path,
) -> tuple[dict[int, Path], bool]:
    """Map projid -> directory for the shapes the predicate leaves out.

    The second stage of the fail-safe reconcile's carrier search, and the
    reason a used orphan row can still be released: "this worker manages this
    directory" is a *disk fact* -- the directory really carries a project id --
    not a statement about its name or about the ``sandbox.json`` it may have
    lost (review R1/R2). Two families of candidates, and only these:

    * top-level directories the shape rule rejects (an infrastructure-prefixed
      name with no top-level record, a name that is not a sandbox id at all,
      ``_untrusted.trees`` itself) minus
      :data:`gateway_common.paths.RESERVED_PLATFORM_NAMESPACES`, which nothing
      ever assigns a project id to and which must never be an asset;
    * the quarantine's own children: a parked tree was a top-level tree of this
      worker until it was renamed, and a rename keeps its project id, so a row
      a failed release left behind is still this worker's row.

    The snapshot store (top-level ``snapshot.json`` + ``fs/``, no project id:
    ``SnapshotRegistry.create_from_sandbox`` copies the contents into a fresh
    directory) and the other platform namespaces therefore map to nothing here
    and are never released against. Returns ``(mapping, asked)`` like
    :func:`_read_top_level_project_ids`: an unreadable listing is reported as
    unasked, never as "nothing carries a project id".
    """
    base = Path(workspace_base)
    candidates: list[Path] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return {}, False
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        if is_sandbox_workspace_dir(entry):
            # Already covered by the shape stage; asking twice would double
            # the disk reads of a round for the common case.
            continue
        if is_reserved_platform_namespace(entry.name):
            continue
        candidates.append(entry)
    quarantine = base / UNTRUSTED_TREE_DIR
    try:
        parked = sorted(quarantine.iterdir())
    except FileNotFoundError:
        parked = []
    except OSError:
        return {}, False
    for child in parked:
        if child.is_symlink() or not child.is_dir():
            continue
        candidates.append(child)
    return _read_top_level_project_ids(base, candidates)


def _directory_read_error(directory: Path) -> OSError | None:
    """The error that stops this process from reading ``directory``, or None.

    Asked of the path itself, because neither backend reports the two shapes
    apart: the fd backend folds them into ``open``, and ``lsattr`` answers
    "cannot stat" for both a deleted directory and one it may not read.
    """
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        return exc
    os.close(fd)
    return None


def _path_state_error(directory: Path) -> ProjectQuotaError | None:
    """``directory`` is gone or unreadable: which class, or None if readable."""
    error = _directory_read_error(directory)
    if error is None:
        return None
    if error.errno in (errno.ENOENT, errno.ENOTDIR):
        return ProjectDirectoryGone(
            f"{directory} is gone from the disk ({error})"
        )
    if error.errno in (errno.EACCES, errno.EPERM):
        return ProjectDirectoryUnreadable(
            f"{directory} exists but this worker cannot read it ({error})"
        )
    return None


def _read_failure_class(directory: Path, reason: str) -> ProjectQuotaError:
    """Which of the three shapes a failed project-id read is (follow-up 1).

    ``reason`` is the mechanism's own explanation (the fd backend's error, or
    what ``lsattr`` did), used only for the "this host cannot ask the disk"
    case: the path's own state decides whether there was anything to ask
    about in the first place.
    """
    state_error = _path_state_error(directory)
    if state_error is not None:
        return state_error
    return ProjectQuotaError(
        f"cannot ask the disk for the project id of {directory} ({reason})"
    )


def directory_project_id(project_dir: str | Path) -> int | None:
    """The project id the *disk* reports for one directory (``None`` = none).

    The worker's own view of its project ids lives in ``sandbox.json``, which
    sits inside the sandbox-owned tree and can be replaced by the sandbox, so
    the destructive paths must read the truth from the filesystem instead:
    the same ``lsattr -p -d`` / fd-backend read :func:`_scan_project_dirs`
    uses for the orphan quota scan.

    The read is gated on the containing *mount* (``FS_IOC_FSGETXATTR``
    availability, not quota administration) and its failures are classified,
    because the three shapes need three different answers. The mount answers
    as the mount's representative directory, and a mount-level "no" is not
    final: the directory being read gets the last word, and no failure is
    cached, so a mount that is not ready yet is picked up on a later read
    instead of degrading every read until the process restarts:

    * :class:`ProjectDirectoryGone` -- the directory is not there any more
      (``ENOENT``). Expected when a volume deletion removed the slice first;
      the caller has nothing to verify and nothing to release.
    * :class:`ProjectDirectoryUnreadable` -- the directory is there and this
      process may not read it (``EACCES``/``EPERM``): a real anomaly.
    * :class:`ProjectQuotaError` -- the host cannot ask the disk at all (no
      fd backend and no ``lsattr``, e.g. an NFS-mounted workspace whose quota
      lives on the storage server): the caller must then leave the project
      state alone rather than trust the record's claim.

    Only the last shape may reach the ``lsattr`` fallback: asking it about a
    path that is not there reports "cannot stat" exactly like a permission
    problem, which is what made the 12 production WARNINGs unreadable.
    """
    directory = Path(project_dir)
    try:
        # The backend probe itself must never take the caller down: it only
        # decides between the fd read and ``lsattr``, and both report their
        # own failure below. A failed probe is not remembered, so a read that
        # lands while the mount is still coming up is retried by the next
        # caller instead of pinning this mount to the fallback.
        use_quotactl = _use_quotactl_for_read(directory)
    except Exception:  # pragma: no cover - defensive
        use_quotactl = False
    if use_quotactl:
        try:
            return xfs_quotactl.projid_of(directory) or None
        except xfs_quotactl.QuotactlError as exc:
            raise _read_failure_class(
                directory, f"the fd backend failed on {directory}: {exc}"
            ) from exc
    # Never hand a path that is not there (or that this process may not read)
    # to the fallback: ``lsattr`` reports both as "cannot stat", which is the
    # same line a genuine backend failure produces, and it would spend a
    # subprocess on an answer already known.
    unreachable = _path_state_error(directory)
    if unreachable is not None:
        raise unreachable
    argv = ["lsattr", "-p", "-d", str(directory)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_XFS_QUOTA_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _read_failure_class(
            directory, f"lsattr could not run for {directory}: {exc}"
        ) from exc
    if proc.returncode != 0:
        raise _read_failure_class(
            directory, f"lsattr failed for {directory}: {proc.stderr.strip()}"
        )
    for line in proc.stdout.splitlines():
        match = _LSATTR_PROJID_LINE.match(line)
        if match is None:
            continue
        return int(match.group(1)) or None
    return None


def cleanup_orphan_project(
    *,
    projid: int,
    mount_point: str | Path,
    project_dir: str | Path | None = None,
) -> None:
    """Remove an orphan project id from the quota table (local only).

    When ``project_dir`` still carries the project state, ``project -C``
    first returns its accounting to the default project; resetting the block
    limits to 0 (``limit -p bsoft=0 bhard=0``) then makes XFS drop the quota
    record once usage is zero (E2.4 verified behavior). The directory itself
    is never deleted: orphan cleanup only touches quota metadata, so user
    files stay untouched for a later record-driven decision.
    """
    if project_dir is not None:
        if _use_quotactl(mount_point):
            try:
                xfs_quotactl.clear_projid(project_dir)
            except xfs_quotactl.QuotactlError as exc:
                raise ProjectQuotaError(
                    f"project cleanup failed for {project_dir}: {exc}"
                ) from exc
            clear_project_limits(mount_point=mount_point, projid=projid)
            return
        command = f"project -C -p {shlex.quote(str(project_dir))} {projid}"
        _local_run_xfs_quota(mount_point, command)
    clear_project_limits(mount_point=mount_point, projid=projid)


def clear_project_limits(
    *,
    mount_point: str | Path,
    projid: int,
    via_agent: bool = False,
) -> None:
    """Reset a released project's block limits so XFS drops its row (N12).

    ``release_project`` clears the *directory's* project state (``project -C``);
    the accounting then follows the tree the caller removes. The **row** does not
    go with it: XFS keeps a project record while its limits are non-zero, so a
    deleted sandbox stayed visible in ``report -p`` as "0 used, hard_blocks=N"
    until something reset the limits -- and on a running worker the only thing
    that did was the startup orphan reconciliation. Measured: one create/delete
    burst left 40 rows, of which a `POST /reconcile` cleaned 36.

    Order matters and is the caller's: run this **after** the tree is gone. With
    the tree still on disk the row stays anyway (usage > 0) and a teardown that
    then fails would leave a live sandbox with no disk limit at all.
    """
    if via_agent:
        _agent_call(
            "clear_limits",
            mount_point=str(mount_point),
            projid=int(projid),
        )
        return
    if _use_quotactl(mount_point):
        xfs_quotactl.clear_limit(mount_point, projid)
        return
    _local_run_xfs_quota(mount_point, f"limit -p bsoft=0 bhard=0 {projid}")


def _local_reconcile(
    workspace_base: str | Path, mount_point: str | Path
) -> dict[str, Any]:
    """Local reconciliation: quota table entries no record references are
    orphaned and cleaned; recorded projids and project 0 are never touched.

    The directory an orphan row is released against comes from the disk, in
    two stages: the shape rule's own trees (:func:`_scan_project_dirs`) and,
    when that answers nothing for a used row, the top-level directories the
    shape rule leaves out plus the parked trees
    (:func:`_scan_leftover_project_dirs`). Both are disk reads, so an
    infrastructure-prefixed leftover whose record is gone -- a legal sandbox
    id, invisible to every shape-based scan -- is still found and released
    (review R1), while the snapshot store and the platform's other namespaces
    carry no project id and are never candidates.

    A used row that *no* directory carries and that no record references is
    not pinned by anything this worker can see: the cleanup runs without a
    directory (limits reset, no directory project state touched) and the row
    drops as the deferred accounting settles. That is the bounded end of the
    shape review R2 measured as "skipped forever", and it is only taken when
    the carrier search *answered*: a disk this host cannot ask leaves the row
    reported, never guessed at.
    """
    table = project_quota_table(mount_point)
    recorded = _recorded_projids(workspace_base)
    orphans = sorted(
        projid for projid in table if projid != 0 and projid not in recorded
    )
    cleaned: list[int] = []
    skipped: list[dict[str, Any]] = []
    dir_by_projid: dict[int, Path] | None = None
    leftover_by_projid: dict[int, Path] | None = None
    disk_answered = True
    for projid in orphans:
        usage = table[projid]
        project_dir: Path | None = None
        if usage.used_blocks > 0:
            if dir_by_projid is None:
                dir_by_projid, asked = _scan_project_dirs_with_status(
                    workspace_base
                )
                disk_answered = disk_answered and asked
            project_dir = dir_by_projid.get(projid)
            if project_dir is None:
                # The shape rule's own trees do not carry this row: it may
                # still be carried by a directory the shapes leave out (a
                # prefixed leftover, a parked tree), which is a disk question.
                if leftover_by_projid is None:
                    leftover_by_projid, asked = _scan_leftover_project_dirs(
                        workspace_base
                    )
                    disk_answered = disk_answered and asked
                project_dir = leftover_by_projid.get(projid)
            if project_dir is None and not disk_answered:
                # Not a fact: this host could not ask the disk which directory
                # carries the row, so it is reported rather than guessed at
                # (the same degradation ``_scan_project_dirs`` documents).
                skipped.append(
                    {
                        "projid": projid,
                        "reason": (
                            f"{usage.used_blocks} used blocks and this host "
                            "cannot ask the disk which directory carries them; "
                            "entry left for manual review"
                        ),
                    }
                )
                continue
        try:
            cleanup_orphan_project(
                projid=projid,
                mount_point=mount_point,
                project_dir=project_dir,
            )
        except ProjectQuotaError as exc:
            skipped.append({"projid": projid, "reason": str(exc)})
            logger.warning("orphan project %s cleanup failed: %s", projid, exc)
            continue
        cleaned.append(projid)
        if project_dir is None and usage.used_blocks > 0:
            # No record references this row and no directory on this worker
            # carries it: the block limits are what pinned it, so they are
            # reset and the entry goes with the accounting.
            logger.warning(
                "orphan project %s: %d used blocks and no directory on this "
                "worker carries it; reset its limits",
                projid,
                usage.used_blocks,
            )
        else:
            logger.info("cleaned orphan project %s", projid)
    return {"cleaned": cleaned, "skipped": skipped}


def reconcile_orphan_projects(
    *,
    workspace_base: str | Path,
    mount_point: str | Path,
    via_agent: bool = False,
) -> dict[str, Any]:
    """Reconcile the quota table against ``sandbox.json`` project ids.

    Every project id in ``report -p`` that no sandbox record references is an
    orphan and is removed from the quota table (project state cleared on the
    directory when present; zero-usage records dropped by resetting limits).
    Normal sandbox quotas are never touched and directories are never
    deleted.

    NFS form: ``via_agent=True`` delegates the server-side reconciliation to
    quota-agent (E2.6 contract: ``agent_ops["reconcile"](workspace_base,
    mount_point) -> {"cleaned": [projid], "skipped": [{"projid", "reason"}]}``).

    Returns ``{"cleaned": [projid], "skipped": [{"projid", "reason"}]}``.
    """
    if via_agent:
        data = _agent_call(
            "reconcile",
            workspace_base=str(workspace_base),
            mount_point=str(mount_point),
        )
        if not isinstance(data, dict):
            raise ProjectQuotaError(
                f"quota-agent reconcile returned invalid data: {data!r}"
            )
        return data
    return _local_reconcile(workspace_base, mount_point)


def _fail(reason: str) -> tuple[bool, str]:
    return (False, reason)


def _read_proc_mounts() -> str | None:
    """Return ``/proc/mounts`` text, or None when unavailable (e.g. macOS)."""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_proc_self_status() -> str | None:
    """Return ``/proc/self/status`` text, or None when unavailable."""
    try:
        with open("/proc/self/status", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _unescape_mount_path(value: str) -> str:
    """Decode octal escapes (\040 space, \011 tab, \134 backslash) in mount paths."""

    def _replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return _OCTAL_ESCAPE.sub(_replace, value)


def _find_mount(
    mounts_text: str, mount_point: str | Path
) -> tuple[str, frozenset[str]] | None:
    """Return (fs_type, mount_options) of the deepest mount containing mount_point."""
    target = str(Path(mount_point).resolve())
    best: tuple[int, str, frozenset[str]] | None = None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mount_path = _unescape_mount_path(parts[1])
        if mount_path != target and not target.startswith(
            mount_path.rstrip("/") + "/"
        ):
            continue
        candidate = (len(mount_path), parts[2], frozenset(parts[3].split(",")))
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None
    return best[1], best[2]


def containing_mount_point(path: str | Path) -> str | None:
    """Return the deepest mount point containing ``path``, or None.

    Used by the volume quota path (E2.5): a volume subdirectory may live on
    a different filesystem than the sandbox workspace, and ``xfs_quota``
    commands must target the filesystem's own mount point.
    """
    mounts_text = _read_proc_mounts()
    if mounts_text is None:
        return None
    target = str(Path(path).resolve())
    best: tuple[int, str] | None = None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        mount_path = _unescape_mount_path(parts[1])
        if mount_path != target and not target.startswith(
            mount_path.rstrip("/") + "/"
        ):
            continue
        if best is None or len(mount_path) > best[0]:
            best = (len(mount_path), mount_path)
    return best[1] if best is not None else None


def _run_xfs_info(mount_point: str | Path) -> str | None:
    """Run read-only ``xfs_info``; return stdout or None on any failure."""
    try:
        proc = subprocess.run(
            ["xfs_info", str(mount_point)],
            capture_output=True,
            text=True,
            timeout=_XFS_INFO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_projid32bit(xfs_info_output: str) -> bool | None:
    """Return True/False for ``projid32bit=1/0``, None when absent."""
    match = _PROJID32BIT.search(xfs_info_output)
    if match is None:
        return None
    return match.group(1) == "1"


def _xfs_quota_available() -> bool:
    return shutil.which("xfs_quota") is not None


def _local_facts(mount_point: str | Path) -> dict[str, Any]:
    """Gather facts about the local mount of ``mount_point``."""
    mounts_text = _read_proc_mounts()
    if mounts_text is None:
        return {"error": "filesystem detection unavailable: cannot read /proc/mounts"}
    entry = _find_mount(mounts_text, mount_point)
    if entry is None:
        return {"error": f"no mount entry found for {mount_point}"}
    fs_type, options = entry
    facts: dict[str, Any] = {
        "fs_type": fs_type,
        "prjquota": "prjquota" in options,
        "xfs_quota": _xfs_quota_available(),
    }
    if fs_type != "xfs":
        facts["projid32bit"] = False
        return facts
    if _use_quotactl(mount_point):
        # Device-free path: the geometry ioctl is ENOTTY in a container that
        # only bind-mounts the filesystem, so projid32bit is established
        # functionally (write a project id above 0xFFFF, read it back).
        supported, reason = xfs_quotactl.projid32bit(mount_point)
        if supported is None:
            logger.warning(
                "cannot determine projid32bit on %s: %s", mount_point, reason
            )
            return {"error": f"cannot determine projid32bit: {reason}"}
        facts["projid32bit"] = supported
        facts["projid32bit_source"] = reason
        # The tool may exist without being usable here; what this fact reports
        # is whether quota administration is available at all.
        facts["xfs_quota"] = True
        facts["backend"] = "quotactl"
        return facts
    info = _run_xfs_info(mount_point)
    if info is None:
        return {"error": "cannot determine projid32bit: xfs_info unavailable or failed"}
    projid32bit = _parse_projid32bit(info)
    if projid32bit is None:
        return {
            "error": "cannot determine projid32bit: xfs_info output has no projid32bit",
        }
    facts["projid32bit"] = projid32bit
    return facts


def _evaluate_facts(facts: dict[str, Any]) -> tuple[bool, str]:
    """Apply the quota decision rules to server-side or local facts."""
    if not isinstance(facts, dict):
        return _fail("invalid facts from quota-agent")
    if "error" in facts:
        return _fail(str(facts["error"]))
    if facts.get("fs_type") != "xfs":
        return _fail(f"filesystem is {facts.get('fs_type') or 'unknown'}, not xfs")
    if facts.get("projid32bit") is not True:
        return _fail("xfs projid32bit not enabled (projid32bit=0)")
    if facts.get("prjquota") is not True:
        return _fail("mount option prjquota not enabled")
    if facts.get("xfs_quota") is not True:
        return _fail("xfs_quota tool not found")
    return _PASS


def _has_effective_cap_sys_admin() -> bool:
    """True when the process holds effective CAP_SYS_ADMIN (bit 21).

    ``xfs_quota -x`` administration is gated by effective CAP_SYS_ADMIN in
    the kernel, not by euid: Docker clears the effective set for non-root
    users (CapEff=0 -> EPERM), while k8s ``runAsUser: 65534`` +
    ``capabilities.add [SYS_ADMIN]`` keeps it (E5.1 review Important-2).
    """
    status = _read_proc_self_status()
    if status is None:
        return False
    for line in status.splitlines():
        if not line.startswith("CapEff:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return False
        try:
            cap_eff = int(parts[1], 16)
        except ValueError:
            return False
        return (cap_eff >> 21) & 1 == 1
    return False


def direct_quota_privileged() -> bool:
    """True when this process may run ``xfs_quota -x`` directly.

    Root always qualifies; non-root qualifies only with effective
    CAP_SYS_ADMIN — exactly how the kernel gates quotactl/ioctl.
    """
    return os.geteuid() == 0 or _has_effective_cap_sys_admin()


def local_fs_type(mount_point: str | Path) -> str | None:
    """Return the fs_type of the deepest mount containing ``mount_point``.

    None when ``/proc/mounts`` is unavailable or the path has no mount
    entry; callers keep their real failure reason in that case.
    """
    mounts_text = _read_proc_mounts()
    if mounts_text is None:
        return None
    entry = _find_mount(mounts_text, mount_point)
    return entry[0] if entry is not None else None


def direct_quota_unprivileged_reason(mount_point: str | Path) -> str | None:
    """Return :data:`NONROOT_DIRECT_QUOTA_REASON` when this process lacks
    direct ``xfs_quota`` privilege on an XFS mount; None otherwise.

    The privilege guard only applies to XFS mounts: on non-XFS hosts (macOS
    local dev, ext4 workspaces) the real filesystem reason reported by
    detection takes priority (E5.1 review Minor-13).
    """
    if direct_quota_privileged():
        return None
    if local_fs_type(mount_point) != "xfs":
        return None
    return NONROOT_DIRECT_QUOTA_REASON


def _detect_local(mount_point: str | Path) -> tuple[bool, str]:
    # E5.1 review: direct ``xfs_quota -x`` is gated by effective
    # CAP_SYS_ADMIN, not euid. Non-root without the capability (Docker
    # clears CapEff) always gets EPERM -> disclose the quota-agent
    # requirement before any XFS probe; non-root with effective SYS_ADMIN
    # (k8s) keeps the direct path. The guard only applies on XFS mounts:
    # non-XFS hosts keep their real reason.
    reason = direct_quota_unprivileged_reason(mount_point)
    if reason is not None:
        return _fail(reason)
    return _evaluate_facts(_local_facts(mount_point))


def _detect_via_agent(mount_point: str | Path) -> tuple[bool, str]:
    query = agent_query
    if query is None:
        return _fail("quota-agent not configured (E2.6)")
    try:
        facts = query(str(mount_point))
    except Exception as exc:  # agent unreachable / protocol error -> degrade
        return _fail(f"quota-agent query failed: {exc}")
    return _evaluate_facts(facts)


def configure_agent_query(
    query: Callable[[str], dict[str, Any]] | None,
) -> None:
    """Wire the quota-agent client; E2.6 replaces the default None."""
    global agent_query
    agent_query = query


def xfs_project_supported(
    mount_point: str | Path, via_agent: bool = False
) -> tuple[bool, str]:
    """Return whether XFS project quota is usable for ``mount_point``.

    ``via_agent=False`` inspects the local mount; ``via_agent=True`` asks
    quota-agent for the NFS server-side filesystem. Never raises — every
    failure is reported as ``(False, reason)`` (macOS included) and logged
    as a warning so the caller can skip quota while still creating the
    sandbox.
    """
    result = _detect_via_agent(mount_point) if via_agent else _detect_local(mount_point)
    if not result[0]:
        logger.warning(
            "XFS project quota unavailable for %s: %s",
            str(mount_point),
            result[1],
        )
    return result
