"""Worker agent: node registration, heartbeat and sandbox lifecycle API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from envd_service.config import Settings
from envd_service.executors.factory import (
    sandlock_failure_detail,
    sandlock_not_installed,
    sandlock_unusable_error,
)
from envd_service.runtime.image_resolver import (
    peek_image_warm,
    resolve_image_rootfs,
)
from envd_service.runtime.context import mcp_port_stats as _mcp_port_stats
from envd_service.uid_pool import (
    align_shared_uid_workspace,
    apply_sandbox_ownership,
)
from envd_service.xfs_quota import (
    ProjectDirectoryGone,
    ProjectQuotaError,
    containing_mount_point,
    clear_project_limits,
    directory_project_id,
    project_quota_table,
    projids_in_record,
    provision_project,
    reconcile_orphan_projects,
    release_project,
    xfs_project_supported,
)
from envd_service.volumes import build_volume_mounts, cleanup_volume_projects
from gateway_common.paths import (
    UNTRUSTED_TREE_DIR,
    is_reserved_platform_namespace,
    is_sandbox_workspace_dir,
    validate_sandbox_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: XFS drops a released project's quota record only once its inode accounting
#: settles, so the reconcile running right after an ``rmtree`` can legitimately
#: report the row as still there. Retry the quota pass a bounded number of
#: times instead of leaving the entry to manual review.
_QUOTA_RECLAIM_ATTEMPTS = 3
_QUOTA_RECLAIM_DELAY_S = 0.5

#: A disk sweep that had to be deferred because the fleet's records could not
#: all be enumerated is retried on later heartbeats: doubling, capped, so a
#: persistent shortfall (a record whose node never comes back) cannot hide the
#: sweep forever -- review round 1, M1. The unit is the agent's 5s heartbeat
#: interval, so the first retry lands on the next heartbeat and the cap is
#: reached at 60s.
_RECONCILE_RETRY_MAX_INTERVALS = 12

#: N25/L2b: wall-clock ceiling for one round of the per-sandbox disk scan.
#: The scan walks each tree once against the heartbeat's own thread, so the
#: budget is what keeps a worker with one enormous tree from stalling the
#: pulse: the round returns what it finished and the next one resumes at the
#: sandbox after the last it scanned.
_DISK_SCAN_BUDGET_S = 1.0

# ``UNTRUSTED_TREE_DIR`` (where a refused tree is parked, review W7 / W7-3) is
# defined in ``gateway_common.paths`` because the fail-safe quota scan has to
# know the namespace too: a parked tree keeps the project id of the tree it
# was renamed from, so a release that failed at park time still has a
# directory the reconcile can find (review R2).


def _extract_sandbox_archive(archive_path: Path, dest: Path) -> None:
    """Extract a sandbox tar.gz, skipping absolute symlink members.

    Volume mounts are archived as symlinks to host paths that only exist on
    the source node; provisioning re-creates them on the target.
    """
    with tarfile.open(archive_path) as tar:
        members = []
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f"archive member escapes workspace: {member.name}")
            if member.issym() and os.path.isabs(member.linkname):
                continue
            members.append(member)
        try:
            tar.extractall(dest, members=members, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12
            tar.extractall(dest, members=members)


def _require_internal_key(request: Request, settings: Settings) -> None:
    key = request.headers.get("X-Internal-Key")
    # E3.6: accept any key in the rotation window list (falls back to the
    # single E2B_INTERNAL_API_KEY when the list is empty).
    if key is None or not any(
        secrets.compare_digest(key, candidate)
        for candidate in settings.all_internal_api_keys
    ):
        raise PermissionError("Unauthorized")


def _node_resources(settings: Settings) -> dict[str, int]:
    """Report node capacity: explicit env overrides, else host probing."""
    memory_mb = int(os.getenv("E2B_NODE_MEMORY_MB", "0"))
    if memory_mb <= 0:
        try:
            memory_mb = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
        except (ValueError, OSError):
            memory_mb = settings.default_memory_mb * 100
    cpu = int(os.getenv("E2B_NODE_CPU_PERCENT", "0"))
    if cpu <= 0:
        cpu = os.cpu_count() * 100 or 100
    disk = int(os.getenv("E2B_NODE_DISK_MB", "0"))
    if disk <= 0:
        try:
            disk = shutil.disk_usage(settings.workspace_base).total // (1024 * 1024)
        except OSError:
            disk = settings.default_disk_mb * 100
    processes = int(os.getenv("E2B_NODE_PROCESSES", "0"))
    if processes <= 0:
        processes = settings.default_max_processes * 100
    return {
        "totalMemoryMB": memory_mb,
        "totalCPUPercent": cpu,
        "totalDiskMB": disk,
        "totalProcesses": processes,
    }


def _node_type() -> str:
    if os.path.exists("/.dockerenv"):
        return "container"
    return "physical"


def _register_payload(settings: Settings) -> dict[str, Any]:
    return {
        "nodeID": os.getenv("E2B_NODE_ID"),
        "address": os.getenv("E2B_NODE_ADDRESS"),
        "images": [i for i in (settings.base_image,) if i],
        "labels": {
            "node-type": os.getenv("E2B_NODE_TYPE") or _node_type(),
        },
        **_node_resources(settings),
    }


def _disk_enforce_interval_s() -> float:
    """How often the worker rewalks sandbox trees for the disk report.

    ``E2B_DISK_ENFORCE_INTERVAL_S`` (default 30 s); ``0`` disables the report
    entirely, which turns the control plane's measured-disk gate off with it.
    """
    from gateway_common.env import env_float

    return env_float("E2B_DISK_ENFORCE_INTERVAL_S", 30.0)


def _heartbeat_usage_payload(
    settings: Settings,
    metrics_provider: Callable[[], dict[str, Any]] | None = None,
    activity_provider: Callable[[], dict[str, float]] | None = None,
    port_provider: Callable[[], dict[str, int]] | None = None,
    disk_report: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Disk usage + quota alerts + MCP port band carried by each heartbeat."""
    payload: dict[str, Any] = {}
    try:
        usage = shutil.disk_usage(settings.workspace_base)
        payload["diskUsedMB"] = usage.used // (1024 * 1024)
        payload["diskTotalMB"] = usage.total // (1024 * 1024)
    except OSError:
        pass
    if activity_provider is not None:
        try:
            # E9.1: per-sandbox last-activity timestamps; the control plane
            # turns them into ``last_active_at`` for idle detection.
            activity = activity_provider()
        except Exception:
            logger.warning("sandbox activity provider failed", exc_info=True)
            activity = None
        if isinstance(activity, dict) and activity:
            payload["sandboxActivity"] = activity
    if disk_report:
        # N25/L2b: measured tree sizes. The control plane pauses what is over
        # its declared ``diskMB`` -- the per-node and fleet ledgers only bound
        # what a sandbox was *sold*, so this is the only signal that sees what
        # it actually wrote. Absent when the round found nothing to report.
        payload["sandboxDiskUsage"] = dict(disk_report)
    if port_provider is not None:
        # N8: the MCP gateway port band (61001-65535, §2.9) is a per-worker
        # resource with a hard ceiling, and both workers now run the same
        # shape -- so one global watermark per node (readable from the control
        # plane's node view) replaces the old per-shape comparison. Reported
        # before the quota metrics below on purpose: a broken quota provider
        # returns early and must not take the watermark with it.
        try:
            ports = port_provider()
        except Exception:
            logger.warning("MCP port pool provider failed", exc_info=True)
            ports = None
        if isinstance(ports, dict):
            for key, wire in (
                ("in_use", "mcpPortsInUse"),
                ("capacity", "mcpPortsCapacity"),
            ):
                if key in ports:
                    payload[wire] = ports[key]
    if metrics_provider is not None:
        try:
            metrics = metrics_provider()
        except Exception:
            logger.warning("quota metrics provider failed", exc_info=True)
            return payload
        if isinstance(metrics, dict):
            for key in (
                "quotaOverLimit",
                "quotaNearLimit",
                "quotaOverLimitCount",
                "quotaNearLimitCount",
                "diskWarnCount",
                "diskErrorCount",
            ):
                if key in metrics:
                    payload[key] = metrics[key]
    return payload


@dataclass(frozen=True)
class _TeardownPlan:
    """Verified teardown targets for one sandbox tree (review round 1, M4).

    ``sandbox.json`` is written at the root of the tree the sandbox itself
    owns (``0770``, owner = the sandbox's host uid), so the sandbox can
    replace it with a record that points anywhere. Everything destructive is
    therefore taken from the *disk* — the directory the scan found and the
    project id the filesystem reports — never from the record's own fields.
    """

    workspace_dir: Path
    #: Project state to release; ``None`` means "nothing to release" (the
    #: disk reports no project id, or the disk cannot be asked at all).
    project_id: int | None
    volume_projects: tuple[dict[str, Any], ...]
    #: The project ids this teardown is expected to make reclaimable, for the
    #: bounded quota reclaim retry. Unverified entries contribute the record's
    #: claim so a row that never settles is still reported instead of silent.
    expected_projids: frozenset[int]


class SandboxTeardownRefused(Exception):
    """The record contradicts the disk, so nothing was touched (W1).

    Raised by :func:`_delete_sandbox_runtime` when the record it was asked to
    act on disagrees with the tree the disk has. The record is input the
    sandbox can rewrite, so a contradiction is refused rather than resolved:
    the caller gets the reason (the delete endpoint turns it into a 409) and
    an operator can look at the tree, which is left exactly as found.
    """


class SandboxTreeNotRemoved(RuntimeError):
    """The tree survived its own teardown, so the teardown did not happen.

    Raised by :func:`_delete_sandbox_runtime` when the in-process removal and
    the ``e2b-maint`` fallback could not take the workspace off the disk (a
    sealed directory on a worker with no brokers, a mount the process may not
    walk). Reporting success here is what let a control plane forget a sandbox
    whose tree -- and whose unreadable leftovers -- stayed behind forever
    (review W7 / W7-2).
    """


def _release_teardown_tombstone(runtime_registry, sandbox_id: str) -> None:
    """Disarm the just-unregistered marker after a teardown that did not run.

    ``unregister`` arms the marker so a concurrent ``get()`` cannot
    materialise the record of a tree that is being deleted (race B). A refusal
    keeps the files, so the marker must not outlive it: for the next
    ``UNREGISTER_TOMBSTONE_S`` seconds ``get()`` would answer ``None``, and
    "no record" is read as "nothing to verify" (review W7 / W7-1).
    """
    release = getattr(runtime_registry, "release_tombstone", None)
    if release is not None:
        release(sandbox_id)


def _recorded_dir_matches(recorded_dir: Any, workspace_dir: Path) -> bool:
    """Whether a record's ``workspace_dir`` names the tree the caller scans.

    ``sandbox.json`` is input the sandbox can rewrite, so no teardown ever
    acts on the path a record claims -- every target is ``<base>/<id>``. This
    check only decides whether the record *contradicts* the disk, and it
    compares directory identities instead of spellings (review W7 / C1):

    * the same directory reached through a symlinked base, a relative path, a
      ``..`` segment or a trailing slash is the same tree and must not be
      refused. Refusing it turned a working delete (``de555f8``: 204, tree and
      record gone) into a 409 and pinned the tree plus its quota row forever;
    * a record that aims at *another* existing directory is exactly the shape
      the refusal exists for, and is still refused;
    * a recorded path that is not on this disk (an old base, a path that lives
      on another node) does not identify a second tree this teardown could
      reach into, so the convention path is the only target and it proceeds.

    ``resolve()`` (spelling) and ``(st_dev, st_ino)`` (identity, which also
    covers hard links and bind-mount spellings) are both compared.
    """
    recorded = Path(recorded_dir)
    try:
        if recorded.resolve() == Path(workspace_dir).resolve():
            return True
        recorded_stat = recorded.stat()
        workspace_stat = Path(workspace_dir).stat()
    except OSError:
        # Either the recorded path or the tree itself is not on the disk:
        # nothing here names a second tree that could be torn down instead.
        return True
    return (recorded_stat.st_dev, recorded_stat.st_ino) == (
        workspace_stat.st_dev,
        workspace_stat.st_ino,
    )


def _is_the_directory_it_spells(path: Path) -> bool:
    """Whether ``path`` *is* the directory it names, not a link to another.

    The volume slice check used to compare the entry's *name* (is it spelled
    after this sandbox?) and the *resolved* path (is it inside the shared
    volume root?), and both pass for a link that lives inside the volume root,
    is named after this sandbox, and points at another tenant's slice: the
    containment test resolves the link to a path that is still inside the
    root, and the project id is then read (and released) through it, i.e. off
    the victim's slice (review W7 / W7-5). The path a slice is *declared* at
    and the directory a destructive call would open have to be the same
    object, which is an identity question, not a spelling one:
    ``lstat`` describes the entry itself and ``stat`` the directory a
    ``rmtree``/``chown`` would reach, so a symlink (and anything that is not a
    directory at all) fails while hard links, bind mounts and equivalent
    spellings of the same directory pass.

    A path that is not on the disk (or cannot be stat'ed) is not refused here:
    there is no second directory it could point at, and the disk read in
    :func:`_verified_project_id` already reports that shape (its row is left
    to the fail-safe reconcile).
    """
    if os.path.islink(path):
        # The entry itself is a link -- including a broken one, whose target
        # ``stat`` cannot reach: either way the destructive call would open
        # something other than the slice this sandbox owns.
        return False
    try:
        declared = os.lstat(path)
        opened = os.stat(path)
    except OSError:
        return True
    if not path.is_dir():
        # A file/FIFO/socket named after the sandbox is not a slice: a
        # project id can live on any inode, and a release would act on it.
        return False
    return (declared.st_dev, declared.st_ino) == (opened.st_dev, opened.st_ino)


def _verified_project_id(
    path: Path,
    claimed: Any,
    expected: set[int],
    *,
    context: str = "reconcile",
    force: bool = False,
) -> tuple[int | None, str | None]:
    """Project id to release for ``path``, read from the disk (M4).

    Returns ``(project_id, reason)``: ``reason`` is set when the record's
    claim contradicts the disk, in which case the caller must refuse the tree
    (a mismatched claim is exactly what a rewritten ``sandbox.json`` looks
    like). A disk that cannot be asked yields ``(None, None)``: the tree is
    still reclaimed, but its project state is left alone and the fail-safe
    quota reconcile drops the row once the tree is gone.

    ``context`` is the log prefix ("reconcile" for the orphan-tree sweep,
    "delete" for the explicit delete endpoint) so an operator can tell which
    caller is talking about the path.

    ``force`` is the operator's explicit "tear this tree down from the disk"
    decision (review W7 / C1-3): a claim that contradicts the disk is then
    *reported* instead of refused, and the project id the disk reports is the
    one released. Only the caller's own convention path and its own slice
    names are ever reached either way.
    """
    try:
        disk_projid = directory_project_id(path)
    except ProjectQuotaError as exc:
        # Three shapes, three lines an operator can tell apart without
        # stat'ing the path by hand (follow-up 1). The message text carries
        # the class; the level carries the expectation:
        #
        # * gone (ENOENT) -- the control plane deleted the volume, and this
        #   slice went with it: nothing to verify, and the row is left to the
        #   fail-safe reconcile, so this is INFO and not an anomaly;
        # * exists but unreadable (EACCES) -- a real anomaly, WARNING;
        # * this host cannot ask the disk at all -- WARNING.
        if isinstance(exc, ProjectDirectoryGone):
            logger.info(
                "%s: %s; nothing to verify, its quota row is left to "
                "the fail-safe reconcile",
                context,
                exc,
            )
        else:
            logger.warning(
                "%s: %s; reclaiming it without releasing its quota row",
                context,
                exc,
            )
        if isinstance(claimed, int) and claimed > 0:
            expected.add(claimed)
        return None, None
    if disk_projid is None:
        # The directory carries no project id: there is no row to release, so
        # whatever the record claims cannot be acted on (a release needs a
        # matching (directory, projid) pair). This is also the shape a
        # half-finished earlier teardown leaves behind.
        return None, None
    if isinstance(claimed, int) and claimed > 0 and claimed != disk_projid:
        if not force:
            return None, (
                f"its sandbox.json claims project id {claimed} but the disk "
                f"says {disk_projid}"
            )
        logger.warning(
            "%s: forcing %s: its sandbox.json claims project id %s but the "
            "disk says %s; releasing the project id the disk reports",
            context,
            path,
            claimed,
            disk_projid,
        )
    expected.add(disk_projid)
    return disk_projid, None


def _verified_teardown_plan(
    workspace_base: str | Path,
    sandbox_id: str,
    record,
    *,
    shared_volume_root: str | Path | None = None,
    verify_quota: bool = True,
    context: str = "reconcile",
    force: bool = False,
    verify_tree_project: bool = True,
) -> tuple[_TeardownPlan | None, str | None]:
    """Verified teardown targets for one sandbox tree (M4 / W1).

    Both callers act on trees whose record lives inside the tree itself,
    i.e. on input the sandbox could rewrite: the orphan-tree GC scans those
    trees off the disk, and the explicit delete endpoint reads the record
    back through the registry (which falls back to the same file). Three
    rules keep a rewritten record from aiming either path at another
    tenant's data:

    * the tree is always ``<workspace_base>/<sandbox_id>``, never the
      ``workspace_dir`` the record claims (``sandbox_id`` is the directory
      name the scan read off the filesystem);
    * a record whose ``sandbox_id``/``workspace_dir`` disagree with the
      directory it was found in is refused outright;
    * a volume slice is only honoured when it is named after this sandbox and
      lives under the worker's configured ``shared_volume_root``, and when the
      entry *is* the directory it spells -- ``lstat``/``stat`` identity, so a
      link inside the volume root that is named after this sandbox but points
      at another tenant's slice cannot smuggle that slice into the target set
      (review W7 / W7-5).

    The project ids come from the disk, not from the record: a
    contradiction refuses the tree, a silent disk drops the release and
    leaves the row to the fail-safe quota reconcile.

    ``record`` is ``None`` for a teardown with nothing to verify (no record
    anywhere): the convention path is then all that is left, and it carries
    no project state.

    ``verify_quota=False`` is for a teardown that keeps the files
    (``keep_files=true``: migration stop, shared-workspace teardown): no
    project state is released and no slice is removed, so reading the disk's
    project ids would only produce an anomaly line for something this call is
    not going to touch. The record still has to describe its own tree.

    ``context`` prefixes the anomaly lines ("reconcile" for the sweep,
    "delete" for the endpoint).

    ``workspace_base`` is the root the caller scanned or read the record
    under: the GC passes the worker's configured base, the delete endpoint
    passes the registry's own base (where the record file it just read
    lives), so the target is always derived from the caller's evidence and
    never from the record's ``workspace_dir``.

    ``shared_volume_root`` is the configured volume root a slice has to be
    inside to be honoured (``None`` = this worker has no shared volume root
    configured, so only the slice's own name is checked).

    ``force`` (review W7 / C1-3) is the operator's explicit "tear this tree
    down using the disk alone" decision, and it is the bounded exit a refusal
    needs: the claim checks below stop returning a refusal reason and *report*
    what they overruled instead, while every target still comes from the
    caller's own convention path -- the tree is always
    ``<workspace_base>/<sandbox_id>``, the project ids are always the ones the
    filesystem reports, and a volume entry still has to be named after this
    sandbox and live under the configured volume root. No target of another
    tenant is reachable with ``force`` either.

    ``verify_tree_project=False`` is for a caller that releases no project
    state for the tree itself -- the combined node's local teardown cleans
    volume slices and removes the tree, and its workspace project is the envd
    half's business. Reading the tree's project id would then only produce an
    anomaly line (on a host that cannot ask the disk at all) for something
    this call is not going to touch.
    """
    workspace_dir = Path(workspace_base) / sandbox_id
    if record is not None:
        recorded_id = getattr(record, "sandbox_id", None)
        if recorded_id != sandbox_id:
            if not force:
                return None, f"its sandbox.json names sandbox {recorded_id!r}"
            logger.warning(
                "%s: forcing the teardown of %s: its sandbox.json names "
                "sandbox %r; using the directory name",
                context,
                sandbox_id,
                recorded_id,
            )
        recorded_dir = getattr(record, "workspace_dir", None)
        if recorded_dir is not None and not _recorded_dir_matches(
            recorded_dir, workspace_dir
        ):
            if not force:
                return None, f"its sandbox.json points at {recorded_dir}"
            logger.warning(
                "%s: forcing the teardown of %s: its sandbox.json points at "
                "%s; using the convention path %s",
                context,
                sandbox_id,
                recorded_dir,
                workspace_dir,
            )
    if not verify_quota:
        return (
            _TeardownPlan(
                workspace_dir=workspace_dir,
                project_id=None,
                volume_projects=(),
                expected_projids=frozenset(),
            ),
            None,
        )
    expected: set[int] = set()
    project_id: int | None = None
    if verify_tree_project:
        project_id, reason = _verified_project_id(
            workspace_dir,
            getattr(record, "project_id", None),
            expected,
            context=context,
            force=force,
        )
        if reason is not None:
            return None, reason
    volume_entries = _verified_volume_slices(
        record,
        sandbox_id,
        shared_volume_root,
        expected=expected,
        context=context,
        force=force,
    )
    return (
        _TeardownPlan(
            workspace_dir=workspace_dir,
            project_id=project_id,
            volume_projects=tuple(volume_entries),
            expected_projids=frozenset(expected),
        ),
        None,
    )


def _verified_volume_slices(
    record,
    sandbox_id: str,
    shared_volume_root: str | Path | None,
    *,
    expected: set[int],
    context: str,
    force: bool = False,
) -> list[dict[str, Any]]:
    """The volume slices of ``record`` this worker may act on, with disk projids.

    Every entry a rewritten ``volume_projects`` list can point somewhere else
    is dropped, and the project id of the ones that survive is the id the
    *disk* reports for the slice (never the record's claim):

    * the slice has to be named after this sandbox and to declare this
      sandbox as its owner;
    * when the worker has a configured ``shared_volume_root``, the slice has
      to live under it;
    * the entry has to *be* the directory it spells (``lstat``/``stat``
      identity, review W7 / W7-5), so a link inside the volume root that is
      named after this sandbox cannot smuggle another tenant's slice in;
    * a claim that contradicts the disk refuses the entry (``force=True``
      reports it and releases the disk's id instead).

    ``expected`` accumulates every project id the caller may have to reclaim
    (it is the plan's ``expected_projids``). Shared by the teardown plan and by
    :func:`_park_refused_tree`, which releases the same slices after a refusal
    the plan itself never got far enough to describe (review R2).
    """
    shared_root = (
        Path(shared_volume_root).resolve() if shared_volume_root else None
    )
    volume_entries: list[dict[str, Any]] = []
    for entry in getattr(record, "volume_projects", None) or []:
        if not isinstance(entry, dict):
            continue
        sandbox_dir = entry.get("sandbox_dir")
        slice_dir = Path(sandbox_dir) if isinstance(sandbox_dir, (str, Path)) else None
        if (
            slice_dir is None
            or entry.get("sandbox_id") != sandbox_id
            or slice_dir.name != sandbox_id
        ):
            logger.warning(
                "%s: refusing a volume entry of %s: %s is not a slice "
                "of this sandbox",
                context,
                sandbox_id,
                sandbox_dir,
            )
            continue
        if shared_root is not None and not slice_dir.resolve().is_relative_to(
            shared_root
        ):
            logger.warning(
                "%s: refusing the volume slice %s of %s: it is outside "
                "the shared volume root %s",
                context,
                slice_dir,
                sandbox_id,
                shared_root,
            )
            continue
        if not _is_the_directory_it_spells(slice_dir):
            logger.warning(
                "%s: refusing the volume slice %s of %s: the entry is not "
                "the directory it spells (a link or a non-directory), so it "
                "could name another tenant's slice",
                context,
                slice_dir,
                sandbox_id,
            )
            continue
        slice_projid, slice_reason = _verified_project_id(
            slice_dir,
            entry.get("projid"),
            expected,
            context=context,
            force=force,
        )
        if slice_reason is not None:
            logger.warning(
                "%s: refusing the volume slice %s of %s: %s",
                context,
                slice_dir,
                sandbox_id,
                slice_reason,
            )
            continue
        volume_entries.append({**entry, "projid": slice_projid})
    return volume_entries


def _untrusted_entry_for(
    settings: Settings, runtime_registry, sandbox_id: str
) -> tuple[dict | None, str | None]:
    """One audit entry for a refused tree, or why ``sandbox_id`` is not one.

    Review W7 / W7-3: the refusal used to be visible in the worker's log only
    (``_report_reconcile_summary``) and had no exit an operator could reach
    once the control plane had released the record -- ``force`` on the API
    then answers 404, because the node hosting the tree is exactly what the
    released record no longer says. The entry carries the reason a teardown
    refuses the tree and the project ids involved, so parking it is an
    informed, bounded action rather than a guess.

    Two shapes count as "refused": a tree whose own ``sandbox.json``
    contradicts the directory it lives in, and a sandbox-shaped tree with no
    readable record at all (the leftover an interrupted teardown leaves --
    with no record there is no project id the GC could release, which is why
    it is reported instead of torn down). A missing or *healthy* tree is
    returned as ``(None, reason)``: parking it would be acting on a tree the
    product still owns.

    Review R1 added a third shape, and it is the one the shape rule cannot
    see at all: a top-level directory whose name is one of the infrastructure
    namespaces (``snap_`` / ``_``) and whose ``sandbox.json`` is gone. Both
    prefixes are *legal sandbox ids* (``X-Sandbox-Id`` is checked with
    ``validate_sandbox_id`` alone and a snapshot id may be chosen by the
    caller), so such a directory can be the leftover of a real sandbox tree,
    and the disk -- not the name, not the missing record -- is what says so:
    if the directory really carries a project id, it is this worker's quota
    asset and has to be visible to the operator's exit. Without this, its
    tree and its quota row pinned each other permanently and no entry existed
    anywhere.
    """
    base = Path(settings.workspace_base)
    entry = base / sandbox_id
    if not is_sandbox_workspace_dir(entry):
        leftover_projid = _leftover_quota_projid(entry)
        if leftover_projid is not None:
            return (
                {
                    "sandbox_id": sandbox_id,
                    "reason": (
                        "it has no readable sandbox.json and its name is "
                        "outside the sandbox shapes this worker acts on; the "
                        f"disk reports project id {leftover_projid} for it"
                    ),
                    "disk_project_id": leftover_projid,
                    "claimed_project_ids": [],
                },
                None,
            )
        return None, (
            f"{entry} is not a sandbox workspace tree on this worker"
        )
    try:
        record = runtime_registry.peek(sandbox_id)
    except Exception:  # pragma: no cover - defensive
        record = None
    if record is None:
        return (
            {
                "sandbox_id": sandbox_id,
                "reason": "it has no readable sandbox.json",
                "disk_project_id": _disk_project_id_or_none(entry),
                "claimed_project_ids": [],
            },
            None,
        )
    try:
        plan, reason = _verified_teardown_plan(
            base,
            sandbox_id,
            record,
            shared_volume_root=settings.shared_volume_root,
            context="untrusted",
        )
    except Exception as exc:  # pragma: no cover - defensive
        plan, reason = None, f"its targets cannot be verified: {exc}"
    if plan is not None:
        return None, (
            "its sandbox.json describes the tree it lives in: it is a healthy "
            "tree this worker would tear down itself, not a refused one"
        )
    return (
        {
            "sandbox_id": sandbox_id,
            "reason": reason,
            "disk_project_id": _disk_project_id_or_none(entry),
            "claimed_project_ids": sorted(projids_in_record(record.to_dict())),
        },
        None,
    )


def _disk_project_id_or_none(path: Path) -> int | None:
    """The project id the disk reports, or ``None`` when it cannot be asked."""
    try:
        return directory_project_id(path)
    except ProjectQuotaError:
        return None


def _leftover_quota_projid(entry: Path) -> int | None:
    """The project id the disk reports for a directory the shape rule rejects.

    ``is_sandbox_workspace_dir`` separates sandbox trees from infrastructure
    by *shape* (a real directory whose name is a legal id and which is either
    outside the infrastructure namespaces or carries its own top-level
    ``sandbox.json``). The shapes it leaves out are not automatically "not
    ours": a client may choose an id that starts with ``snap_`` or ``_``, so a
    leftover of such a tree loses every name-based signal the moment its
    record is gone.

    What does not lie is the disk. A directory that carries a project id is a
    quota asset this worker has a row for, so it is exactly what the operator's
    park exit and the fail-safe reconcile's second stage have to cover; a
    directory that carries none -- the snapshot store (top-level
    ``snapshot.json`` + ``fs/``), ``_volumes``, every other platform namespace
    -- is left completely alone. The platform's own namespaces are excluded by
    name as well, because nothing ever assigns a project id to them (a base
    carrying ``PROJINHERIT`` would be the one way for them to report one) and
    parking one would move a whole namespace off the workspace.

    The name still has to be one a sandbox *could* have
    (:func:`gateway_common.paths.validate_sandbox_id`): this exit offers a
    directory to an operator to move, and a name that can never be a sandbox
    id (``_untrusted.trees`` itself, the route-B scratch root, anything with a
    dot or a space) is platform or foreign storage rather than the leftover of
    a sandbox tree. Its *row* is not forfeited by that -- the fail-safe
    reconcile's carrier search has no name rule and releases those rows -- only
    this exit's "park it" offer is.
    """
    if entry.is_symlink() or not entry.is_dir():
        return None
    if not validate_sandbox_id(entry.name):
        return None
    if is_reserved_platform_namespace(entry.name):
        return None
    return _disk_project_id_or_none(entry)


def _untrusted_workspace_trees(settings: Settings, runtime_registry) -> list[dict]:
    """Every tree on this worker that a teardown would refuse (W7-3).

    The listing ``GET /agent/untrusted`` serves: the operator's positive,
    greppable view of what the refusal branch (``reconcile: leaving %s on
    disk``) reported in the log, and the input an informed ``park`` needs.
    Its candidate set is the union of the two shapes R1/R2 make visible: the
    shape rule's own trees, and the top-level directories the shape rule
    leaves out *that the disk reports a project id for* (a handful of
    directories at most, so the extra read per candidate is bounded; see
    :func:`_leftover_quota_projid`). :func:`_untrusted_entry_for` is the single
    decision point for both, so a candidate is never read twice and a
    directory that is neither is answered without a disk read.
    """
    base = Path(settings.workspace_base)
    entries: list[dict] = []
    try:
        candidates = sorted(base.iterdir())
    except OSError:  # pragma: no cover - defensive
        return entries
    for candidate in candidates:
        entry, _reason = _untrusted_entry_for(
            settings, runtime_registry, candidate.name
        )
        if entry is not None:
            entries.append(entry)
    return entries


def _park_refused_tree(
    settings: Settings, runtime_registry, sandbox_id: str, reason: str
) -> tuple[int | None, str | None]:
    """Park a tree this worker refuses to act on, and free the row it pinned.

    Review W7 / W7-3's bounded exit. A record that contradicts the disk must
    not be acted on (it may be describing a bind-mounted other tenant's tree,
    which is what the refusal protects), and once the control plane has
    released the record (eviction, TTL, a worker that was unreachable) there
    is no API ``force`` left to reach it with. Left where they are, those
    trees are a permanent pin: their own ``sandbox.json`` keeps the project
    ids they claim in ``xfs_quota._recorded_projids`` ("still live"), so the
    fail-safe reconcile never drops the row.

    The exit is a *move*, never a delete:

    * the tree goes to ``<base>/_untrusted.trees/<id>`` -- out of the
      top-level scan (the name cannot be a sandbox id, so the quarantine can
      never land inside a live sandbox's workspace), payload intact, with a
      ``.reason`` marker next to it, so an operator can see what was refused
      and why;
    * the project id released is the one the *disk* reports for the tree
      (read before the move; a rename keeps the inode's project id), never one
      the record claims -- the same rule every other teardown follows;
    * the same holds for the sandbox's per-volume slices (review R2): a
      refused record can *claim* volume projects too, and those claims kept
      their rows "recorded" for exactly as long as the tree stayed in the
      workspace scan -- after which no scan could find them, so they were
      orphaned forever. Only slices that pass the teardown's own guards are
      considered, and the id released is the one the disk reports for the
      slice (a contradicting claim is named in the log, not acted on);
    * nothing is deleted: if the path is a mount point (``EBUSY``) or the
      move fails for any other reason, the tree stays exactly where it was
      and the caller gets the failure.

    Returns ``(released_projid, error)``: ``error`` is set when the tree could
    not be parked, in which case nothing was touched.
    """
    base = Path(settings.workspace_base)
    source = base / sandbox_id
    quarantine = base / UNTRUSTED_TREE_DIR
    try:
        if not source.is_dir() or source.is_symlink():
            return None, f"{source} is not a directory"
        # Read the record while it is still where ``peek`` looks for it: the
        # move takes the tree -- ``sandbox.json`` included -- with it.
        slices = _parkable_volume_slices(settings, runtime_registry, sandbox_id)
        try:
            # Disk truth, read while the tree is still where the records put
            # it (the probes' fake table, like ``lsattr``, is path-keyed).
            projid = directory_project_id(source)
        except ProjectQuotaError as exc:
            logger.warning(
                "park: %s: cannot read its project id from the disk (%s); "
                "parking it without a release",
                sandbox_id,
                exc,
            )
            projid = None
        quarantine.mkdir(parents=True, exist_ok=True)
        dest = quarantine / sandbox_id
        if dest.exists() or dest.is_symlink():
            # A previous attack already parked one under this id: keep both
            # trees, and keep the audit trail readable.
            dest = quarantine / f"{sandbox_id}.{time.time_ns()}"
        os.replace(source, dest)  # same filesystem: a rename, never a copy
        try:
            (quarantine / f"{dest.name}.reason").write_text(
                f"{time.time():.0f}\t{sandbox_id}\t{reason}\n", encoding="utf-8"
            )
        except OSError:
            logger.warning(
                "park: %s: could not write its reason marker",
                sandbox_id,
                exc_info=True,
            )
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if projid is not None:
        try:
            release_project(
                project_dir=dest,
                mount_point=settings.workspace_base,
                projid=projid,
                via_agent=settings.quota_via_agent,
            )
        except ProjectQuotaError as exc:
            logger.warning(
                "park: %s: its project %s could not be released (%s); the "
                "fail-safe reconcile takes the row now that the tree is out "
                "of the workspace scan",
                sandbox_id,
                projid,
                exc,
            )
    for slice_dir, slice_projid in slices:
        try:
            release_project(
                project_dir=slice_dir,
                mount_point=(
                    containing_mount_point(slice_dir) or settings.workspace_base
                ),
                projid=slice_projid,
                via_agent=settings.quota_via_agent,
            )
        except ProjectQuotaError as exc:
            logger.warning(
                "park: %s: the volume slice %s could not be released (%s); "
                "the fail-safe reconcile takes its row, including from the "
                "parked tree when the release that failed is the slice's own "
                "project state",
                sandbox_id,
                slice_dir,
                exc,
            )
            continue
        logger.warning(
            "park: %s: released the project id %s its volume slice %s "
            "carries on the disk",
            sandbox_id,
            slice_projid,
            slice_dir,
        )
    logger.warning(
        "park: moved the refused tree %s to %s (payload kept, project %s "
        "released)",
        sandbox_id,
        UNTRUSTED_TREE_DIR,
        projid,
    )
    return projid, None


def _parkable_volume_slices(
    settings: Settings, runtime_registry, sandbox_id: str
) -> list[tuple[Path, int]]:
    """The ``(slice, projid)`` volume pairs a park of ``sandbox_id`` releases.

    Review R2: parking a refused tree moved the record (and with it every
    project id it *claims* out of ``xfs_quota._recorded_projids``) while only
    the tree's own disk row was released, so a claimed volume project stayed
    orphaned where nothing could reach it.

    The claims are never the evidence. The slice list comes from
    :func:`_verified_volume_slices` -- the same guards the teardown plan uses,
    so the entry has to be named after this sandbox under the configured
    volume root and has to *be* the directory it spells -- and the project id
    is the one the disk reports for that directory. ``force=True`` is the
    operator's park decision applied to the slice's own claim: a record that
    names a different project id than the disk does is reported and the disk's
    id is the one released, exactly like ``DELETE ...?force=true``.
    """
    try:
        record = runtime_registry.peek(sandbox_id)
    except Exception:  # pragma: no cover - defensive
        record = None
    if record is None:
        return []
    entries = _verified_volume_slices(
        record,
        sandbox_id,
        settings.shared_volume_root,
        expected=set(),
        context="park",
        force=True,
    )
    return [
        (Path(entry["sandbox_dir"]), entry["projid"])
        for entry in entries
        if isinstance(entry.get("projid"), int) and entry["projid"] > 0
    ]


def _registry_workspace_base(runtime_registry, settings: Settings) -> Path:
    """Workspace root the registry reads this sandbox's record under.

    A teardown has to act on the tree its evidence came from, and for the
    delete endpoint that is the registry's own base: it is where the record
    file lives, and where ``<base>/<sandbox_id>`` is a real directory of this
    worker rather than a path some record named. In every deployment shape it
    is the same value as ``settings.workspace_base``; the registry is used
    because it is the one holding the file that was read.
    """
    base = getattr(runtime_registry, "workspace_base", None)
    return Path(base) if base is not None else Path(settings.workspace_base)


def _delete_sandbox_runtime(
    settings: Settings,
    runtime_registry,
    sandbox_id: str,
    *,
    keep_files: bool = False,
    keep_volume_slices: bool = False,
    plan: _TeardownPlan | None = None,
    unregister: bool = True,
    force: bool = False,
) -> None:
    """Full local teardown for one sandbox runtime (shared by the delete
    endpoint and E6.1 orphan reconciliation).

    Every teardown acts on a verified target set (review round 1 M4 / W1).
    The orphan-tree GC scans records that live inside sandbox-owned trees and
    passes what it read off the disk; the explicit delete path passes nothing,
    and the record it reads through the registry is only *checked* against
    the disk -- a record that contradicts the tree it was found in is refused
    (:class:`SandboxTeardownRefused`) instead of being acted on.

    ``unregister=False`` is for a caller that already unregistered on the
    event loop (the reconcile round does: ``unregister``'s callbacks touch
    loop-owned objects, while this whole function runs on a worker thread).

    ``force=True`` (review W7 / C1-3) is the operator's bounded exit from a
    refusal: :func:`_verified_teardown_plan` reports instead of refusing the
    claims it overrules, and the teardown still runs on the convention path
    and the project ids the disk reports. Without it a contradicting record
    is refused -- but the *process tree* is stopped either way (review W7 /
    C1-4): a refusal keeps the files, never a running runtime behind a control
    plane that has already forgotten the sandbox.
    """
    if plan is None:
        record = runtime_registry.get(sandbox_id)
        base = _registry_workspace_base(runtime_registry, settings)
        if record is None:
            # No record at all: there is nothing on the disk to check, so the
            # convention path is the only target and it carries no project
            # state to release (unchanged behaviour for this shape).
            plan = _TeardownPlan(
                workspace_dir=base / sandbox_id,
                project_id=None,
                volume_projects=(),
                expected_projids=frozenset(),
            )
        else:
            plan, reason = _verified_teardown_plan(
                base,
                sandbox_id,
                record,
                shared_volume_root=settings.shared_volume_root,
                verify_quota=not keep_files,
                context="delete",
                force=force,
            )
            if plan is None:
                # The record does not describe the tree it was found in, so it
                # is not evidence of anything: refusing keeps whatever it
                # points at (another tenant's tree, another tenant's quota
                # row) out of this teardown's reach (W1). The runtime is not
                # part of the record's reach, so it still goes (review W7 /
                # C1-4): leaving it running is what turned a refused record
                # into "the control plane forgot it and the process tree is
                # still up". Files are what the refusal protects.
                logger.warning(
                    "delete: refusing to tear down %s: %s", sandbox_id, reason
                )
                if unregister:
                    runtime_registry.unregister(sandbox_id)
                    # The just-unregistered marker covers the teardown itself
                    # (race B): nothing is being torn down here, and the record
                    # file stays on disk on purpose, so the marker has to go
                    # with the refusal. Leaving it armed made
                    # ``RuntimeRegistry.get()`` answer ``None`` for this id for
                    # the next ``UNREGISTER_TOMBSTONE_S`` seconds, and the next
                    # delete then read "no record" as "nothing to verify" and
                    # deleted the very files the refusal promised to keep
                    # (review W7 / W7-1). Release it before the raise: an
                    # exception is not a teardown.
                    _release_teardown_tombstone(runtime_registry, sandbox_id)
                raise SandboxTeardownRefused(
                    f"refusing to tear down {sandbox_id}: {reason}"
                )
    workspace_dir = plan.workspace_dir
    project_id = plan.project_id
    volume_projects = list(plan.volume_projects)
    if unregister:
        runtime_registry.unregister(sandbox_id)
    try:
        # Shared-workspace deployments keep the directory (keep_files=true):
        # the same storage hosts the sandbox on every node, so removing it
        # would destroy the live sandbox's files, and its project id must stay
        # until the sandbox is really deleted.
        # keep_volume_slices=true is the migration counterpart: the workspace
        # may be removed (non-shared workspace export finished), but
        # per-sandbox volume slices under a shared volume root are still in
        # use by the target node and must never be deleted by a migration
        # stop/rollback.
        if keep_files:
            return
        if project_id is not None:
            try:
                release_project(
                    project_dir=workspace_dir,
                    mount_point=settings.workspace_base,
                    projid=project_id,
                    via_agent=settings.quota_via_agent,
                )
            except ProjectQuotaError as exc:
                logger.warning(
                    "XFS project quota cleanup failed for %s: %s",
                    sandbox_id,
                    exc,
                )
        if not keep_volume_slices:
            cleanup_volume_projects(
                volume_projects=volume_projects,
                fallback_mount_point=settings.workspace_base,
                via_agent=settings.quota_via_agent,
            )
        # Broker-first (Track F): a sandbox workspace is `0770` owned by its
        # own host uid with the worker's gid (fix round 1 / c1), so the
        # worker's own group access normally deletes it in-process; e2b-maint
        # is the fallback for trees that access cannot reach (sandbox-made
        # 0700 subdirs, root-owned leftovers from before the cut-over).
        from envd_service import priv_helpers

        # ``on_error="raise"`` (review W7 / W7-2): a silent failure here used to
        # be indistinguishable from success, so a tree the worker could not
        # remove still ended in "the record is gone and the tree is not". The
        # broker is the fallback; when even it cannot remove the tree, the
        # caller has to see a failure rather than a 204.
        try:
            priv_helpers.remove_tree(workspace_dir, on_error="raise")
        except Exception as exc:
            # The in-process ``rmtree`` failed *and* the brokers were absent,
            # refused, or failed too (W7-4 is what makes the broker branch
            # reachable at all). Name it, so the endpoint answers 500 with the
            # reason instead of an unhandled traceback.
            raise SandboxTreeNotRemoved(
                f"the workspace of {sandbox_id} could not be removed "
                f"({workspace_dir}): {type(exc).__name__}: {exc}"
            ) from exc
        if workspace_dir.exists():  # pragma: no branch - defensive re-check
            # A broker that reported success while the tree survived is a
            # failure too: "recorded as removed" with the tree still on disk is
            # the exact shape the GC can never reclaim (it has no record to
            # read), so it must not be reported as a teardown.
            raise SandboxTreeNotRemoved(
                f"the workspace of {sandbox_id} survived its teardown: "
                f"{workspace_dir} is still on disk"
            )
        if project_id is not None:
            # N12: only now, with the tree gone and its accounting at zero, can
            # the quota row itself be dropped -- XFS keeps a record while its
            # limits are non-zero, and this is the delete that no longer has to
            # wait for a reconciliation to say so. It runs after the removal on
            # purpose: resetting the limits first would leave a *live* sandbox
            # unbounded if the removal then failed.
            try:
                clear_project_limits(
                    mount_point=settings.workspace_base,
                    projid=project_id,
                    via_agent=settings.quota_via_agent,
                )
            except ProjectQuotaError as exc:
                logger.warning(
                    "XFS project row cleanup failed for %s: %s", sandbox_id, exc
                )
    finally:
        # The just-unregistered marker only has to cover the teardown itself
        # (race B): once the tree is gone, its disk record cannot come back.
        release_tombstone = getattr(runtime_registry, "release_tombstone", None)
        if release_tombstone is not None:
            release_tombstone(sandbox_id)


def _scan_workspace_runtimes(
    settings: Settings, runtime_registry
) -> tuple[dict[str, Any], list[str]]:
    """Materialise runtime records for the sandbox trees still on disk.

    ``RuntimeRegistry`` starts empty in a fresh process (it does not scan the
    workspace at startup), while the workspace keeps every tree the worker was
    running — ``sandbox.json`` included. ``RuntimeRegistry`` already knows how
    to read that file back (``peek``, the non-caching form used here so a
    foreign node's record never enters this process), so the reconciler only
    needs to know *which ids to ask about*: every top-level sandbox workspace
    directory, filtered by the same predicate the quota orphan scan uses.

    Returns ``(records, unmaterialised)``: the records read back successfully,
    and the ids of sandbox-shaped trees whose record could not be read
    (missing, corrupt, non-JSON or non-record ``sandbox.json``). The latter
    are reported but never torn down from here: with no record there is no
    project id to release, so deleting the tree would be all risk and no
    reclaim.
    """
    records: dict[str, Any] = {}
    unmaterialised: list[str] = []
    base = settings.workspace_base
    try:
        entries = sorted(base.iterdir())
    except FileNotFoundError:
        return records, unmaterialised
    except OSError as exc:
        logger.warning(
            "reconcile: cannot scan %s for sandbox trees: %s",
            base,
            exc,
            exc_info=True,
        )
        return records, unmaterialised
    for entry in entries:
        if not is_sandbox_workspace_dir(entry):
            continue
        try:
            record = runtime_registry.peek(entry.name)
        except Exception:
            # ``peek`` reads and parses the file: a tree whose sandbox.json is
            # JSON but not a usable record (e.g. ``{}``) must degrade to
            # "unreadable", never take the whole reconcile round down.
            logger.warning(
                "reconcile: cannot read the sandbox record of %s",
                entry,
                exc_info=True,
            )
            record = None
        if record is None:
            unmaterialised.append(entry.name)
        else:
            records[entry.name] = record
    return records, unmaterialised


class NodeAgent:
    """Periodically registers with the control plane and sends heartbeats."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_registry,
        control_plane_url: str | None,
        node_address: str | None,
        metrics_provider: Callable[[], dict[str, Any]] | None = None,
        port_provider: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self._settings = settings
        self._runtime_registry = runtime_registry
        self._control_url = (control_plane_url or "").rstrip("/")
        self._node_address = node_address or ""
        self._metrics_provider = metrics_provider
        #: N8: the MCP gateway port band's watermark, shipped with every
        #: heartbeat so the control plane's node view is the single place to
        #: read it (defaults to the process-wide pool when not injected).
        self._port_provider = port_provider or _mcp_port_stats
        #: E9.1: per-sandbox activity to ship with each heartbeat (registries
        #: without activity tracking simply report nothing).
        self._activity_provider = getattr(
            runtime_registry, "activity_snapshot", None
        )
        #: N25/L2b: measured per-sandbox tree sizes. Scanned on its own,
        #: slower cadence (``E2B_DISK_ENFORCE_INTERVAL_S``, 0 disables) and
        #: cached for every heartbeat in between: the walk is a few
        #: milliseconds per sandbox, but there is no reason to redo it five
        #: times a minute, and a heartbeat that finds nothing must not erase
        #: the control plane's view of what it learned a moment ago.
        self._disk_provider = getattr(runtime_registry, "disk_usage_snapshot", None)
        self._disk_interval_s = _disk_enforce_interval_s()
        self._disk_report: dict[str, int] = {}
        self._disk_report_at = 0.0
        self._node_id: str | None = None
        self._task: asyncio.Task | None = None
        #: The reconcile round currently running, if any. The round is its own
        #: task so the heartbeat never waits behind it (see ``_loop``), and it
        #: is single-flight: a trigger that arrives while one runs stays set
        #: (``_start_reconcile_if_due``) instead of stacking a second sweep over
        #: the same trees.
        self._reconcile_task: asyncio.Task | None = None
        # E6.1: set when the control plane may have missed this worker (first
        # start, heartbeat failures). The next successful heartbeat after a
        # registration runs a local-runtime reconciliation.
        self._reconcile_pending = True
        #: M1: heartbeat intervals still to wait before retrying a disk sweep
        #: that had to be deferred because the fleet's records could not all
        #: be enumerated (``None`` = no retry scheduled). ``_reconcile_retry_attempts``
        #: drives the capped doubling and is reported in the WARNING.
        self._reconcile_retry_in: int | None = None
        self._reconcile_retry_attempts = 0

    def start(self) -> None:
        if not self._control_url or not self._node_address:
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        """Heartbeat on its own cadence, with reconcile rounds running *beside*.

        The round used to run inline here, which made every heartbeat gap
        ``interval + round duration``: a round walks every tree on the shared
        base (``_scan_workspace_runtimes``), so its length grows with the fleet's
        history and has no bound. Two things followed from that, and both were
        real damage: a worker that was merely *busy* could go quiet past
        ``E2B_NODE_HEARTBEAT_TIMEOUT`` and have its live sandboxes reaped as
        orphans (E6.1, the N18 failure mode), and the window could not be lowered
        to the sensor it is meant to be (three missed beats) without risking
        exactly that -- which kept a *dead* node's capacity reserved for minutes.

        Detaching the round is what makes the window meaningful: the gap is the
        interval plus the registration round trip again, not the interval plus
        however long the sweep happens to take. Failures still flow the same way
        (a failed pulse or round sets ``_reconcile_pending``), and the round keeps
        its single-flight guard.
        """
        while True:
            try:
                await self._pulse()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Any failure here may mean the control plane did not hear this
                # worker, so the next successful heartbeat must reconcile.
                self._reconcile_pending = True
                logger.warning("node agent heartbeat failed", exc_info=True)
            await asyncio.sleep(5)

    async def _pulse(self) -> None:
        """One register-or-heartbeat exchange, then whatever round it triggers."""
        headers = {"X-Internal-Key": self._settings.internal_api_key}
        async with httpx.AsyncClient(timeout=10) as client:
            payload = _register_payload(self._settings)
            payload["address"] = self._node_address
            if self._node_id is None:
                resp = await client.post(
                    f"{self._control_url}/internal/nodes/register",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    self._node_id = resp.json().get("nodeID")
                    logger.info(
                        "registered node %s at %s", self._node_id, self._node_address
                    )
                    # A fresh registration is exactly the E6.1 trigger: the
                    # control plane may have orphaned what this worker still
                    # runs while it was away.
                    self._reconcile_pending = True
            else:
                resp = await client.post(
                    f"{self._control_url}/internal/nodes/{self._node_id}/heartbeat",
                    json=_heartbeat_usage_payload(
                        self._settings,
                        self._metrics_provider,
                        self._activity_provider,
                        self._port_provider,
                        self._disk_report_for_heartbeat(),
                    ),
                    headers=headers,
                )
                if resp.status_code == 404:
                    # The control plane lost us (e.g. it restarted);
                    # re-register on the next cycle.
                    self._node_id = None
        # Outside the pulse's client scope on purpose: the round owns its client
        # (this one is closed when the ``async with`` above exits, and a detached
        # round outlives it).
        self._start_reconcile_if_due()

    def _disk_report_for_heartbeat(self) -> dict[str, int]:
        """The last measured tree sizes, refreshed on its own cadence.

        Runs the (blocking, file-system) scan on the event loop's thread by
        design: it is a few milliseconds per sandbox with a hard scan budget
        (``disk_usage_snapshot(budget_s=...)``), and moving it to a thread
        would buy nothing but a race with the next heartbeat.
        """
        if self._disk_provider is None or self._disk_interval_s <= 0:
            return {}
        now = time.monotonic()
        if now - self._disk_report_at < self._disk_interval_s:
            return self._disk_report
        try:
            report = self._disk_provider(budget_s=_DISK_SCAN_BUDGET_S)
        except Exception:
            logger.warning("sandbox disk scan failed", exc_info=True)
            return self._disk_report
        self._disk_report = dict(report or {})
        self._disk_report_at = now
        return self._disk_report

    def _start_reconcile_if_due(self) -> None:
        """Start one reconcile round as its own task, if one is due.

        The triggers (``_reconcile_pending`` from a failed pulse or a fresh
        registration, and the deferred-sweep countdown) are only consumed once a
        round actually starts: while one is in flight this returns *before*
        asking ``_reconcile_due``, so a trigger that arrives mid-round is retried
        on a later heartbeat rather than dropped or run concurrently.
        """
        if self._node_id is None:
            # Nothing to reconcile against: a 404 heartbeat clears the id, and
            # the next registration re-arms ``_reconcile_pending``. Leaving the
            # trigger unconsumed is what keeps the recovery round from being
            # skipped.
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        if not self._reconcile_due():
            return
        self._reconcile_task = asyncio.create_task(self._reconcile_round())

    async def _reconcile_round(self) -> None:
        """Run one round with its own client, and report its summary.

        The client is per-round because the heartbeat loop closes the one it
        uses at the end of every pulse; a detached round cannot borrow it.
        """
        headers = {"X-Internal-Key": self._settings.internal_api_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                summary = await self._reconcile_with_control_plane(client, headers)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Same contract as a failed pulse: we may have missed the control
            # plane, so the next successful heartbeat reconciles again.
            self._reconcile_pending = True
            logger.warning("reconcile round failed", exc_info=True)
            return
        if summary:
            self._report_reconcile_summary(summary)

    def _stop_refused_runtime(self, sandbox_id: str) -> None:
        """Stop the runtime of a tree whose teardown was refused (W7 / C1-4).

        The refusal protects *files* -- the targets a contradicting record
        could aim at -- and nothing else. Leaving the runtime registered is
        what made a refused record end in "the control plane has forgotten the
        sandbox and the process tree is still running": the callbacks
        ``unregister`` fires are exactly the ones that shut the sandbox's
        process tree down (``kill_all`` in production). Runs on the event
        loop, where those callbacks belong.
        """
        try:
            self._runtime_registry.unregister(sandbox_id)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "reconcile: could not unregister the runtime of %s while "
                "refusing its tree",
                sandbox_id,
                exc_info=True,
            )

    def _report_reconcile_summary(self, summary: dict[str, Any]) -> None:
        """Make the reconcile summary visible to operators (L1).

        ``_loop`` used to drop the return value, so the fields that exist only
        in the summary -- ``disk_sweep_skipped`` and ``untrusted_records`` in
        particular -- were observable as WARNING text alone. Per-tree detail
        stays at WARNING; this is the positive, greppable record of a round
        that ran and what it decided.
        """
        logger.info(
            "reconcile summary: deleted=%d delete_failures=%d unmaterialised=%d "
            "protected_elsewhere=%d concurrent_creates=%d quota_cleaned=%d "
            "quota_unreclaimed=%d disk_sweep_skipped=[%s] untrusted_records=[%s]",
            len(summary["deleted"]),
            len(summary["delete_failures"]),
            len(summary["unmaterialised"]),
            len(summary["protected_elsewhere"]),
            len(summary["concurrent_creates"]),
            len(summary["quota_cleaned"]),
            len(summary["quota_unreclaimed"]),
            ",".join(summary["disk_sweep_skipped"]),
            ",".join(summary["untrusted_records"]),
        )

    def _reconcile_due(self) -> bool:
        """Whether this heartbeat should run the reconcile round.

        Two independent triggers: the pending flag (first start, or a failed
        heartbeat — the control plane may have orphaned our sandboxes), and
        the backoff scheduled by a round whose disk sweep the fleet
        enumeration blocked (M1: without it one non-enumerable record would
        silence the sweep until the process restarted).
        """
        if self._reconcile_pending:
            self._reconcile_pending = False
            return True
        if self._reconcile_retry_in is None:
            return False
        self._reconcile_retry_in -= 1
        if self._reconcile_retry_in > 0:
            return False
        self._reconcile_retry_in = None
        return True

    def _defer_sweep(self) -> None:
        """Schedule a retry for a disk sweep the enumeration blocked (M1).

        The retry backs off (doubling, capped at
        ``_RECONCILE_RETRY_MAX_INTERVALS`` heartbeats) so a persistent
        shortfall -- a record whose node never re-registers -- keeps being
        retried at a decaying rate instead of polling the whole fleet every
        heartbeat, and never goes silent.
        """
        self._reconcile_retry_attempts += 1
        delay = min(
            2 ** (self._reconcile_retry_attempts - 1),
            _RECONCILE_RETRY_MAX_INTERVALS,
        )
        self._reconcile_retry_in = delay
        logger.warning(
            "reconcile: disk sweep deferred by an incomplete fleet "
            "enumeration; retrying in %d heartbeat interval(s) (attempt %d)",
            delay,
            self._reconcile_retry_attempts,
        )

    def _sweep_completed(self) -> None:
        """Clear the deferred-sweep backoff after a round that did not skip."""
        self._reconcile_retry_in = None
        self._reconcile_retry_attempts = 0

    async def _reconcile_with_control_plane(self, client, headers) -> dict[str, Any]:
        """Reconcile this worker's local runtimes against the control plane
        after (re)registration (E6.1 recovery path).

        During a partition the control plane marks this node's sandbox
        records ``orphaned`` instead of deleting their workspaces. When the
        worker reconnects:

        * local runtimes the control plane no longer knows are torn down
          here (their records were deleted while we were unreachable) — but
          only runtimes that already existed when the control-plane snapshot
          was requested. A runtime registered *during* the reconcile window
          is a concurrent create that raced the snapshot; it is kept and
          reported back so the control plane never deletes its record;
        * "local runtimes" means the in-memory registry *plus* every
          ``sbx_*`` tree still on disk, re-materialised from its
          ``sandbox.json``. Without the disk side a worker restart made the
          control plane delete live records (the worker reported nothing) and
          left the trees, the ``sandbox.json`` files and their XFS quota rows
          behind forever — the fail-safe quota reconcile only ever reclaims
          rows whose tree is gone;
        * the remaining local ids are reported back together with the
          snapshot ids, so the control plane un-orphans the records it
          still has, removes records for sandboxes we no longer run (only
          ones that were in the snapshot), and leaves records created after
          the snapshot untouched.

        Three properties the round has to keep (review round 1):

        * a record without ``created_at`` -- the shape written before
          2026-09-02 -- takes its timestamp from the ``sandbox.json`` mtime
          instead of the moment it is read, otherwise every legacy tree looks
          like a create racing this round and is pinned forever (M2);
        * a disk sweep that cannot be fenced by a complete fleet enumeration
          is skipped, reported (``disk_sweep_skipped`` plus a WARNING) *and*
          retried on a backing-off schedule, so one non-enumerable record can
          neither hide the sweep forever nor turn it into a per-heartbeat
          poll (M1);
        * the sweep only tears down trees whose targets are read back from
          the disk, because ``sandbox.json`` is sandbox-writable input
          (M4): the directory is ``<workspace_base>/<id>``, the project id
          is the one the filesystem reports, and a record that disagrees with
          either is refused and reported (``untrusted_records``).

        Returns a summary of what the round did (see the ``summary`` dict at
        the end); callers may ignore it, tests and operators use it to tell a
        quiet round from a round that skipped work.
        """
        if not self._control_url or not self._node_id:
            return {}
        # Wall-clock boundary captured *before* the snapshot request. Any
        # local runtime registered after this point cannot have been in the
        # control-plane snapshot, so it must be a concurrent create.
        reconcile_started_at = time.time()
        try:
            resp = await client.get(
                f"{self._control_url}/internal/nodes/{self._node_id}/sandboxes",
                headers=headers,
            )
            resp.raise_for_status()
            known = set(resp.json().get("sandboxIDs") or [])
        except (httpx.HTTPError, ValueError):
            logger.warning(
                "reconcile: cannot fetch control-plane sandbox list",
                exc_info=True,
            )
            return {}
        in_memory = {r.sandbox_id: r for r in self._runtime_registry.list()}
        local = dict(in_memory)
        # The walk is one filesystem round trip per tree on the shared base, so
        # it belongs off the event loop: run there, it answered nothing -- file
        # API, commands, and its own heartbeat -- for the length of the scan
        # (N21). ``peek`` does not touch the in-memory registry, so a thread is
        # safe against concurrent creates.
        scanned, unmaterialised = await asyncio.to_thread(
            _scan_workspace_runtimes, self._settings, self._runtime_registry
        )
        for sandbox_id, record in scanned.items():
            local.setdefault(sandbox_id, record)
        concurrent_creates = {
            sandbox_id
            for sandbox_id, record in local.items()
            if record.created_at > reconcile_started_at
        }
        # A runtime this process registered itself is this worker's own
        # sandbox, so a node-local snapshot that no longer lists it means the
        # record was deleted while we were unreachable (E6.1 semantics).
        # Trees found only on disk are weaker evidence: every worker of the
        # production stack mounts the same workspace volume
        # (deploy/stack/docker-compose.prod.yml), so this worker sees the
        # other nodes' live sandboxes too and the node-local snapshot cannot
        # tell them apart from a true orphan.
        orphaned = {
            sandbox_id for sandbox_id in in_memory if sandbox_id not in known
        } - concurrent_creates
        disk_candidates = set(scanned) - known - concurrent_creates
        candidates = orphaned | disk_candidates
        deletable = orphaned
        protected_elsewhere: list[str] = []
        disk_sweep_skipped: list[str] = []
        if candidates:
            fleet_owned = await self._fleet_sandbox_ids(client, headers)
            if fleet_owned is None:
                # Fleet-wide ownership cannot be established: fall back to
                # the node-local semantics for runtimes this process owns and
                # leave the disk-only trees for a later round. The skip is
                # reported in the summary and the retry is scheduled with a
                # capped backoff (M1) so this state can never be permanent
                # and silent.
                disk_sweep_skipped = sorted(disk_candidates)
                logger.warning(
                    "reconcile: leaving %d orphan tree(s) on disk alone this "
                    "round (fleet record enumeration unavailable): %s",
                    len(disk_candidates),
                    ",".join(sorted(disk_candidates)),
                )
                self._defer_sweep()
            else:
                deletable = candidates - fleet_owned
                protected_elsewhere = sorted(candidates & fleet_owned)
        deleted: list[str] = []
        delete_failures: list[str] = []
        untrusted_records: list[str] = []
        reclaimable_projids: set[int] = set()
        for sandbox_id in sorted(deletable):
            # Every candidate is torn down from verified targets, in-memory
            # ones included: a record this process "owns" can have been
            # cached straight out of the sandbox-writable ``sandbox.json`` by
            # any request that called ``RuntimeRegistry.get()``, so it is no
            # more trustworthy than the disk copy (M4).
            try:
                # Same reasoning as the scan above: `lstat`/`resolve` per target
                # is filesystem work, and a round that can tear down many trees
                # would otherwise hold the loop for the whole verification pass.
                plan, reason = await asyncio.to_thread(
                    _verified_teardown_plan,
                    self._settings.workspace_base,
                    sandbox_id,
                    local.get(sandbox_id),
                    shared_volume_root=self._settings.shared_volume_root,
                )
            except Exception:
                # Verifying the targets must not cost the worker the rest of
                # the round (nor its heartbeat): an unverifiable tree is left
                # alone and reported like a mismatched record. Its runtime
                # goes with the other refusals (review W7 / C1-4).
                logger.warning(
                    "reconcile: cannot verify the teardown targets of %s; "
                    "leaving it alone",
                    sandbox_id,
                    exc_info=True,
                )
                self._stop_refused_runtime(sandbox_id)
                untrusted_records.append(sandbox_id)
                continue
            if plan is None:
                # A record that does not describe the tree it was found in is
                # not evidence of anything: leave the tree alone (it is this
                # sandbox's own directory) and say so. The runtime is not part
                # of the record's reach, so it still goes (review W7 / C1-4):
                # a refusal keeps files, never a process tree. The refusal is
                # not the end of the story -- see ``GET /agent/untrusted`` and
                # ``POST /agent/untrusted/{id}/park`` for the operator's
                # bounded, non-destructive exit (review W7 / W7-3).
                logger.warning(
                    "reconcile: leaving %s on disk: %s",
                    sandbox_id,
                    reason,
                )
                self._stop_refused_runtime(sandbox_id)
                untrusted_records.append(sandbox_id)
                continue
            projids = set(plan.expected_projids)
            logger.warning(
                "reconcile: removing orphan runtime %s (not in control plane)",
                sandbox_id,
            )
            try:
                # Unregistering is the teardown's first step, and its
                # callbacks are loop-side code: the production callback pops
                # the sandbox's runtime context and shuts it down, which
                # cancels the MCP gateway watch -- an ``asyncio.Task`` owned
                # by this loop (race A, review W1). Do it here, where the loop
                # is, and leave the heavy half to the worker thread below.
                self._runtime_registry.unregister(sandbox_id)
                # Per-tree teardown is a synchronous heavy step (an XFS
                # release over the quota agent, an rmtree of a whole
                # workspace), and a shared workspace can hold dozens of
                # trees: run it off the event loop so the heartbeat and the
                # worker's own API never stall behind the sweep (M3).
                await asyncio.to_thread(
                    _delete_sandbox_runtime,
                    self._settings,
                    self._runtime_registry,
                    sandbox_id,
                    plan=plan,
                    unregister=False,
                )
            except Exception:
                # One unrecoverable tree (a permission wall, a broken mount)
                # must not cost the worker the rest of the round: keep going
                # and still report the surviving runtimes to the control
                # plane below.
                logger.warning(
                    "reconcile: orphan runtime %s teardown failed; continuing",
                    sandbox_id,
                    exc_info=True,
                )
                delete_failures.append(sandbox_id)
                continue
            deleted.append(sandbox_id)
            reclaimable_projids |= projids
        if unmaterialised:
            logger.warning(
                "reconcile: %d sandbox tree(s) on disk have no readable "
                "sandbox.json and were left alone: %s",
                len(unmaterialised),
                ",".join(sorted(unmaterialised)),
            )
        if protected_elsewhere:
            logger.info(
                "reconcile: %d tree(s) on disk belong to another control-plane "
                "record; left alone: %s",
                len(protected_elsewhere),
                ",".join(protected_elsewhere),
            )
        remaining = (set(local) & known) | concurrent_creates
        quota_cleaned: list[int] = []
        quota_unreclaimed: list[int] = []
        try:
            resp = await client.post(
                f"{self._control_url}/internal/nodes/{self._node_id}/reconcile",
                json={
                    "sandboxIDs": sorted(remaining),
                    "snapshotIDs": sorted(known),
                },
                headers=headers,
            )
            resp.raise_for_status()
            result = resp.json()
            if concurrent_creates:
                logger.info(
                    "reconcile: kept %d concurrent create(s) started during "
                    "reconcile window: %s",
                    len(concurrent_creates),
                    ",".join(sorted(concurrent_creates)),
                )
            if result.get("recovered"):
                logger.info(
                    "reconcile: restored sandboxes %s",
                    ",".join(result["recovered"]),
                )
            if result.get("removed"):
                logger.info(
                    "reconcile: removed stale records %s",
                    ",".join(result["removed"]),
                )
        except (httpx.HTTPError, ValueError):
            logger.warning(
                "reconcile: control-plane record update failed",
                exc_info=True,
            )
        if reclaimable_projids or deleted:
            # The startup quota reconcile is scheduled before this worker has
            # even registered, so it usually ran while these trees were still
            # present (their sandbox.json kept the rows "recorded") and XFS
            # may not have dropped the released accounting yet. Reclaim the
            # rows of the trees this round removed, with a bounded retry.
            # ``reclaimable_projids`` can be empty (a tree with no project id
            # the worker can read back, or none at all): the pass still runs
            # once so those rows are reclaimed by the fail-safe reconcile as
            # soon as their tree is gone.
            quota_cleaned, quota_unreclaimed = await self._reclaim_quota_rows(
                reclaimable_projids
            )
        if not disk_sweep_skipped:
            # A round that reached the fleet's full record set clears the
            # deferred-sweep backoff; a deferred one keeps its schedule.
            self._sweep_completed()
        summary = {
            "deleted": sorted(deleted),
            "delete_failures": sorted(delete_failures),
            "unmaterialised": sorted(unmaterialised),
            "protected_elsewhere": protected_elsewhere,
            "disk_sweep_skipped": disk_sweep_skipped,
            "untrusted_records": sorted(untrusted_records),
            "concurrent_creates": sorted(concurrent_creates),
            "quota_cleaned": quota_cleaned,
            "quota_unreclaimed": quota_unreclaimed,
        }
        return summary

    async def _fleet_sandbox_ids(self, client, headers) -> set[str] | None:
        """Every sandbox id the control plane records, or ``None`` when that
        answer cannot be trusted.

        Used to fence the disk-side sweep: a tree is only reclaimable when no
        record anywhere in the fleet references it. The enumeration is only
        trusted when it accounts for *every* record — ``/internal/fleet/metrics``
        reports the fleet-wide record count, so a shortfall (a node missing
        from the node registry, e.g. right after a control-plane restart while
        the other workers have not re-registered yet) makes the caller skip
        the sweep instead of deleting someone else's live tree.
        """
        try:
            resp = await client.get(
                f"{self._control_url}/internal/nodes", headers=headers
            )
            resp.raise_for_status()
            node_list = resp.json()
            if not isinstance(node_list, list):
                raise ValueError("node list is not an array")
            node_ids = [
                str(node["nodeID"])
                for node in node_list
                if isinstance(node, dict) and node.get("nodeID")
            ]
            owned: set[str] = set()
            for node_id in node_ids:
                node_url = (
                    f"{self._control_url}/internal/nodes/"
                    f"{quote(node_id, safe='')}/sandboxes"
                )
                listed = await client.get(node_url, headers=headers)
                listed.raise_for_status()
                payload = listed.json()
                if not isinstance(payload, dict):
                    raise ValueError(
                        f"sandbox list for {node_id} is not an object"
                    )
                owned.update(payload.get("sandboxIDs") or [])
            metrics = await client.get(
                f"{self._control_url}/internal/fleet/metrics", headers=headers
            )
            metrics.raise_for_status()
            fleet_metrics = metrics.json()
            if not isinstance(fleet_metrics, dict):
                raise ValueError("fleet metrics is not an object")
            fleet_count = int(fleet_metrics.get("activeSandboxes") or 0)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            logger.warning(
                "reconcile: cannot enumerate the fleet's sandbox records",
                exc_info=True,
            )
            return None
        if len(owned) != fleet_count:
            logger.warning(
                "reconcile: fleet sandbox enumeration is incomplete "
                "(%d of %d records accounted for)",
                len(owned),
                fleet_count,
            )
            return None
        return owned

    async def _reclaim_quota_rows(
        self, projids: set[int]
    ) -> tuple[list[int], list[int]]:
        """Reclaim the XFS quota rows of the trees this round removed.

        Returns ``(cleaned, unreclaimed)``. The quota pass is best-effort: a
        failure here is reported, never raised, because the caller is in the
        middle of a recovery round that must still reach the control plane.
        """
        outstanding = set(projids)
        cleaned: set[int] = set()
        for attempt in range(1, _QUOTA_RECLAIM_ATTEMPTS + 1):
            try:
                result = await asyncio.to_thread(
                    reconcile_orphan_projects,
                    workspace_base=self._settings.workspace_base,
                    mount_point=self._settings.workspace_base,
                    via_agent=self._settings.quota_via_agent,
                )
                table = await asyncio.to_thread(
                    project_quota_table,
                    self._settings.workspace_base,
                    via_agent=self._settings.quota_via_agent,
                )
            except Exception:
                logger.warning(
                    "reconcile: quota reconciliation failed",
                    exc_info=True,
                )
                break
            cleaned |= {int(projid) for projid in result.get("cleaned") or []}
            # N12: "still in the table" is the question, not "did this pass
            # report it". The teardown itself now drops the row of every tree
            # this round removed (limits reset once the tree is gone), so those
            # projids are settled *without* ever appearing in ``cleaned`` --
            # judging by the pass's list reported them as unreclaimed and logged
            # a warning about rows that were already gone.
            outstanding = {projid for projid in outstanding if projid in table}
            if not outstanding:
                break
            if attempt < _QUOTA_RECLAIM_ATTEMPTS:
                await asyncio.sleep(_QUOTA_RECLAIM_DELAY_S)
        if outstanding:
            logger.warning(
                "reconcile: %d quota row(s) not reclaimed after %d attempt(s): %s",
                len(outstanding),
                _QUOTA_RECLAIM_ATTEMPTS,
                sorted(outstanding),
            )
        return sorted(cleaned), sorted(outstanding)

    async def stop(self) -> None:
        """Stop the heartbeat loop and any round it left running.

        The round is detached, so cancelling the loop alone would leave a sweep
        running through shutdown (touching the shared base while the process is
        tearing its state down).
        """
        for attribute in ("_task", "_reconcile_task"):
            task: asyncio.Task | None = getattr(self, attribute)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, attribute, None)


def _agent_create_sandbox(request: Request, settings: Settings, payload: dict) -> None:
    runtime_registry = request.app.state.runtime_registry
    workspace_base = settings.workspace_base
    sandbox_id = payload.get("sandboxID")
    if not sandbox_id:
        raise ValueError("sandboxID is required")
    workspace_dir = workspace_base / sandbox_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = payload.get("snapshotID")
    if snapshot_id:
        snapshot_fs = workspace_base / "_snapshots" / snapshot_id / "fs"
        if not snapshot_fs.is_dir():
            raise ValueError(f"Snapshot {snapshot_id} not found on this node")
        shutil.copytree(snapshot_fs, workspace_dir, dirs_exist_ok=True, symlinks=True)
    else:
        (workspace_dir / "workspace").mkdir(parents=True, exist_ok=True)
    volume_mounts = payload.get("volumeMounts") or []
    existing = runtime_registry.get(sandbox_id)
    # E3.2: allocate the sandbox's host uid before materializing volumes so
    # per-sandbox volume slices can be chowned to it. Only a root worker -- or
    # a non-root worker that resolved the file-capability brokers (Track F),
    # which is what makes the chown possible there -- can put a sandbox under
    # its own host uid; everything else keeps the fixed-uid + Landlock model
    # and never allocates.
    host_uid = None
    pool = getattr(runtime_registry, "uid_pool", None)
    from envd_service import priv_helpers

    if (
        settings.per_sandbox_uid
        and (os.geteuid() == 0 or priv_helpers.active_helpers() is not None)
        and pool is not None
    ):
        host_uid = pool.acquire(
            sandbox_id,
            preferred=existing.host_uid if existing is not None else None,
        )
    try:
        mount_paths, volume_projects = build_volume_mounts(
            sandbox_id=sandbox_id,
            volume_mounts=volume_mounts,
            shared_volume_root=settings.shared_volume_root,
            workspace_dir=workspace_dir,
            fallback_mount_point=settings.workspace_base,
            via_agent=settings.quota_via_agent,
            existing_volume_projects=(
                existing.volume_projects if existing is not None else []
            ),
            host_uid=host_uid,
        )
        disk_mb = int(payload.get("diskMB", settings.default_disk_mb))
        project_id = None
        if existing is not None:
            project_id = existing.project_id
        if xfs_project_supported(
            workspace_base, via_agent=settings.quota_via_agent
        )[0]:
            try:
                project_id = provision_project(
                    sandbox_id=sandbox_id,
                    project_dir=workspace_dir,
                    mount_point=workspace_base,
                    disk_mb=disk_mb,
                    via_agent=settings.quota_via_agent,
                    project_id=project_id,
                )
            except ProjectQuotaError as exc:
                logger.warning(
                    "XFS project quota setup failed for %s: %s",
                    sandbox_id,
                    exc,
                )
                # A failed provision may have cleared the project state
                # (project -C after a half-created setup), so the sandbox
                # must not keep a stale projid: persisting it would claim
                # quota is active while the files no longer belong to any
                # project, and delete would only leave an orphan quota table
                # entry.
                project_id = None
        if host_uid is not None:
            apply_sandbox_ownership(workspace_dir, host_uid)
        elif not settings.per_sandbox_uid:
            # FUP #6: legacy shared-uid shape under a root worker — every
            # sandlock shell runs as host uid 1000. The pure no-chroot
            # workspace is written directly by that identity (no supervisor
            # mediation tier), so a root-created workspace must be chowned
            # to it or the first command is EACCES; the image-rootfs chroot
            # shape's supervisor-mediated writes are unaffected. Per-sandbox
            # uid mode is handled above; non-root workers create the
            # workspace as their own RunAs identity.
            align_shared_uid_workspace(workspace_dir)
        runtime_registry.register(
            sandbox_id=sandbox_id,
            access_token=payload.get("accessToken", ""),
            workspace_dir=str(workspace_dir),
            env_vars=dict(payload.get("envVars") or {}),
            base_image=payload.get("baseImage"),
            host_uid=host_uid,
            memory_mb=int(
                payload.get("memoryMB", settings.default_memory_mb)
            ),
            cpu_percent=int(
                payload.get("cpuPercent", settings.default_cpu_percent)
            ),
            disk_mb=disk_mb,
            project_id=project_id,
            max_processes=int(
                payload.get("maxProcesses", settings.default_max_processes)
            ),
            allow_internet_access=bool(
                payload.get("allowInternetAccess", False)
            ),
            max_command_timeout=int(
                payload.get("maxCommandTimeout", 3600)
            ),
            mcp=payload.get("mcp"),
            network=payload.get("network"),
            allow_public_traffic=bool(
                payload.get("allowPublicTraffic", False)
            ),
            volume_mounts=mount_paths,
            volume_projects=volume_projects,
            iam_tokens=payload.get("iamTokens"),
        )
    except BaseException:
        # I3: any failure between acquire and register (invalid volume
        # mounts -> 400, quota/ownership errors -> 500) must return the
        # reserved uid to the pool instead of leaking a slot.
        if host_uid is not None and pool is not None:
            pool.release(sandbox_id)
        raise
    # I1: the record is durable — drop the cross-process reservation marker
    # so other workers can reuse the free set without seeing a stale hold.
    if host_uid is not None and pool is not None:
        pool.commit(sandbox_id)


async def _prime_runtime_context(request: Request, sandbox_id: str | None) -> None:
    """Build the sandbox's runtime context -- off the event loop.

    The context (and with it ``create_executor`` -> the image rootfs resolution)
    is otherwise created lazily by the sandbox's *first RPC*
    (``envd_service/rpc.py::_context``), i.e. inside the very request the official
    SDK bounds with its 60s ``request_timeout`` -- and on the event loop, so a slow
    resolve also stopped this worker's heartbeats for its duration. On a
    network-backed image cache that resolve is minutes (measured on Aliyun NAS
    2026-09-17: 61s to unpack a python-slim rootfs versus 0.26s onto local disk),
    which made a healthy node look dead and let E6.1 reap the sandbox mid-create
    (docs/task-backlog.md N18).

    Failure is not fatal here on purpose: this is an optimisation of the *first
    command*, and the create contract is unchanged -- if the image cannot be
    resolved now, the first RPC reports exactly what it reports today.
    """
    if not sandbox_id:
        return
    runtimes = request.app.state.runtimes
    if runtimes.get(sandbox_id) is not None:
        return
    registry = request.app.state.runtime_registry
    try:
        record = registry.get(sandbox_id)
    except Exception:  # pragma: no cover - defensive
        logger.exception("could not read the runtime record for %s", sandbox_id)
        return
    if record is None:
        return
    factory = request.app.state.context_factory
    try:
        ctx = await asyncio.to_thread(factory, record)
    except Exception:
        logger.warning(
            "could not prime the runtime context for %s: the first command will "
            "build it (and report its error) instead",
            sandbox_id,
            exc_info=True,
        )
        return
    runtimes[sandbox_id] = ctx


@router.post("/agent/sandboxes", status_code=201)
async def agent_create_sandbox(request: Request) -> Response:
    settings = request.app.state.settings
    # F6: auth gets its own try. It used to share one ``except PermissionError``
    # with provisioning, so an EPERM/EACCES *during* provisioning (e.g. a
    # root-owned cold shared volume) came back as 401 with an empty body and
    # the control plane could only report a bare "failed to provision: ".
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        # Deliberately body-less: same answer for a missing and a wrong key,
        # and never any hint about the expected value.
        return Response(status_code=401)
    try:
        payload = await request.json()
        _agent_create_sandbox(request, settings, payload)
        await _prime_runtime_context(request, payload.get("sandboxID"))
    except PermissionError as e:
        # A worker-side permission fault while provisioning, not an auth
        # failure: 500 with the reason so the control plane's
        # "failed to provision: <body>" names the real cause.
        logger.exception("agent create sandbox failed (permission)")
        return Response(status_code=500, content=str(e)[:500])
    except (ValueError, json.JSONDecodeError) as e:
        return Response(status_code=400, content=str(e))
    except Exception:
        logger.exception("agent create sandbox failed")
        return Response(status_code=500)
    return Response(status_code=201)


@router.delete("/agent/sandboxes/{sandbox_id}", status_code=204)
async def agent_delete_sandbox(
    sandbox_id: str,
    request: Request,
    keepFiles: bool = Query(default=False),
    keepVolumeSlices: bool = Query(default=False),
    force: bool = Query(default=False),
) -> Response:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    runtime_registry = request.app.state.runtime_registry
    try:
        _delete_sandbox_runtime(
            settings,
            runtime_registry,
            sandbox_id,
            keep_files=keepFiles,
            keep_volume_slices=keepVolumeSlices,
            force=force,
        )
    except SandboxTeardownRefused as exc:
        # The record the sandbox could rewrite disagrees with the disk, so
        # the record-derived targets were left alone (the runtime itself was
        # already stopped): answer with the reason instead of a 204 that says
        # a teardown happened (and instead of acting on the record). An
        # operator who wants the tree gone anyway sends force=true, which
        # reclaims it from the disk alone -- the bounded exit of this refusal
        # (review W7 / C1-3).
        return Response(status_code=409, content=str(exc))
    except SandboxTreeNotRemoved as exc:
        # The tree survived the in-process removal *and* the e2b-maint
        # fallback (review W7 / W7-4): the teardown did not happen, so it must
        # not be answered with a 204 that makes the control plane drop the
        # record of a tree that is still on the disk. 500 with the reason.
        logger.warning("agent delete %s failed: %s", sandbox_id, exc)
        return Response(status_code=500, content=str(exc))
    return Response(status_code=204)


@router.get("/agent/untrusted")
async def agent_list_untrusted(request: Request) -> Response:
    """The trees this worker refuses to tear down, with the reason (W7-3).

    The refusal branch of the orphan-tree GC keeps a contradicting tree's
    files and only *reports* it (``reconcile: leaving <id> on disk: <reason>``,
    the ``untrusted_records`` field of the reconcile summary). Once the control
    plane has released the sandbox record -- an eviction, a TTL, a kill while
    this worker was unreachable -- there is no API ``force`` left to reclaim
    it with, because the record is what named the node. This listing is the
    positive answer: an operator (or the control plane) can ask a worker what
    it is holding back, and ``POST /agent/untrusted/{id}/park`` is the bounded
    exit. Internal key only, like every other agent route.
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    entries = _untrusted_workspace_trees(
        settings, request.app.state.runtime_registry
    )
    return JSONResponse({"untrusted": entries})


@router.post("/agent/untrusted/{sandbox_id}/park")
async def agent_park_untrusted(sandbox_id: str, request: Request) -> Response:
    """Move a refused tree out of the sandbox namespace; never delete it.

    The bounded exit of review W7 / W7-3. ``<base>/<id>`` is renamed to
    ``<base>/_untrusted.trees/<id>`` (a name that cannot be a sandbox id, so
    it is out of every workspace scan and can never be created inside a live
    sandbox's workspace), a ``.reason`` marker is written next to it, and the
    project id *the disk reports* is released -- so the quota row the tree's
    own record pinned becomes reclaimable by the fail-safe reconcile. The
    payload is kept: this action exists because the tree may be holding
    another tenant's data behind a rewritten record, and destroying it is
    exactly what the refusal is for.

    * ``404``: ``sandbox_id`` is not a tree this worker refuses (see
      ``GET /agent/untrusted``) -- including a healthy tree, which is never
      parked.
    * ``409``: the tree could not be moved (a mount point, a filesystem
      error); nothing was touched.
    * ``200``: the tree is parked; the body names the project id released.
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    entry, reason = _untrusted_entry_for(
        settings, request.app.state.runtime_registry, sandbox_id
    )
    if entry is None:
        return Response(status_code=404, content=reason)
    projid, error = _park_refused_tree(
        settings, request.app.state.runtime_registry, sandbox_id, entry["reason"]
    )
    if error is not None:
        return Response(
            status_code=409,
            content=f"cannot park {sandbox_id}: {error}",
        )
    return JSONResponse(
        {
            "sandbox_id": sandbox_id,
            "reason": entry["reason"],
            "parked_at": f"{UNTRUSTED_TREE_DIR}/{sandbox_id}",
            "released_project_id": projid,
        }
    )


def _agent_set_paused(
    request: Request, sandbox_id: str, *, paused: bool
) -> Response:
    """Freeze/thaw one sandbox's running exec children on this worker.

    Delivery counterpart of the control-plane pause/resume endpoints (FUP
    G1a): a separated worker never shares the control plane's runtime
    registry, so the control plane asks the hosting agent directly. The
    handler mirrors the internal-key auth and shape of the network/delete
    agent routes.

    * Runtime missing -> 404: the control plane keeps its own state
      bookkeeping and treats this as "no live runtime to freeze/thaw".
    * No live context (nothing launched yet) -> 204: there is no process
      tree to stop/continue.
    * Idempotent: ``ctx.pause()``/``ctx.resume()`` delegate to
      ``ProcessManager.pause_all/resume_all``, which are no-ops with no
      running children, matching the combined (shared-registry) path.
    * No new-command gating: pause freezes the currently running command
      groups; it does not gate future execs (parity with the combined
      deployment).

    Any JSON body is accepted and ignored for symmetry with the other agent
    routes (none is needed).
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        return Response(status_code=404)
    ctx = request.app.state.runtimes.get(sandbox_id)
    if ctx is not None:
        if paused:
            ctx.pause()
        else:
            ctx.resume()
    logger.info(
        "agent %s sandbox %s (%s)",
        "pause" if paused else "resume",
        sandbox_id,
        "live context" if ctx is not None else "no live context",
    )
    return Response(status_code=204)


@router.post("/agent/sandboxes/{sandbox_id}/pause", status_code=204)
async def agent_pause_sandbox(sandbox_id: str, request: Request) -> Response:
    """Freeze the sandbox's running exec child groups on this worker."""
    return _agent_set_paused(request, sandbox_id, paused=True)


@router.post("/agent/sandboxes/{sandbox_id}/resume", status_code=204)
async def agent_resume_sandbox(sandbox_id: str, request: Request) -> Response:
    """Thaw the sandbox's paused exec child groups on this worker."""
    return _agent_set_paused(request, sandbox_id, paused=False)


@router.get("/agent/images/{image:path}/warm")
async def agent_image_warm_peek(image: str, request: Request) -> Response:
    """Return ``{cached, digest}`` for a base image without extracting it.

    Used by the control plane to decide fast path (image cached -> create
    directly) vs slow path (image cold -> require ``X-Sandbox-Id`` and warm
    before creating the sandbox record).
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    if not _executor_needs_images(settings.executor):
        return JSONResponse({"cached": True, "required": False, "digest": None})
    return JSONResponse(
        peek_image_warm(
            image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
            credential_host=settings.image_registry_host,
        )
    )


@router.post("/agent/images/{image:path}/warm")
async def agent_image_warm_now(image: str, request: Request) -> Response:
    """Ensure a base image rootfs is extracted (idempotent, may take long)."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    if not _executor_needs_images(settings.executor):
        return JSONResponse({"cached": True, "required": False, "digest": None})
    try:
        rootfs = await asyncio.to_thread(
            resolve_image_rootfs,
            image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
            credential_host=settings.image_registry_host,
        )
    except Exception as e:
        logger.warning("agent warm failed for %s: %s", image, e)
        return Response(status_code=500, content=str(e)[:500])
    return JSONResponse({"cached": True, "rootfs": str(rootfs)})


def _executor_needs_images(mode: str) -> bool:
    """Whether this worker's executor resolves image rootfs at all."""
    if mode == "local":
        return False
    if mode == "sandlock":
        return True
    # auto: images only when the sandlock executor will actually run (the
    # factory falls back to local for a *missing* package and for Landlock
    # ABI < 6). An installed-but-unusable package is NOT one of those fallback
    # cases -- the factory fails closed on it -- so answering "no images"
    # here would hide the reason behind a missing rootfs (B1 fix round 2).
    try:
        import sandlock  # noqa: F401

        return sandlock.landlock_abi_version() >= 6
    except ModuleNotFoundError as exc:
        # Only a missing *top-level* package is the fallback case: a
        # half-upgraded tree ("No module named 'sandlock.exceptions'") is
        # installed-but-broken and must fail closed (B1 fix round 3).
        if sandlock_not_installed(exc):
            return False
        raise sandlock_unusable_error(
            "auto", sandlock_failure_detail(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001 - classified right here
        raise sandlock_unusable_error("auto", sandlock_failure_detail(exc)) from exc


@router.post("/agent/sandboxes/{sandbox_id}/network", status_code=204)
async def agent_update_sandbox_network(
    sandbox_id: str,
    request: Request,
) -> Response:
    """Apply a control-plane network update to a live sandbox runtime.

    D4=A: validation runs before either record copy is mutated. A launched
    instance accepts only expressible (monotone ip-only narrowing) updates;
    a rejection returns HTTP 409 with the stable ``{"code": 409, "message"}``
    body and leaves ``runtime.network`` / ``runtime.allow_internet_access``
    untouched, so the control plane can refuse to persist it.
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
        payload = await request.json()
    except PermissionError:
        return Response(status_code=401)
    except json.JSONDecodeError:
        return Response(status_code=400, content="Invalid JSON body")
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        return Response(status_code=404)
    if not isinstance(payload, dict):
        return Response(status_code=400, content="Request body must be an object")
    network = payload.get("network")
    if network is not None and not isinstance(network, dict):
        return Response(status_code=400, content="network must be an object")
    merged_network = dict(network) if network else None
    allow_internet = payload.get("allowInternetAccess")
    allow_public = payload.get("allowPublicTraffic")
    ctx = request.app.state.runtimes.get(sandbox_id)
    updater = getattr(ctx, "update_network", None) if ctx is not None else None
    if updater is not None:
        from gateway_common.network import NetworkUpdateConflictError

        try:
            # Validates + applies first; ``ctx.record`` is the runtime record
            # object, so ``runtime.network`` persists only on success.
            updater(merged_network)
        except NetworkUpdateConflictError as exc:
            logger.warning(
                "agent network update rejected for sandbox %s: %s",
                sandbox_id,
                exc,
            )
            return JSONResponse(
                status_code=409,
                content={"code": 409, "message": str(exc)},
            )
    else:
        # No live runtime context -> no instance launched: the update is
        # applicable and simply becomes the static policy the future
        # instance is built with.
        runtime.network = merged_network
    if isinstance(allow_internet, bool):
        runtime.allow_internet_access = allow_internet
    if isinstance(allow_public, bool):
        runtime.allow_public_traffic = allow_public
    logger.info(
        "agent network update for sandbox %s: %s",
        sandbox_id,
        runtime.network,
    )
    return Response(status_code=204)


@router.get("/agent/sandboxes/{sandbox_id}/export")
async def agent_export_sandbox(sandbox_id: str, request: Request) -> Response:
    """Stream a tar.gz of the sandbox workspace for filesystem migration."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    workspace = settings.workspace_base / sandbox_id
    if not workspace.is_dir():
        return Response(status_code=404)
    migrate_dir = settings.workspace_base / "_migrate"
    migrate_dir.mkdir(parents=True, exist_ok=True)
    tar_path = migrate_dir / f"{sandbox_id}.tar.gz"
    try:
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(workspace, arcname=".", recursive=True)
    except OSError:
        return Response(status_code=500)

    def _stream():
        try:
            with open(tar_path, "rb") as f:
                while chunk := f.read(64 * 1024):
                    yield chunk
        finally:
            tar_path.unlink(missing_ok=True)

    return StreamingResponse(_stream(), media_type="application/gzip")


@router.post("/agent/sandboxes/{sandbox_id}/import", status_code=204)
async def agent_import_sandbox(sandbox_id: str, request: Request) -> Response:
    """Restore a sandbox workspace from a raw tar.gz body."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    body = await request.body()
    if not body:
        return Response(status_code=400, content="Upload body is empty")
    workspace = settings.workspace_base / sandbox_id
    if workspace.exists():
        # Retry-friendly: a previous failed migration may have left partial
        # files; the incoming archive is the full source of truth.
        from envd_service import priv_helpers

        priv_helpers.remove_tree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    migrate_dir = settings.workspace_base / "_migrate"
    migrate_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = migrate_dir / f"{sandbox_id}.tar.gz"
    try:
        tmp_path.write_bytes(body)
        logger.info(
            "import %s: received %d bytes",
            sandbox_id,
            len(body),
        )
        _extract_sandbox_archive(tmp_path, workspace)
    except (OSError, tarfile.TarError) as e:
        logger.warning("import %s failed: %s", sandbox_id, e, exc_info=True)
        return Response(status_code=400, content="Invalid tar archive")
    finally:
        tmp_path.unlink(missing_ok=True)
    if not settings.per_sandbox_uid:
        # FUP #6: archive extraction with the ``data`` filter drops uid/gid
        # metadata, so the imported workspace is root-owned again — re-align
        # it to the legacy shared RunAs identity the same way create does.
        # With per-sandbox uids the follow-up agent create allocates and
        # chowns to the sandbox's own host uid instead.
        align_shared_uid_workspace(workspace)
    return Response(status_code=204)


@router.get("/agent/sandboxes/{sandbox_id}/logs")
async def agent_sandbox_logs(sandbox_id: str, request: Request) -> Response:
    """Return the sandbox's command output log (JSON list)."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    log_path = settings.workspace_base / sandbox_id / "command-logs.jsonl"
    if not log_path.is_file():
        return JSONResponse(content=[])
    entries: list[dict[str, Any]] = []
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return Response(status_code=500)
    return JSONResponse(content=entries)


@router.get("/agent/health")
async def agent_health(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    payload = _register_payload(settings)
    payload["nodeID"] = os.getenv("E2B_NODE_ID")
    return payload


@router.post("/agent/snapshots", status_code=201)
async def agent_create_snapshot(request: Request) -> Response:
    settings = request.app.state.settings
    # Same F6 shape as the create-sandbox route: a PermissionError from
    # ``copytree`` below is a worker-side EPERM/EACCES, not a bad key.
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    try:
        body = await request.json()
        snapshot_id = body.get("snapshotID")
        sandbox_id = body.get("sandboxID")
        if not snapshot_id or not sandbox_id:
            return Response(status_code=400, content="snapshotID and sandboxID required")
        src = settings.workspace_base / sandbox_id
        dst = settings.workspace_base / "_snapshots" / snapshot_id / "fs"
        if not src.is_dir():
            return Response(status_code=404, content=f"Sandbox {sandbox_id} not found")
        if dst.exists():
            return Response(status_code=409, content="snapshot already exists")
        shutil.copytree(src, dst, symlinks=True)
    except PermissionError as e:
        logger.exception("agent create snapshot failed (permission)")
        return Response(status_code=500, content=str(e)[:500])
    except Exception:
        logger.exception("agent create snapshot failed")
        return Response(status_code=500)
    return Response(status_code=201)


@router.delete("/agent/snapshots/{snapshot_id}", status_code=204)
async def agent_delete_snapshot(snapshot_id: str, request: Request) -> Response:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    shutil.rmtree(
        settings.workspace_base / "_snapshots" / snapshot_id, ignore_errors=True
    )
    return Response(status_code=204)
