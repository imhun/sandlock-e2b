"""XFS project quota capability detection (E2.1).

Two quota domains:

- local (``via_agent=False``): inspect the worker's own mount of
  ``mount_point`` — filesystem type from ``/proc/mounts``, ``projid32bit``
  from ``xfs_info``, the ``prjquota`` mount option, and the ``xfs_quota``
  tool on PATH.
- NFS server side (``via_agent=True``): the worker only sees an NFS mount,
  so the real filesystem lives on the server. Ask quota-agent (E2.6) for
  the server-side facts and evaluate them with the same rules.

Detection is strictly read-only: it never mounts, never enables quotas and
never writes files. Any unsupported result is logged as a warning so callers
can degrade (skip quota, sandbox still created).
"""

from __future__ import annotations

import logging
import re
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

_XFS_INFO_TIMEOUT_SECONDS = 5
_PASS: tuple[bool, str] = (True, "")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
_PROJID32BIT = re.compile(r"\bprojid32bit=([01])\b")


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
