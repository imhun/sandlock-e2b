"""XFS project quota capability detection and project management (E2.1/E2.2).

Two quota domains:

- local (``via_agent=False``): inspect the worker's own mount of
  ``mount_point``, and run ``xfs_quota`` directly on the worker.
- NFS server side (``via_agent=True``): the worker only sees an NFS mount,
  so the real filesystem lives on the server. Ask quota-agent (E2.6) for
  the server-side facts and let it execute the project operations.

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

The agent branch is reserved for E2.6: ``agent_ops`` maps op names to
callables — ``provision(sandbox_id, project_dir, disk_mb, mount_point,
project_id=None) -> int`` and ``release(project_dir, projid, mount_point)
-> None`` — installed via :func:`configure_agent_ops`. Unconfigured agent
ops raise :class:`ProjectQuotaError` so callers degrade.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

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

#: Project id range for sandboxes (design doc §3.2: 1..2^31).
_PROJID_MIN = 1
_PROJID_MAX = 1 << 31


class ProjectQuotaError(RuntimeError):
    """A quota management operation failed; callers degrade with a warning."""


def _hash_projid(sandbox_id: str) -> int:
    """Map a sandbox id to a stable projid in ``[_PROJID_MIN, _PROJID_MAX]``."""
    digest = hashlib.sha256(sandbox_id.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    return _PROJID_MIN + value % (_PROJID_MAX - _PROJID_MIN + 1)


def _parse_project_report(output: str) -> set[int]:
    """Extract the defined project ids from ``xfs_quota report -p`` output."""
    return {int(match.group(1)) for match in _PROJECT_ID_LINE.finditer(output)}


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
    output = _local_run_xfs_quota(mount_point, "report -p")
    return _parse_project_report(output)


def allocate_project_id(sandbox_id: str, mount_point: str | Path) -> int:
    """Pick a free projid: stable sandbox-id hash, linear-probed on conflict."""
    in_use = _local_in_use_projids(mount_point)
    candidate = _hash_projid(sandbox_id)
    while candidate in in_use:
        candidate = _PROJID_MIN if candidate >= _PROJID_MAX else candidate + 1
    return candidate


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
    command = f"project -C -p {shlex.quote(str(project_dir))} {projid}"
    _local_run_xfs_quota(mount_point, command)


def _fail(reason: str) -> tuple[bool, str]:
    return (False, reason)


def _read_proc_mounts() -> str | None:
    """Return ``/proc/mounts`` text, or None when unavailable (e.g. macOS)."""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
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


def _detect_local(mount_point: str | Path) -> tuple[bool, str]:
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
