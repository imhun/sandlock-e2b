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
import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from envd_service.agent_fileops import AgentFileOpsError
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
from envd_service.runtime.checkpoint_store import (
    capture_checkpoint_image,
    checkpoint_status,
    list_checkpoint_stores,
    record_restore_outcome,
    remove_checkpoint_images,
    remove_orphan_checkpoint_stores,
    restore_checkpoint_image,
    resume_sandbox,
)
from envd_service.runtime.cpu_activity import CpuActivityTracker, sample_cpu_ticks
from envd_service.runtime.context import mcp_port_stats as _mcp_port_stats
from envd_service.uid_pool import (
    align_shared_uid_workspace,
    apply_sandbox_ownership,
)
from envd_service.worker_identity import (
    reported_container_id,
    worker_identity_fields,
    worker_pid_namespace,
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
from gateway_common import create_trace
from gateway_common.archive import ArchiveRefusal, extract_sandbox_archive
from gateway_common.paths import (
    CHECKPOINT_ROOT_NAME,
    SNAPSHOT_PAYLOAD_DIR_NAME,
    SNAPSHOT_PAYLOAD_TAR_NAME,
    UNTRUSTED_TREE_DIR,
    is_reserved_platform_namespace,
    is_sandbox_workspace_dir,
    migrate_staging_dir,
    sandbox_checkpoint_dir,
    sandbox_command_log_path,
    sandbox_creating_marker,
    sandbox_node_runtime_dir,
    sandbox_runtime_dir,
    snapshot_payload_dir,
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


def _agent_fileops():
    """The C3 file-operation client, or ``None`` in every other shape.

    One helper for the call sites that were broker-first before Task 4: in the
    agent shape every privileged file step is asked of the control plane as
    ``{sandbox_id, op}`` (no path and no uid -- hard rules 1/3), and in every
    other shape this is ``None`` and the pre-C3 path runs unchanged.
    """
    from envd_service import agent_fileops

    return agent_fileops.active()


def _lexists(path: Path) -> bool:
    """``True`` for anything on that path, including a dangling symlink."""
    return path.exists() or path.is_symlink()


def _remove_agent_half(
    client,
    sandbox_id: str,
    *,
    path: Path,
    op: str,
    what: str,
) -> None:
    """Remove one half of a teardown through the agent, idempotently (D19).

    ``e2b-maint rm`` hard-refuses a path that is not there (``realpath`` →
    NULL), while a *teardown* has to stay idempotent: the control plane retries
    deletes, and a sandbox that never materialised its runtime directory has
    nothing to remove in the first place. So "already absent" is **success** --
    said out loud in the log, never inferred -- and everything else is
    fail-closed and named:

    * a refusal that leaves the path in place (a permission or IO error) is
      re-raised as :class:`SandboxTreeNotRemoved`, so the endpoint answers 500
      with the reason instead of a 204 that makes the control plane drop the
      record of a tree still on the disk;
    * the post-check is the disk's answer, not the agent's: a return that
      leaves the path behind is a failure too.

    The re-check after a refusal also covers the race where the path disappears
    between the pre-check and the agent's ``rm`` -- the one case where the
    refusal is correct about the mechanism and wrong about the outcome.

    ⚠ **Recorded narrowness** (C3 Task 4 second review, N5): the absence this
    checks is the *worker's* path, while the removal the agent performs is on
    the path the *control plane* derived. A deployment whose two bases disagree
    (``E2B_WORKSPACE_BASE``/``E2B_STATE_BASE`` named differently on the CP and
    the worker) could therefore report success here while the tree still exists
    where the CP looks. It is not a security hole -- the CP's derivation is
    still the one that is (not) executed -- and a divergence of that kind is
    already visible elsewhere (the disk report never sees the tree the worker
    measures, and the sandbox's own files disappear from every API). Recording
    it rather than adding a second round trip per teardown; the final review may
    want a config-agreement check at registration instead.
    """
    if not _lexists(path):
        logger.info(
            "agent delete %s: the %s of %s is already absent; nothing to remove",
            sandbox_id,
            what,
            sandbox_id,
        )
        return
    try:
        getattr(client, op)(sandbox_id)
    except Exception as exc:
        if not _lexists(path):
            logger.info(
                "agent delete %s: the %s of %s was already absent (%s: %s); "
                "nothing to remove",
                sandbox_id,
                what,
                sandbox_id,
                type(exc).__name__,
                exc,
            )
            return
        raise SandboxTreeNotRemoved(
            f"the {what} of {sandbox_id} could not be removed ({path}): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if _lexists(path):
        raise SandboxTreeNotRemoved(
            f"the {what} of {sandbox_id} survived its teardown: {path} is "
            "still on disk"
        )


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


def _register_payload(
    settings: Settings, node_id: str | None = None
) -> dict[str, Any]:
    """The registration/heartbeat payload.

    ``node_id`` lets an embedder (the test harness) declare a stable node id
    instead of relying on ``E2B_NODE_ID`` in the process environment; production
    leaves it ``None`` and reads the env, exactly as before.
    """
    payload = {
        "nodeID": node_id or os.getenv("E2B_NODE_ID"),
        "address": os.getenv("E2B_NODE_ADDRESS"),
        "images": [i for i in (settings.base_image,) if i],
        "labels": {
            "node-type": os.getenv("E2B_NODE_TYPE") or _node_type(),
        },
        **_node_resources(settings),
    }
    # C3 Task 3 (ruling D9.3): the worker's own pid namespace identity. The
    # control plane stores it and hands it to the agent, which is what makes the
    # container-pid → host-pid lookup unambiguous when one host runs several
    # workers. Absent on a platform without ``/proc/self/ns/pid`` (a macOS dev
    # box): the control plane then refuses grants for this node by name instead
    # of matching on the pid alone.
    pid_namespace = worker_pid_namespace()
    if pid_namespace:
        payload["pidNamespace"] = pid_namespace
    # C3 Task 4 / ruling D25: the container identity the *file operations*'
    # anchor is matched against -- the worker's hostname, which the runtime sets
    # to (a prefix of) the container id and which the agent can find in the
    # worker's host-side cgroup path (world-readable; face B needs no
    # ``CAP_SYS_PTRACE`` and no uid change to read that). Absent when this
    # platform has no container identity, or when a deployment overrode
    # ``hostname:`` -- the control plane then refuses those operations by name.
    #
    # F2: reported only by the shape whose anchor it *is* (compose). The k8s
    # lane verifies the worker's identity from the pod spec, so its worker skips
    # the probe -- its pod-name hostname can never be a container id, and
    # warning about that told the operator a falsehood ("every C3 file operation
    # will be refused") about a node where they all succeed.
    container_id = reported_container_id()
    if container_id:
        payload["containerID"] = container_id
    # C3 Task 4: the worker's own uid/gid, for the same reason (and on the same
    # path): the agent's file operations need the identity a tree's group and
    # ``--worker`` refer to, and the control plane may only take it from its own
    # records (hard rule 3).
    payload.update(worker_identity_fields())
    return payload


def _disk_enforce_interval_s() -> float:
    """How often the worker rewalks sandbox trees for the disk report.

    ``E2B_DISK_ENFORCE_INTERVAL_S`` (default 1 s); ``0`` disables the report
    entirely, which turns the control plane's measured-disk gate off with it.

    The interval is when the next *drain* starts, and with dirty-directory
    accounting a round costs what changed rather than what exists (measured:
    2.34 ms to re-check one reported directory, against 1043.9 ms for a
    whole-tree walk of 400 directories). It is no longer the bound on how
    *fast* an over-budget sandbox is acted on: a sandbox that crosses its
    budget is reported out of band the moment the round sees it
    (``_report_budget_crossings``), and the periodic report is for everything
    else -- the metric, and drift.
    """
    from gateway_common.env import env_float

    return env_float("E2B_DISK_ENFORCE_INTERVAL_S", 1.0)


def _disk_enforce_dirty_enabled() -> bool:
    """Whether the report is built incrementally (N25/L2c).

    ``E2B_DISK_ENFORCE_DIRTY`` (default **off**): with it on, the worker asks
    each sandbox's mediator which directories changed and re-walks only those,
    instead of walking every tree; with it off, or when a sandbox cannot answer
    (no live session, an older wheel, the pure shape), it walks the tree
    exactly as before. Off by default because the two must produce the same
    number -- the flag is what makes "they do" a measurable claim rather than a
    promise, and the accounting turns it on only after that measurement.
    """
    from gateway_common.env import env_bool

    return env_bool("E2B_DISK_ENFORCE_DIRTY", False)


def _cpu_activity_interval_s() -> float:
    """How often the worker samples per-sandbox CPU (E9.1 blind spot 2).

    ``E2B_CPU_ACTIVITY_INTERVAL_S`` (default 5 s = the heartbeat's own cadence);
    ``0`` disables the sampling, which puts a CPU-bound sandbox back in the
    "looks empty" bucket -- see `docs/resource-contention.md` §6 for what that
    costs.
    """
    from gateway_common.env import env_float

    return env_float("E2B_CPU_ACTIVITY_INTERVAL_S", 5.0)


def _cpu_activity_percent() -> float:
    """Percent of one core that counts as "this sandbox is working".

    ``E2B_CPU_ACTIVITY_PERCENT`` (default 5). A delta of *any* size is not
    activity -- a process that wakes once a minute has one -- and treating it as
    activity would make every sandbox un-evictable.
    """
    from gateway_common.env import env_float

    return env_float("E2B_CPU_ACTIVITY_PERCENT", 5.0)


#: MiB, the unit both sides of the platform's checkpoint account are written in.
_MIB = 1024 * 1024


def _measure_platform_account(workspace_base, state_base=None) -> dict[str, int]:
    """The platform's checkpoint account as heartbeat wire values (S2/D3).

    Two numbers, because a budget without its usage says nothing and usage
    without a budget reads as "unlimited". ``0`` for the budget is the honest
    encoding of unlimited (``E2B_PLATFORM_DISK_MB`` defaults to it), not a
    missing value. An *unmeasurable* usage is the other case and is **omitted**
    (see below): a missing usage field is "no update", a 0 would be a claim.
    """
    from envd_service.runtime.platform_disk import (
        measure_platform_disk_bytes,
        platform_budget_bytes,
    )

    used = measure_platform_disk_bytes(workspace_base, state_base=state_base)
    budget = platform_budget_bytes()
    payload = {"platformDiskBudgetMB": budget // _MIB}
    if used is not None:
        payload["platformDiskUsedMB"] = used // _MIB
    # When the account could not be measured the key is **omitted**, not sent as
    # 0 (C3 Task 4 third review, I-3): the control plane's ``update_usage``
    # treats a missing field as "no update" and keeps the number it already has,
    # while a 0 would assert "the platform stores nothing" -- the fail-open this
    # account exists to prevent. The worker-side reason is logged by
    # ``platform_disk`` where the unreadable tree is named.
    return payload


def _heartbeat_usage_payload(
    settings: Settings,
    metrics_provider: Callable[[], dict[str, Any]] | None = None,
    activity_provider: Callable[[], dict[str, float]] | None = None,
    port_provider: Callable[[], dict[str, int]] | None = None,
    disk_report: dict[str, int] | None = None,
    platform_disk: dict[str, int] | None = None,
    pid_namespace: str | None = None,
    worker_identity: dict[str, int] | None = None,
    container_id: str | None = None,
) -> dict[str, Any]:
    """Disk usage + quota alerts + MCP port band carried by each heartbeat."""
    payload: dict[str, Any] = {}
    if pid_namespace:
        # C3 Task 3: refreshed with every heartbeat, because a restarted worker
        # container is a *new* pid namespace under the same node id -- without
        # this the control plane would keep handing the agent the dead inode and
        # every slot grant on this node would be refused until it was forgotten
        # (the "node pinned" failure mode §11.1 item 9 warns about).
        payload["pidNamespace"] = pid_namespace
    if worker_identity:
        # C3 Task 4: refreshed with every heartbeat, exactly as the pid
        # namespace is -- a restart under a different ``runAsGroup`` is a new
        # gid under the same node id, and a stale one would put sandbox trees in
        # a group the worker does not have.
        payload.update(worker_identity)
    if container_id:
        # D25: refreshed with every heartbeat for the same reason the pid
        # namespace is -- a recreated worker container is a *new* container id
        # under the same node id, and a stale one would make the agent match a
        # cgroup that no longer exists (every file operation refused by name).
        payload["containerID"] = container_id
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
    if platform_disk:
        # S2/D3: the platform's own checkpoint account (``E2B_PLATFORM_DISK_MB``,
        # 0 = unlimited). It is deliberately *not* a per-sandbox number: the
        # images are the deployment's, billed to nobody's ``diskMB``, and this is
        # what makes them visible to the control plane instead of invisible.
        # Absent until the first measurement, exactly like the report above.
        payload.update(platform_disk)
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
    base = _registry_workspace_base(runtime_registry, settings)
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
        # The platform's files for this sandbox are evidence for whoever has to
        # review the quarantine, and they describe a tree that is no longer in
        # the workspace scan. Move them in beside it -- keeping the record and
        # the payload together, which is what this path (review W7 / W7-3)
        # always did, just from two places now instead of one -- the *record*
        # from the state base (N27) and the payload from the workspace base.
        # ``os.replace`` is a rename, not a copy: the two bases are directories
        # of one export under the committed shape, and a deployment that gave
        # the state base a mount of its own would get the warning below rather
        # than a half-moved tree.
        runtime_dir = sandbox_runtime_dir(
            base,
            sandbox_id,
            state_base=_registry_state_base(runtime_registry, settings),
        )
        if runtime_dir.is_dir():
            for entry in runtime_dir.iterdir():
                try:
                    os.replace(entry, dest / entry.name)
                except OSError:  # pragma: no cover - best effort
                    logger.warning(
                        "park: %s: could not move %s into the quarantine",
                        sandbox_id,
                        entry,
                        exc_info=True,
                    )
            try:
                runtime_dir.rmdir()
            except OSError:  # pragma: no cover - defensive
                pass
        # The node-local half of the same directory (N57 / Task 4) is *not*
        # evidence of anything -- it holds the create's marker and its
        # regenerable ``statfs`` seed -- so it is dropped rather than moved,
        # and it is dropped here for the same reason the shared half is: a
        # parked tree leaves nothing behind that describes a sandbox nobody can
        # act on. Its reader (this node's slot/worker) is gone with the tree.
        try:
            _node_runtime_dir(settings, sandbox_id).rmdir()
        except OSError:  # pragma: no cover - absent, or not empty
            pass
        # The checkpoint images are evidence of the same kind and live in their
        # own store (``_runtime/.checkpoints/<id>`` -- the sandbox's slot is what
        # writes them, so they cannot sit under the worker-owned runtime dir).
        # They move in beside the record rather than staying in the live store,
        # where nothing would describe them any more.
        images = sandbox_checkpoint_dir(
            base,
            sandbox_id,
            state_base=_registry_state_base(runtime_registry, settings),
        )
        if images.is_dir():
            try:
                os.replace(images, dest / CHECKPOINT_ROOT_NAME)
            except OSError:  # pragma: no cover - best effort
                logger.warning(
                    "park: %s: could not move its checkpoint images into the "
                    "quarantine",
                    sandbox_id,
                    exc_info=True,
                )
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


def _registry_state_base(runtime_registry, settings: Settings) -> Path:
    """Base the registry keeps this sandbox's *platform* files under.

    Same argument as :func:`_registry_workspace_base`, and it is what keeps the
    pair together: the runtime record, the command log and the checkpoint
    images are addressed where the registry that read the record wrote them.
    The two answers differ only for a caller that handed the app a registry of
    its own on a base the settings do not name; in every deployment shape the
    registry's state base *is* ``settings.state_base``.
    """
    state = getattr(runtime_registry, "state_base", None)
    return Path(state) if state is not None else Path(settings.state_base)


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
            agent_fileops_client = _agent_fileops()
            if agent_fileops_client is not None:
                # C3 Task 4: in the agent shape the removal is the agent's
                # (``e2b-maint rm`` over the tree the control plane derived
                # from the same records). The worker asks with
                # ``{sandbox_id, op}`` -- no path -- and the disk's own answer
                # decides (including "already absent" = success, D19).
                _remove_agent_half(
                    agent_fileops_client,
                    sandbox_id,
                    path=workspace_dir,
                    op="remove_workspace",
                    what="tree",
                )
            else:
                priv_helpers.remove_tree(workspace_dir, on_error="raise")
        except SandboxTreeNotRemoved:
            # Already named by the helper: re-wrap below would only hide the
            # reason behind a second layer of the same sentence.
            raise
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
        # The platform's files live next to the tree now (``_runtime/<id>/``),
        # so they are removed with it -- paired收尾, the N12/N24 lesson: the
        # record is what the *next* delete verifies against, so it must not
        # outlive the tree it describes (nor be dropped while the tree stands,
        # which is why ``unregister`` leaves it alone).
        runtime_dir = sandbox_runtime_dir(
            _registry_workspace_base(runtime_registry, settings),
            sandbox_id,
            state_base=_registry_state_base(runtime_registry, settings),
        )
        agent_fileops_client = _agent_fileops()
        if agent_fileops_client is not None:
            # The paired half goes through the agent too, under the *same*
            # named-error handling as the tree (D19): unlike the
            # ``ignore_errors=True`` this replaces, a runtime dir that survives
            # its removal is a failure the caller sees (the N12/N24 lesson:
            # "recorded as removed" with the files still on disk is the shape no
            # GC can ever reclaim) -- and "it was never there" is a success the
            # log names.
            _remove_agent_half(
                agent_fileops_client,
                sandbox_id,
                path=runtime_dir,
                op="remove_runtime",
                what="platform state",
            )
        else:
            shutil.rmtree(runtime_dir, ignore_errors=True)
        # ...and its **node-local** sibling (N57 / Task 4), where the create's
        # ``.creating`` marker and its ``statfs`` accounting seed live once a
        # deployment names ``E2B_NODE_STATE_BASE``. Nothing else collects that
        # directory: the node-local base is not a sandbox-id namespace, so
        # neither the orphan GC nor any scan walks it, and one directory per
        # deleted sandbox would accumulate on the node's disk for ever. It is
        # the worker's own file (the worker created it as 65534), so this is a
        # plain local removal rather than a privileged file step -- and with no
        # node base named it is the same path as the removal above, which is
        # what keeps the one-base deployments byte-for-byte unchanged.
        shutil.rmtree(
            _node_runtime_dir(settings, sandbox_id), ignore_errors=True
        )
        # ...and the pure shape's synthesized root (N16), the third thing the
        # platform holds for this sandbox: ``<pure_rootfs_dir>/<id>`` is the
        # skeleton the sandbox's own mount namespace binds into. It goes with
        # the tree for the same reason the runtime record does -- a skeleton
        # whose sandbox is gone is a root nobody owns, and this one has no
        # collector either (the namespace is reserved, so neither the
        # orphan-tree GC nor the fail-safe quota scan walks it).
        #
        # The path is the *executor's* own source of truth rather than a
        # workspace-base convention: ``settings.pure_rootfs_dir`` is what
        # ``executors.factory`` hands the executor, and ``E2B_PURE_ROOTFS_DIR``
        # may pin it onto a volume of the worker's own -- deriving
        # ``<base>/_pure_rootfs`` here would look right by default and leak on
        # exactly the deployments that moved it.
        pure_rootfs_dir = Path(settings.pure_rootfs_dir)
        if pure_rootfs_dir != Path(pure_rootfs_dir.anchor):
            # A filesystem root is never a synthesized-root parent: a
            # misconfigured ``E2B_PURE_ROOTFS_DIR=/`` must not turn a teardown
            # into an ``rmtree`` of ``/<id>``, exactly as the executor's heal
            # refuses to ``chmod`` one (``_materialize_synthetic_rootfs``).
            shutil.rmtree(pure_rootfs_dir / sandbox_id, ignore_errors=True)
        # ...and the checkpoint images, which live *beside* that runtime dir
        # (``_runtime/.checkpoints/<id>``, `gateway_common.paths`) because the
        # sandbox's own slot has to be able to write them. They are the largest
        # thing the platform holds for a sandbox, so a teardown that forgot them
        # would leave the platform account behind with no owner.
        remove_checkpoint_images(
            _registry_workspace_base(runtime_registry, settings),
            sandbox_id,
            state_base=_registry_state_base(runtime_registry, settings),
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
            if is_reserved_platform_namespace(entry.name):
                # A reserved name without a record is the platform's own
                # namespace, not an unreadable sandbox tree. ``state`` is the
                # one that bites (N27): the transitional config keeps the old
                # workspace base for a while with the new state base already
                # created under it, and ``state`` spells a legal sandbox id --
                # without this the platform's whole tree is reported as an
                # unmaterialised sandbox on every reconcile round (reported,
                # never deleted: noise, but noise that teaches its reader to
                # ignore the report). Read through the record first on purpose:
                # a tree that really carries this id is still materialised, so
                # this cannot strand one the way a name filter would (M1).
                continue
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
        node_id: str | None = None,
        metrics_provider: Callable[[], dict[str, Any]] | None = None,
        port_provider: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self._settings = settings
        self._runtime_registry = runtime_registry
        self._control_url = (control_plane_url or "").rstrip("/")
        self._node_address = node_address or ""
        #: The id this worker declares when it registers. Production reads
        #: ``E2B_NODE_ID`` (``_register_payload``); a harness sets this so the
        #: control plane's resolver can be pointed at the worker's endpoint.
        self._declared_node_id = node_id
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
        #: N25/L2c: build the report from the mediator's dirty set instead of
        #: walking every tree. Off by default -- see `_disk_enforce_dirty_enabled`.
        self._disk_dirty = _disk_enforce_dirty_enabled()
        #: N25: a per-round trace of what the scan actually saw, for the
        #: question "why did it take this long to notice" -- the interval and
        #: the push are both visible, but what a round *observed* is not.
        self._disk_trace = str(
            os.getenv("E2B_DISK_TRACE", "") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._disk_report: dict[str, int] = {}
        #: S2/D3: the platform's own account (checkpoint images under
        #: ``_runtime``), measured in the same round as the per-sandbox report --
        #: same volume, same cadence, same single-flight -- and carried by every
        #: heartbeat in between. Empty until the first round completes, and empty
        #: forever on a worker that turned the per-sandbox scan off
        #: (``E2B_DISK_ENFORCE_INTERVAL_S=0``), which has no accounting report at
        #: all; the endpoints' own replies still carry the numbers there.
        self._platform_disk_report: dict[str, int] = {}
        #: E9.1 blind spot 2 (``docs/resource-contention.md`` §6): a sandbox that
        #: only burns CPU crossed no request, so eviction read it as idle. The
        #: sampler turns its own ``/proc`` CPU time into activity, on the same
        #: ``sandboxActivity`` channel a request uses -- no new wire field.
        self._cpu_interval_s = _cpu_activity_interval_s()
        self._cpu_tracker = CpuActivityTracker(
            percent_threshold=_cpu_activity_percent()
        )
        self._cpu_sampler = sample_cpu_ticks
        self._cpu_trace = str(os.getenv("E2B_CPU_TRACE", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._cpu_loop_task: asyncio.Task | None = None
        self._disk_report_at = 0.0
        #: The scan round in flight, if any (single-flight, like the reconcile
        #: round): the heartbeat reads the last completed report and never
        #: waits for a walk.
        self._disk_scan_task: asyncio.Task | None = None
        #: N25: the out-of-band report for a sandbox that just crossed its
        #: budget (see `_report_budget_crossings`).
        self._push_task: asyncio.Task | None = None
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
        # E9.1 blind spot 2: CPU sampling runs on its own cadence, for the same
        # reason the disk round does -- the pulse is every 5 s and a round must
        # not be given that as its floor.
        if self._cpu_interval_s > 0:
            self._cpu_loop_task = asyncio.create_task(self._cpu_activity_loop())
        # N25: the scan cadence gets its own task, because the heartbeat only
        # runs every 5 s -- a round started from there would inherit that as
        # its floor no matter what the interval says (measured: a 4.7 s freeze
        # latency with the interval set to 1 s).
        self._disk_loop_task: asyncio.Task | None = None
        if self._disk_provider is not None and self._disk_interval_s > 0:
            self._disk_loop_task = asyncio.create_task(self._disk_loop())
            # N25: the pushed-append path runs a round *now* when a sandbox has
            # written enough for the cached number to be stale. The event pump
            # lives on the slot's own thread, so the callback crosses back to
            # this loop; `call_soon_threadsafe` is what makes that safe, and
            # the registry rate-limits how often it is even asked.
            registry = getattr(self._disk_provider, "__self__", None)
            install = getattr(registry, "set_disk_wakeup", None)
            logger.info(
                "disk wakeup wiring: provider=%s registry=%s installable=%s",
                type(self._disk_provider).__name__,
                type(registry).__name__,
                install is not None,
            )
            if install is not None:
                loop = asyncio.get_running_loop()
                install(
                    lambda sandbox_id: loop.call_soon_threadsafe(
                        lambda: self._maybe_scan_disk(force=True)
                    )
                )

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
            payload = _register_payload(self._settings, self._declared_node_id)
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
                    # Diagnosability (C3 Task 2): a worker whose registration is
                    # refused used to say nothing at all locally -- every round
                    # just retried. The named line is what makes "this worker
                    # never joined" visible on the node; the most common cause
                    # is the shape this identity layer requires: a *separated*
                    # worker must declare ``E2B_NODE_ID`` (the control plane
                    # verifies a node-scoped claim against the node's resolved
                    # address, and cannot invent an identity from a shared key).
                    logger.warning(
                        "node agent: registration rejected by the control plane "
                        "(HTTP %s): this worker will not join; a separated worker "
                        "must declare E2B_NODE_ID",
                        resp.status_code,
                    )
            else:
                resp = await client.post(
                    f"{self._control_url}/internal/nodes/{self._node_id}/heartbeat",
                    json=_heartbeat_usage_payload(
                        self._settings,
                        self._metrics_provider,
                        self._activity_provider,
                        self._port_provider,
                        self._disk_report_for_heartbeat(),
                        self._platform_disk_report,
                        worker_pid_namespace(),
                        worker_identity_fields(),
                        reported_container_id(),
                    ),
                    headers=headers,
                )
                if resp.status_code == 404:
                    # The control plane lost us (e.g. it restarted);
                    # re-register on the next cycle.
                    self._node_id = None
                elif resp.status_code >= 300:
                    # Same diagnosability rule for the heartbeat: a refusal the
                    # control plane explains in its own log is otherwise
                    # invisible from this node's side.
                    logger.warning(
                        "node agent: heartbeat for node %s rejected by the "
                        "control plane (HTTP %s)",
                        self._node_id,
                        resp.status_code,
                    )
        # Outside the pulse's client scope on purpose: the round owns its client
        # (this one is closed when the ``async with`` above exits, and a detached
        # round outlives it).
        self._start_reconcile_if_due()

    def _maybe_scan_disk(self, *, force: bool = False) -> dict[str, int]:
        """Start a scan round if one is due (single-flight); return the last.

        Called from the heartbeat *and* from `_disk_loop`. The loop is what
        makes the interval real: the heartbeat only runs every 5 s, so a round
        started from there would give the interval an effective floor of one
        pulse -- measured as a 4.7 s freeze latency with the interval set to
        1 s, because the crossing was only noticed on a pulse.

        ``force`` is the pushed-append path (N25): the mediator has seen the
        sandbox write enough that the cached number is out of date by more
        than the trigger threshold, so the round runs now rather than at the
        next tick. The interval still governs *unforced* rounds, and the
        single-flight guard is what keeps a burst of triggers from becoming a
        burst of walks.
        """
        if self._disk_provider is None or self._disk_interval_s <= 0:
            return {}
        now = time.monotonic()
        in_flight = self._disk_scan_task is not None and not self._disk_scan_task.done()
        due = force or now - self._disk_report_at >= self._disk_interval_s
        if not in_flight and due:
            self._disk_scan_task = asyncio.create_task(self._scan_disk_round())
        return self._disk_report

    async def _disk_loop(self) -> None:
        """Drive the scan cadence independently of the pulse (N25)."""
        tick = max(0.1, min(self._disk_interval_s, 1.0))
        while True:
            await asyncio.sleep(tick)
            try:
                self._maybe_scan_disk()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.warning("disk loop tick failed", exc_info=True)

    async def _cpu_activity_loop(self) -> None:
        """Mark CPU-burning sandboxes active, on the sampling cadence (E9.1)."""
        while True:
            await asyncio.sleep(self._cpu_interval_s)
            try:
                await self._cpu_activity_round()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.warning("cpu activity round failed", exc_info=True)

    async def _cpu_activity_round(self) -> dict[str, float]:
        """One CPU sample: mark the sandboxes that were actually working.

        The sample itself is a `/proc` walk, so it runs on a worker thread. Only
        the *delta* since the previous sample counts, and only above the
        configured percentage -- "there was a delta" is not activity (a process
        that wakes once a minute has one) and would make eviction impossible.

        Returns the marked sandboxes for tests and for the trace log; a sandbox
        with no pooled uid (the shared-uid shape) is deliberately skipped, since
        its CPU cannot be told apart from the worker's own.
        """
        ticks = await asyncio.to_thread(self._cpu_sampler)
        percents = self._cpu_tracker.observe(ticks, now=time.time())
        busy = self._cpu_tracker.busy(percents)
        marked: dict[str, float] = {}
        if busy:
            for record in self._runtime_registry.list():
                uid = getattr(record, "host_uid", None)
                if uid is None or getattr(record, "state", "running") != "running":
                    continue
                uid = int(uid)
                if uid in busy:
                    self._runtime_registry.mark_active(record.sandbox_id)
                    marked[record.sandbox_id] = percents[uid]
        if self._cpu_trace:
            logger.info(
                "cpu trace: uids=%d busy=%d marked=%s",
                len(percents),
                len(busy),
                {k: round(v, 1) for k, v in marked.items()},
            )
        return marked

    def _disk_report_for_heartbeat(self) -> dict[str, int]:
        """The last measured tree sizes; a fresh scan is started, never awaited.

        The heartbeat carries whatever the last completed round produced. The
        round itself is a worker-thread task behind a single-flight guard (the
        shape N21 gave the reconcile round), so a slow or failing scan can never
        stall a pulse or a request -- it only means the next round starts late.
        """
        return self._maybe_scan_disk()

    def _publish_disk_stats(self, report: dict[str, int]) -> None:
        """Mirror a fresh disk report into each sandbox's ``statfs(2)`` file.

        SEC-K0S-006: the quota is what the sandbox was sold (``diskMB`` at
        create), the usage is what this round just measured. A sandbox with no
        record (already gone) is skipped; ``_write_disk_stats`` never raises.
        """
        for sandbox_id, used in (report or {}).items():
            record = self._runtime_registry.get(sandbox_id)
            if record is None:
                continue
            total = int(getattr(record, "disk_mb", 0) or 0) * 1024 * 1024
            _write_disk_stats(
                self._settings,
                sandbox_id,
                total_bytes=total,
                used_bytes=int(used or 0),
            )

    async def _scan_disk_round(self) -> None:
        """Run one scan round off the loop and publish what it found."""
        # The cadence is claimed *before* the walk, so a failing or slow round
        # cannot turn the heartbeat into a scan storm: the next one waits for
        # the interval either way.
        self._disk_report_at = time.monotonic()
        # S2/D3: the platform's own account, measured in the same off-loop round.
        # It answers a different question than the report below -- what the
        # *deployment* stores (checkpoint images), not what a sandbox wrote -- but
        # it is the same walk of the same volume, so it belongs to the same
        # cadence rather than to a timer of its own. Measured first on purpose: a
        # per-sandbox walk can fail or run out of budget, and the account that
        # bounds the images must not go silent with it.
        try:
            self._platform_disk_report = await asyncio.to_thread(
                _measure_platform_account,
                _registry_workspace_base(self._runtime_registry, self._settings),
                _registry_state_base(self._runtime_registry, self._settings),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "platform checkpoint account measurement failed", exc_info=True
            )
        try:
            report = await asyncio.to_thread(
                self._disk_provider,
                budget_s=_DISK_SCAN_BUDGET_S,
                dirty=self._disk_dirty,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("sandbox disk scan failed", exc_info=True)
            return
        self._disk_report = dict(report or {})
        # SEC-K0S-006: mirror the fresh accounting into each sandbox's
        # `statfs(2)` file, so `df` inside a sandbox shows its own quota and
        # remainder rather than the node's volume. This round is the same
        # measurement the platform's ledger uses, so the two cannot drift
        # apart by more than the scan cadence.
        self._publish_disk_stats(self._disk_report)
        if self._disk_trace:
            logger.info(
                "disk trace: round=%s took=%.3fs got=%s",
                time.strftime("%H:%M:%S", time.localtime())
                + f".{int(time.time() * 1000) % 1000:03d}",
                time.monotonic() - self._disk_report_at,
                dict(report or {}),
            )
        self._report_budget_crossings()

    def _report_budget_crossings(self) -> None:
        """Push a sandbox that just crossed its budget, without waiting (N25).

        The report normally rides the heartbeat, which means a sandbox can keep
        writing for up to a full pulse after it is known to be over. A
        *crossing* is the one moment worth a report of its own: the control
        plane's answer is to freeze the sandbox, so every second of delay is a
        second of writing.
        """
        taker = getattr(self._disk_provider, "__self__", None)
        take = getattr(taker, "take_budget_crossings", None)
        if take is None:
            return
        crossings = take()
        if not crossings:
            return
        logger.info(
            "sandbox disk budget crossed for %s; reporting immediately",
            ",".join(sorted(crossings)),
        )
        self._push_task = asyncio.create_task(self._push_disk_report(crossings))

    async def _push_disk_report(self, usage: dict[str, int]) -> None:
        """Send one out-of-band usage report (N25).

        Same endpoint and credentials as the pulse -- it *is* a heartbeat, with
        only the disk field in it -- because the control plane's disk gate hangs
        off that route and the node's liveness is refreshed by the same call. A
        failure is logged and dropped: the next pulse carries the same numbers
        anyway, so a missed push costs latency, never correctness.
        """
        if self._node_id is None:
            return
        import httpx

        # Only the disk report: this is not a replacement pulse, and sending
        # the full usage twice per interval would double every other report.
        payload = {"sandboxDiskUsage": dict(usage)}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._control_url}/internal/nodes/{self._node_id}/heartbeat",
                    json=payload,
                    headers={"X-Internal-Key": self._settings.internal_api_key},
                )
            if resp.status_code == 404:
                self._node_id = None
            elif resp.status_code != 204:
                logger.warning(
                    "out-of-band disk report rejected by %s: %s %s",
                    self._control_url,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.warning("out-of-band disk report failed", exc_info=True)

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
        that ran and what it decided. The checkpoint images are in it for the
        same reason (E4): what a round reclaimed from the platform's own account
        is the number an operator watching a full account is looking for.
        """
        logger.info(
            "reconcile summary: deleted=%d delete_failures=%d unmaterialised=%d "
            "protected_elsewhere=%d concurrent_creates=%d quota_cleaned=%d "
            "quota_unreclaimed=%d disk_sweep_skipped=[%s] untrusted_records=[%s] "
            "checkpoints_reclaimed=[%s] checkpoints_sweep_skipped=[%s]",
            len(summary["deleted"]),
            len(summary["delete_failures"]),
            len(summary["unmaterialised"]),
            len(summary["protected_elsewhere"]),
            len(summary["concurrent_creates"]),
            len(summary["quota_cleaned"]),
            len(summary["quota_unreclaimed"]),
            ",".join(summary["disk_sweep_skipped"]),
            ",".join(summary["untrusted_records"]),
            ",".join(summary["checkpointsReclaimed"]),
            ",".join(summary["checkpoints_sweep_skipped"]),
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
        * the checkpoint images are collected by the same round under the same
          fence (E4). Their store (``_runtime/.checkpoints/<id>``) is the one
          namespace no other scan walks — both scans above enumerate trees — so
          an image whose record is gone was invisible to every collector while
          being the largest thing the platform holds for a sandbox and the very
          thing its account refuses the next capture on;
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
        # The checkpoint images have a namespace of their own and, until now, no
        # candidate set of their own either: both scans above enumerate *trees*,
        # and a store is a sibling of the per-sandbox runtime dir rather than a
        # child of any tree. So an image whose record is gone -- the record was
        # deleted, or the capture finished and the record vanished right after --
        # stayed on the platform's account forever, until the account was full
        # and refused the next capture. It is the largest thing the platform
        # holds for a sandbox, and it is billed to the platform, so the same
        # round collects it (see ``remove_orphan_checkpoint_stores``).
        images_on_disk = await asyncio.to_thread(
            list_checkpoint_stores,
            _registry_workspace_base(self._runtime_registry, self._settings),
            state_base=_registry_state_base(self._runtime_registry, self._settings),
        )
        deletable = orphaned
        protected_elsewhere: list[str] = []
        disk_sweep_skipped: list[str] = []
        checkpoints_sweep_skipped: list[str] = []
        checkpoints_reclaimed: list[str] = []
        image_owners: set[str] = set()
        fleet_owned: set[str] | None = None
        if candidates or images_on_disk:
            fleet_owned = await self._fleet_sandbox_ids(client, headers)
            if fleet_owned is None:
                # Fleet-wide ownership cannot be established: fall back to
                # the node-local semantics for runtimes this process owns and
                # leave the disk-only trees for a later round. The skip is
                # reported in the summary and the retry is scheduled with a
                # capped backoff (M1) so this state can never be permanent
                # and silent.
                disk_sweep_skipped = sorted(disk_candidates)
                checkpoints_sweep_skipped = sorted(images_on_disk)
                if candidates:
                    logger.warning(
                        "reconcile: leaving %d orphan tree(s) on disk alone this "
                        "round (fleet record enumeration unavailable): %s",
                        len(disk_candidates),
                        ",".join(sorted(disk_candidates)),
                    )
                if images_on_disk:
                    # Same fence, higher stake: an image this round cannot
                    # attribute may belong to a live sandbox on a node that has
                    # not re-registered yet, and deleting it destroys a user's
                    # state rather than leaving a tree behind.
                    logger.warning(
                        "reconcile: leaving %d checkpoint image(s) alone this "
                        "round (fleet record enumeration unavailable): %s",
                        len(images_on_disk),
                        ",".join(sorted(images_on_disk)),
                    )
                self._defer_sweep()
            else:
                deletable = candidates - fleet_owned
                protected_elsewhere = sorted(candidates & fleet_owned)
                # Who may claim an image: every control-plane record in the
                # fleet (the store is on the shared volume, so this worker sees
                # other nodes' images and only a fleet-wide answer separates
                # those from orphans), plus every sandbox this worker still
                # holds a record for -- in memory, or as a tree on its disk. "No
                # record" has to mean nowhere before an image may go.
                #
                # The asymmetry with the trees above is deliberate and is the
                # rule the two halves are judged by: **an image is only
                # meaningful while a record claims it** -- the record is the
                # only thing that knows how to resume one, so an image no
                # record anywhere claims can never be used again and only ever
                # occupies the platform's account -- **while a tree is the
                # user's data, and a record that cannot be read is no reason to
                # delete data**. That is why ``unmaterialised`` ids stay out of
                # this set: the round keeps the tree it could not verify and
                # still collects an image nothing claims.
                image_owners = (
                    set(known) | set(local) | concurrent_creates | fleet_owned
                )
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
        if images_on_disk and fleet_owned is not None:
            # After the teardowns, not before: an id this round just deleted
            # still counts as an owner (it is in ``local``), so its image is
            # the teardown's to remove -- one action, one owner -- and a
            # teardown that *failed* leaves the image for the next round
            # instead of having the sweep delete it behind the failure.
            checkpoints_reclaimed = await asyncio.to_thread(
                remove_orphan_checkpoint_stores,
                _registry_workspace_base(self._runtime_registry, self._settings),
                keep=image_owners,
                state_base=_registry_state_base(self._runtime_registry, self._settings),
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
        if not disk_sweep_skipped and not checkpoints_sweep_skipped:
            # A round that reached the fleet's full record set clears the
            # deferred-sweep backoff; a deferred one keeps its schedule.
            # ``checkpoints_sweep_skipped`` counts here because the images are
            # deferred by the *same* fence and the retry it schedules would
            # otherwise be cleared in the same round that armed it.
            self._sweep_completed()
        # The naming rule of this dict, written down so the next reader does not
        # "tidy it up" by hand: **a key the brief or an existing consumer has
        # already named keeps that spelling** -- ``checkpointsReclaimed`` is
        # named in the Task E4 brief and in its plan doc -- **and every new key
        # follows this dictionary's snake_case** (``disk_sweep_skipped``,
        # ``checkpoints_sweep_skipped``, ``quota_unreclaimed``).
        # 简报/既有消费方点名的键保留原名；新增键一律跟本字典的 snake_case 走。
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
            "checkpointsReclaimed": sorted(checkpoints_reclaimed),
            "checkpoints_sweep_skipped": checkpoints_sweep_skipped,
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

        D6/D7: the records come from the **fleet-scope**
        ``/internal/fleet/sandboxes`` endpoint, not from one call per node.
        Asking each node for its own list would make this sweep depend on
        *every* node being resolvable — and a worker that is permanently gone
        keeps its registry row (and its records) until its sandboxes' TTL, so
        the per-node shape would stall reclamation fleet-wide exactly when a
        node has died. The per-node endpoints stay identity-guarded; this sweep
        was never speaking for another node.

        The answer is attributed (``{"sandboxes": {node_id: [id, …]}}``, D7);
        for the sweep the attribution is irrelevant and only the id set matters,
        so it is flattened here. The completeness rule below (count vs
        ``/internal/fleet/metrics``) is unchanged and deliberately strict.
        """
        try:
            resp = await client.get(
                f"{self._control_url}/internal/fleet/sandboxes", headers=headers
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("fleet sandbox list is not an object")
            by_node = payload.get("sandboxes")
            if not isinstance(by_node, dict):
                raise ValueError("fleet sandbox attribution is not an object")
            owned = {
                str(sid)
                for node_ids in by_node.values()
                for sid in (node_ids or [])
            }
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
                    state_base=_registry_state_base(
                        self._runtime_registry, self._settings
                    ),
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
        for attribute in (
            "_task",
            "_reconcile_task",
            "_disk_scan_task",
            "_disk_loop_task",
            "_cpu_loop_task",
            "_push_task",
        ):
            task: asyncio.Task | None = getattr(self, attribute, None)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, attribute, None)


def _write_disk_stats(
    settings: Settings, sandbox_id: str, *, total_bytes: int, used_bytes: int
) -> None:
    """Publish one sandbox's disk accounting for the fork's ``statfs(2)``.

    SEC-K0S-006: the sandbox's quota and what is left of it are the host's to
    know, so the host writes them next to the sandbox's record (outside the
    sandbox's own tree, which is what keeps them unforgeable) and the fork
    reports them on each ``statfs``. Best effort on purpose: failing to publish
    must never fail a create, and a missing file only means ``statfs`` falls
    back to the kernel's answer.

    **The reader is not the writer.** In the route-B shape the mediator that
    reads this file is the slot process, whose euid is the sandbox's host uid
    (10000+) while the worker writes as 65534 -- so the modes cannot be left to
    the ambient umask. They are the same rules the slot documents follow
    (``route_b._write_slot_documents``): directories traversable by name
    (``0711``, listable by nobody) and the file itself world-readable
    (``0644``) -- these numbers are what the sandbox is *shown*, so there is
    nothing to scope. Measured 2026-10-01: with the runtime directory at its
    historical ``0700`` the slot's ``read`` failed with EACCES and every
    ``statfs`` silently fell back to the node's numbers.
    """
    from gateway_common.paths import sandbox_disk_stats_path, write_text_atomically

    path = sandbox_disk_stats_path(
        settings.workspace_base,
        sandbox_id,
        state_base=settings.state_base,
        node_state_base=settings.node_state_base,
    )
    try:
        runtime_dir = path.parent
        state_dir = runtime_dir.parent
        state_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o711)
        os.chmod(runtime_dir, 0o711)
        write_text_atomically(path, f"{int(total_bytes)} {int(used_bytes)}\n")
        # `write_text_atomically` stages at `0o666 & ~umask`, so say it again
        # after the rename: the slot uid is neither the owner nor in its group.
        os.chmod(path, 0o644)
    except Exception:  # noqa: BLE001 - publish is best effort, never fatal
        logger.warning("disk stats publish failed for %s", sandbox_id, exc_info=True)


def _creating_marker(settings: Settings, sandbox_id: str) -> Path:
    return sandbox_creating_marker(
        settings.workspace_base,
        sandbox_id,
        state_base=settings.state_base,
        node_state_base=settings.node_state_base,
    )


def _node_runtime_dir(settings: Settings, sandbox_id: str) -> Path:
    """The node-local half of ``_runtime/<id>``: the create's two chips.

    Unset ``E2B_NODE_STATE_BASE`` makes this the *shared* runtime directory
    again (``<state base>/_runtime/<id>``), which is what keeps every cleanup
    below idempotent on the deployments that never name the base.

    It reads the base from ``settings`` and not from the runtime registry on
    purpose: the node-local base is a *deployment* input (it is what the init
    container creates), and a registry handed a different workspace/state base
    by an embedder does not make a second node-local one. When the node base is
    named the two agree by construction; when it is not, the path is the shared
    one and the extra cleanup is a no-op.
    """
    return sandbox_node_runtime_dir(
        settings.workspace_base,
        sandbox_id,
        state_base=settings.state_base,
        node_state_base=settings.node_state_base,
    )


def _write_creating_marker(settings: Settings, sandbox_id: str) -> Path:
    """Announce that a create is in flight (design §4.5).

    One byte, no ``fsync``: this is not a record -- it is a *presence*, and the
    record that follows it is what says the create succeeded. A crash between
    the two leaves "marker, no record", which the orphan path already reclaims.
    """
    marker = _creating_marker(settings, sandbox_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    return marker


def _clear_creating_marker(settings: Settings, sandbox_id: str) -> None:
    _creating_marker(settings, sandbox_id).unlink(missing_ok=True)


def _marker_age_s(marker: Path) -> float | None:
    """Seconds since the marker was written, or ``None`` if it is not there."""
    try:
        return max(0.0, time.time() - marker.stat().st_mtime)
    except FileNotFoundError:
        return None


def _await_inflight_create(
    settings: Settings, sandbox_id: str, *, timeout_s: float | None = None
) -> bool:
    """Wait, bounded, for an in-flight create of ``sandbox_id`` to finish.

    Returns ``True`` when there was nothing to wait for (or the wait
    completed), ``False`` when the marker was still there after the bound --
    the caller then reclaims the sandbox as an **unfinished** create (its tree
    and its record are both disposable: the record of a create that did not
    finish is exactly what the orphan path is for).

    A marker older than the bound is treated as abandoned *immediately*
    rather than after another full wait: a worker that crashed mid-create left
    it, and no future create will ever clear it.
    """
    marker = _creating_marker(settings, sandbox_id)
    bound = float(
        settings.create_wait_s if timeout_s is None else timeout_s
    )
    age = _marker_age_s(marker)
    if age is None:
        return True
    if age >= bound:
        logger.warning(
            "sandbox %s has a create marker %.0fs old (bound %.0fs): treating "
            "it as an unfinished create and reclaiming",
            sandbox_id,
            age,
            bound,
        )
        return False
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if _marker_age_s(marker) is None:
            return True
        # Short enough that a delete does not add a visible delay to a create
        # that finished while it was arriving.
        time.sleep(0.05)
    logger.warning(
        "sandbox %s still has a create marker after waiting %.0fs: treating it "
        "as an unfinished create and reclaiming",
        sandbox_id,
        bound,
    )
    return False


def _schedule_record_persist(request: Request, sandbox_id: str | None) -> None:
    """Hand the record write to the loop, off the create's response path (§4.5).

    Scheduled (not awaited) on purpose: the create's contract is "the sandbox
    exists" -- which ``register(persist=False)`` already made true for everyone
    in this process -- while the disk copy exists for *other* processes, and
    the marker is what keeps the two consistent in the meantime.
    """
    if not sandbox_id:
        return
    runtime_registry = request.app.state.runtime_registry
    settings = request.app.state.settings
    asyncio.create_task(
        _persist_runtime_record(runtime_registry, settings, sandbox_id)
    )


async def _persist_runtime_record(
    runtime_registry, settings: Settings, sandbox_id: str
) -> None:
    """Write the record, then -- only then -- let a teardown proceed.

    ``persist`` is the registry's own file work, so it runs in a thread. On
    success the two things that were held back release in order: the create
    marker comes off (so a waiting ``DELETE`` may now tear the sandbox down),
    and the uid reservation is committed (I1 -- the record now pins the uid for
    every worker, so the transient marker is no longer needed).

    On failure the marker **stays**: the disk then says "a create was in flight
    and did not finish", which is exactly the input the orphan path reclaims,
    and the uid stays reserved so another worker cannot hand the same uid to a
    second sandbox.
    """
    try:
        durable = await asyncio.to_thread(runtime_registry.persist, sandbox_id)
    except Exception:  # pragma: no cover - defensive; persist already names
        logger.exception("persisting the runtime record for %s failed", sandbox_id)
        return
    if not durable:
        return
    _clear_creating_marker(settings, sandbox_id)
    pool = getattr(runtime_registry, "uid_pool", None)
    if pool is not None:
        pool.commit(sandbox_id)


def _claim_host_uid(
    settings: Settings,
    runtime_registry,
    payload: dict,
    sandbox_id: str,
    existing,
) -> tuple[int | None, object | None]:
    """The sandbox's host uid for this create, reserving it if this shape does.

    E3.2: the uid is allocated before materializing volumes so per-sandbox
    volume slices can be chowned to it. Only a root worker -- or a non-root
    worker that can ask *someone* for the chown: C3's per-node agent
    (``priv_helpers.file_steps_available``) -- can put a sandbox under its own
    host uid; everything else keeps the fixed-uid + Landlock model and never
    allocates.

    ⚠ This gate is what makes the hand-over *reachable* at all: a predicate
    that asked only about a local privileged shape left ``host_uid`` None in
    the agent shape, so ``apply_sandbox_ownership`` and every face-B
    ``chown`` were skipped in silence (review Task 4 slice A, Important 2).

    Callable twice for one create -- ``prepare`` reserves it and ``finalize``
    asks again -- so the pool's own answer wins when this process already holds
    one (``UidPool.held_uid``). Without that, the second call would come back
    through ``acquire`` and hand out a *different* uid (the first one is
    reserved), i.e. two uids for one sandbox.
    """
    pool = getattr(runtime_registry, "uid_pool", None)
    if pool is None:
        return None, None
    held = pool.held_uid(sandbox_id)
    if held is not None:
        return held, pool
    from envd_service import priv_helpers

    if not settings.per_sandbox_uid or not (
        os.geteuid() == 0 or priv_helpers.file_steps_available(settings)
    ):
        return None, pool
    # OBS-9: the control plane allocates the fleet-wide uid and passes it
    # down; the worker's own pool is the fallback for payloads without one
    # (an older control plane, or a deployment that never enabled it).
    allocated = payload.get("hostUID")
    if isinstance(allocated, int) and not isinstance(allocated, bool):
        return pool.claim(sandbox_id, allocated), pool
    return (
        pool.acquire(
            sandbox_id,
            preferred=existing.host_uid if existing is not None else None,
        ),
        pool,
    )


def _agent_finalize_sandbox(request: Request, settings: Settings, payload: dict) -> None:
    """The tree-dependent half of a create (design §4.6 (b)).

    Everything here needs the tree to exist: building it (when the agent did
    not), the volume mounts and the quota that only a node can decide, the
    ownership hand-over, and the record.

    ``materialized`` says the control plane's node agent already made the tree
    and handed it over, so this worker does neither the tree nor the ownership
    (design v2 §4.4). It is one payload field and not an exception path on
    purpose: absent means "build it yourself", which is what an older control
    plane sends -- so both directions of a rolling upgrade are the old
    behaviour, with nothing to degrade and nothing to report.

    Called either on its own (the control plane's ``phase: finalize``, after its
    ``phase: prepare``) or right behind ``_agent_prepare_sandbox`` in one worker
    thread (the single-shot route every existing caller uses).
    """
    runtime_registry = request.app.state.runtime_registry
    workspace_base = settings.workspace_base
    sandbox_id = payload.get("sandboxID")
    if not sandbox_id:
        raise ValueError("sandboxID is required")
    workspace_dir = workspace_base / sandbox_id
    snapshot_id = payload.get("snapshotID")
    materialized = payload.get("materialized") is True
    if not materialized:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        if snapshot_id:
            # N57: the store hangs off the platform namespace root (the shared
            # export root), not off the tree root -- the record beside this
            # payload is written there by the control plane.
            snapshot_dir = snapshot_payload_dir(
                workspace_base,
                snapshot_id,
                shared_root=settings.shared_volume_root,
            )
            snapshot_tar = snapshot_dir / SNAPSHOT_PAYLOAD_TAR_NAME
            snapshot_fs = snapshot_dir / SNAPSHOT_PAYLOAD_DIR_NAME
            # Task 2: the writer emits ``fs.tar``, and everything snapshot older
            # than that is an exploded ``fs/`` directory -- this path (the
            # control plane did *not* materialize the tree, so the worker does
            # it) reads both, through the same shared extractor the agent's
            # materialize instruction uses.
            try:
                if snapshot_tar.is_file():
                    extract_sandbox_archive(snapshot_tar, workspace_dir)
                elif snapshot_fs.is_dir():
                    shutil.copytree(
                        snapshot_fs, workspace_dir, dirs_exist_ok=True, symlinks=True
                    )
                else:
                    raise ValueError(
                        f"Snapshot {snapshot_id} not found on this node"
                    )
            except ArchiveRefusal as e:
                raise ValueError(
                    f"Snapshot {snapshot_id} is not readable: {e}"
                ) from e
        else:
            (workspace_dir / "workspace").mkdir(parents=True, exist_ok=True)
    volume_mounts = payload.get("volumeMounts") or []
    existing = runtime_registry.get(sandbox_id)
    host_uid, pool = _claim_host_uid(
        settings, runtime_registry, payload, sandbox_id, existing
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
            # The create plan already made every slice and handed it over, so
            # this pass only provisions quota -- the ``mkdir``/``chmod`` and the
            # relayed ``chown-volume-slice`` are exactly what the plan removed
            # (design §4.3 step ③).
            slices_materialized=materialized,
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
        if materialized:
            # The agent handed the tree over in the same call that made it
            # (``chmod`` every directory to 0770 + one recursive ``chown``), so
            # neither local branch applies. Doing ``apply_sandbox_ownership``
            # here anyway would add the very round trip this change removes --
            # measured 2026-10-01 at 71 ms for the relayed ``chown-workspace``.
            pass
        elif host_uid is not None:
            apply_sandbox_ownership(workspace_dir, host_uid, sandbox_id=sandbox_id)
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
            # §4.5: the record goes to the disk *after* the response. The entry
            # is in memory from here on, so the RPC path (and the runtime
            # context this route primes next) sees the sandbox immediately;
            # what the caller would otherwise wait for is one atomic write plus
            # an fsync (47 ms measured 2026-10-01).
            persist=False,
        )
        # The ``statfs(2)`` accounting seed lives in ``_agent_prepare_sandbox``
        # now (design §4.6 (b)): it needs no tree, so it belongs to the half
        # that runs *beside* the agent's materialization, not behind it.
    except BaseException:
        # I3: any failure between acquire and register (invalid volume
        # mounts -> 400, quota/ownership errors -> 500) must return the
        # reserved uid to the pool instead of leaking a slot.
        if host_uid is not None and pool is not None:
            pool.release(sandbox_id)
        raise


def _agent_prepare_sandbox(request: Request, settings: Settings, payload: dict) -> None:
    """The half of a create that never needed the tree (design §4.6 (b)).

    Under (b) the control plane sends this *beside* the node's agent
    materialization, so the two overlap and only the longer one is on the
    create's critical path. That is why nothing here may touch the tree -- not
    the ``mkdir``/``copytree``, not the mount view, not the ownership hand-over
    -- and why nothing here may register: a control plane that died between the
    two hops would otherwise leave a record for a sandbox nobody finished,
    which is exactly the residue design §4.5 closed.

    What is left is the part that needs nothing but this node's own
    bookkeeping: the ``.creating`` marker (the window's announcement), the uid
    reservation, and the seed of the ``statfs(2)`` accounting.

    **It carries no state to the finalize half.** The payload is re-sent whole
    and every value here is re-derivable from it plus the pool's own answer --
    and the split only ever happens for a create the control plane gave a
    ``hostUID`` (a record without one is not splittable at all), so a worker
    that restarts between the two hops finishes the create instead of refusing
    it or handing it a second uid.

    On failure it takes its own half back (``_agent_cancel_sandbox``) rather
    than leaving a marker and a uid reservation for a create nobody will
    finish.
    """
    runtime_registry = request.app.state.runtime_registry
    sandbox_id = payload.get("sandboxID")
    if not sandbox_id:
        raise ValueError("sandboxID is required")
    # §4.5: announce the create *before* anything is materialized, so a
    # ``DELETE`` that arrives while this runs waits instead of tearing down a
    # tree that is about to be written to (and a record that is about to be
    # written after the response).
    _write_creating_marker(settings, sandbox_id)
    try:
        _claim_host_uid(
            settings,
            runtime_registry,
            payload,
            sandbox_id,
            runtime_registry.get(sandbox_id),
        )
        disk_mb = int(payload.get("diskMB", settings.default_disk_mb))
        # SEC-K0S-006: seed the `statfs(2)` accounting before the first scan
        # round runs, so a `df` immediately after create already reports the
        # quota instead of the node's volume. Usage starts at zero and the
        # scan rounds keep it current.
        _write_disk_stats(
            settings, sandbox_id, total_bytes=disk_mb * 1024 * 1024, used_bytes=0
        )
    except BaseException:
        _agent_cancel_sandbox(request, settings, payload)
        raise


def _agent_cancel_sandbox(request: Request, settings: Settings, payload: dict) -> None:
    """Give back what ``_agent_prepare_sandbox`` took, and nothing else.

    The control plane's own undo of the prepared half (design §4.6 (b)): the
    instruction it was about to send did not happen, so the marker, the uid
    reservation and the accounting seed have to go -- or the next ``DELETE`` of
    that id waits out a full create bound on a create that will never finish,
    and a host uid stays reserved for a sandbox nobody has.

    **Never touches a registered sandbox.** The uid the pool remembers for an
    id outlives the create (``register`` does not clear it; only ``commit``
    drops the *on-disk* reservation marker), so releasing it here for a live
    sandbox would hand the same host uid to a second sandbox -- two sandboxes
    behind one isolation wall. A record means the prepared half was finalized
    and there is nothing left to cancel.

    The tree is deliberately not this half's to remove: when the control plane
    could not send the instruction, nothing on this node made one -- and when it
    could, the tree is the *agent's* work, reclaimed by the orphan sweep.
    """
    runtime_registry = request.app.state.runtime_registry
    sandbox_id = payload.get("sandboxID")
    if not sandbox_id:
        return
    if runtime_registry.get(sandbox_id) is not None:
        return
    pool = getattr(runtime_registry, "uid_pool", None)
    if pool is not None:
        pool.release(sandbox_id)
    _discard_disk_stats(settings, sandbox_id)
    _clear_creating_marker(settings, sandbox_id)
    # Both chips are gone, so the (now empty) node-local runtime directory has
    # no reader left -- take it with them. Best effort, like the seed itself:
    # ``_discard_disk_stats``' own ``rmdir`` runs while the marker is still
    # there and therefore always fails, which is why this is a second attempt
    # rather than a move.
    try:
        _node_runtime_dir(settings, sandbox_id).rmdir()
    except OSError:
        pass


def _discard_disk_stats(settings: Settings, sandbox_id: str) -> None:
    """Undo the accounting seed: the file, then the directory if it is empty."""
    from gateway_common.paths import sandbox_disk_stats_path

    path = sandbox_disk_stats_path(
        settings.workspace_base,
        sandbox_id,
        state_base=settings.state_base,
        node_state_base=settings.node_state_base,
    )
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        # Best effort, like the seed itself: a directory that is not empty is
        # not this call's to remove.
        pass


def _agent_create_sandbox(request: Request, settings: Settings, payload: dict) -> None:
    """One create, whole: the two halves in one call (design §4.6 (b)).

    This is the shape every existing caller sends -- an older control plane, the
    migration and fork paths -- and it is also what the worker does for a
    control plane that could not split the create. The halves are the same two
    functions the phased route calls, in the same order, so there is one
    implementation of "how a sandbox is made" no matter who drives it.

    Failure undoes the prepared half (I3): any failure between the uid
    reservation and ``register`` (invalid volume mounts -> 400, quota or
    ownership errors -> 500) returns the reserved uid to the pool instead of
    leaking a slot, and takes the marker and the accounting seed with it.
    """
    try:
        _agent_prepare_sandbox(request, settings, payload)
        _agent_finalize_sandbox(request, settings, payload)
    except BaseException:
        _agent_cancel_sandbox(request, settings, payload)
        raise


#: The phases the create route understands. An absent phase is the whole create
#: in one call -- the shape an older control plane, the migration path and the
#: fork path all send, and the one this route has always had.
_CREATE_PHASES = frozenset({"prepare", "finalize", "cancel"})


def _run_create_phase(request: Request, settings: Settings, payload: dict) -> str:
    """Run the phase this payload names and report which one that was.

    The dispatch (and the undo) lives here rather than in the route so the
    route only has to translate an answer into a status code -- and so the
    single-shot shape stays literally "prepare, then finalize": one
    implementation of how a sandbox is made, whoever drives it.
    """
    phase = payload.get("phase")
    if phase == "cancel":
        # Never raises: an undo that cannot run is not the control plane's
        # problem to solve here, and it must not turn into a second failure.
        _agent_cancel_sandbox(request, settings, payload)
        return "cancel"
    try:
        if phase != "finalize":
            _agent_prepare_sandbox(request, settings, payload)
        if phase != "prepare":
            _agent_finalize_sandbox(request, settings, payload)
    except BaseException:
        # Every failing shape takes the prepared half back -- including the
        # finalize half, whose own ``except`` returns the uid but leaves the
        # marker and the accounting seed for a create that did not happen.
        _agent_cancel_sandbox(request, settings, payload)
        raise
    return phase or "create"


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
        # Provisioning is filesystem work -- mkdir/chown, volume materialisation
        # and (for a snapshot-based create) a full copytree of the snapshot --
        # so it runs off the event loop. Inline it stalled heartbeats and every
        # other request on this worker for the length of the copy.
        #
        # Both halves are timed when ``E2B_CREATE_TRACE`` is on: ``provision``
        # is the whole hand-over (its own stages -- ``record``, ``commit``,
        # ``fileop:*`` -- are logged by the code that does them), and ``prime``
        # is the runtime context, which is separate because it is an
        # optimisation of the first command rather than part of the contract.
        #
        # ``phase`` is the create's two-phase handshake (design §4.6 (b)): the
        # control plane fires ``prepare`` beside the node agent's
        # materialization and then closes with ``finalize``. Absent -- which is
        # what every existing caller sends -- means the whole create in this one
        # call, byte for byte what it always was.
        sandbox_id = payload.get("sandboxID")
        phase = payload.get("phase")
        if phase is not None and phase not in _CREATE_PHASES:
            return Response(
                status_code=400,
                content=(
                    f"unknown create phase {phase!r}: the phases are "
                    + ", ".join(sorted(_CREATE_PHASES))
                    + " (an absent phase means the whole create)"
                ),
            )
        started = time.monotonic()
        done = await asyncio.to_thread(
            _run_create_phase, request, settings, payload
        )
        create_trace.stage("provision" if done == "create" else done, sandbox_id, started)
        if done == "prepare":
            # The prepared half is done and nothing is registered: there is no
            # record to persist and no runtime context to prime yet. Both
            # belong to the finalize call (design §4.5).
            return Response(status_code=200)
        if done == "cancel":
            return Response(status_code=204)
        # §4.5: the record write leaves the response path here. The marker
        # written at the top of ``_agent_create_sandbox`` is what makes that
        # safe -- it comes off only once the record is durable.
        _schedule_record_persist(request, sandbox_id)
        started = time.monotonic()
        await _prime_runtime_context(request, sandbox_id)
        create_trace.stage("prime", sandbox_id, started)
    except PermissionError as e:
        # A worker-side permission fault while provisioning, not an auth
        # failure: 500 with the reason so the control plane's
        # "failed to provision: <body>" names the real cause.
        logger.exception("agent create sandbox failed (permission)")
        return Response(status_code=500, content=str(e)[:500])
    except AgentFileOpsError as e:
        # F1: a face-B file operation (``chown-workspace`` &c.) the control
        # plane refused, or could not be reached for. It is neither an auth
        # fault nor a permission fault, and it used to fall through to the
        # generic arm below -- a body-less 500 the control plane rendered as a
        # bare "failed to provision: ". The module already named the refusal,
        # so carry that name: the operator reads *why* the destination could
        # not provision instead of an empty 502.
        logger.exception("agent create sandbox failed (file operation)")
        return Response(status_code=500, content=str(e)[:500])
    except (ValueError, json.JSONDecodeError) as e:
        return Response(status_code=400, content=str(e))
    except Exception as e:
        logger.exception("agent create sandbox failed")
        # Never a silent 500 again: C3's "named, never silent" rule means the
        # control plane must be able to read the reason (the route is
        # internal-key authenticated, so the body is the platform's own
        # diagnostic, not a leak to a sandbox).
        return Response(status_code=500, content=(str(e) or type(e).__name__)[:500])
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
        # §4.5: a teardown must not race a create of the same sandbox. Bounded,
        # and a marker older than the bound (a create that died) is reclaimed
        # at once rather than waited on -- never a hang.
        await asyncio.to_thread(_await_inflight_create, settings, sandbox_id)
        # Off the loop: this removes a whole sandbox tree, and on this
        # deployment that tree is on the shared NAS -- measured 17.4 s for a
        # 2000-file sandbox, during which the worker sent no heartbeats at all
        # (`DELETE /agent/sandboxes/...` logged its response 17.4 s after the
        # request, the gap the node-health window is measured against; N32).
        # `_delete_sandbox_runtime` is written for a worker thread already --
        # the reconcile round calls it that way (see its own note about
        # ``unregister=False``).
        await asyncio.to_thread(
            _delete_sandbox_runtime,
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


async def _agent_set_paused(
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
    * **The state is written back** (N28/A). This used to be the one delivery
      path that moved the sandbox without telling the worker's own runtime
      record: a remote sandbox was frozen on its node while that node's record
      still said ``running``. The file and command gates read exactly that
      record (``require_http_sandbox`` / ``_require_running``), so without
      this the pause did not gate anything on a remote worker.
    * The optional JSON body carries ``{"reason": "..."}`` -- the platform's
      own explanation for a pause it started (the L2b disk enforcer). It is
      handed to the record so the refusal text (``state_clause``) can say why.
      An absent body is the ordinary case and means "no reason given", which
      is what a caller's own ``pause()`` produces.
    * **S3**: with ``E2B_PAUSE_CHECKPOINT=1`` a pause first writes a checkpoint
      image, so the sandbox's process can outlive this worker, and a resume then
      either thaws the session that is still here or resumes the image that is
      not (D5). Both halves are best-effort by construction -- neither a
      checkpoint that cannot be taken nor an image that cannot be resumed may
      turn the delivery into something other than the 204 the control plane's
      push expects. The reasons land in the worker's log, and the standalone
      ``/checkpoint`` and ``/restore`` endpoints below answer them in full.
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        return Response(status_code=404)
    reason = await _pause_reason_from_body(request)
    # D4/D5: capture *before* the freeze, resume *before* the thaw is published.
    # The ordering is not cosmetic: the engine's capture stops the target child
    # and resumes it when it is done, so capturing a sandbox this worker had
    # already SIGSTOPped would release it again (a "pause" that quietly keeps
    # running), and a resume that published "running" before the image was back
    # would let a command run in an empty session first.
    if paused and settings.pause_checkpoint:
        await _checkpoint_before_pause(settings, request, sandbox_id)
    elif not paused:
        await _resume_process_tree(settings, request, sandbox_id)
    # The freeze/thaw itself rides the registry's state callback
    # (``create_app`` wires ``set_state`` -> ``ctx.pause()/resume()`` for a live
    # context), so the state and the effect cannot diverge -- calling
    # ``ctx.pause()`` here as well would stop every process group twice.
    ctx = request.app.state.runtimes.get(sandbox_id)
    request.app.state.runtime_registry.set_state(
        sandbox_id, "paused" if paused else "running", reason
    )
    logger.info(
        "agent %s sandbox %s (%s)",
        "pause" if paused else "resume",
        sandbox_id,
        "live context" if ctx is not None else "no live context",
    )
    return Response(status_code=204)


async def _checkpoint_before_pause(
    settings: Settings, request: Request, sandbox_id: str
) -> None:
    """Take the image a later resume needs; never fail the pause over it (S3/D4).

    Off the event loop on purpose: a capture is a native call plus the sandbox's
    whole memory written to a network-backed volume, and stalling this worker's
    heartbeats for that would be exactly the N32 shape.
    """
    ctx = request.app.state.runtimes.get(sandbox_id)
    # The image has to land where the *slot* can write it, and the slot runs as
    # the sandbox's pooled uid (route B). ``None`` on a shared-uid worker.
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    try:
        reply = await asyncio.to_thread(
            capture_checkpoint_image,
            _registry_workspace_base(request.app.state.runtime_registry, settings),
            ctx,
            sandbox_id,
            owner_uid=getattr(runtime, "host_uid", None),
            state_base=_registry_state_base(
                request.app.state.runtime_registry, settings
            ),
        )
    except Exception:  # noqa: BLE001 - see the docstring: the pause proceeds
        logger.warning(
            "pause of sandbox %s could not take a checkpoint; it is frozen in "
            "place exactly as before",
            sandbox_id,
            exc_info=True,
        )
        return
    if reply.get("captured"):
        logger.info(
            "pause of sandbox %s wrote checkpoint %s (%s MiB, pid %s, captured %s %s)",
            sandbox_id,
            reply.get("image"),
            reply.get("imageMB"),
            reply.get("pid"),
            reply.get("exe") or "<unknown>",
            reply.get("argv") or [],
        )
    else:
        logger.info(
            "pause of sandbox %s holds no checkpoint: %s",
            sandbox_id,
            reply.get("reason"),
        )


def _restore_outcome_of(reply: dict) -> dict:
    """The four facts a read-only query repeats, out of a resume's reply (E3).

    A resume has three honest endings and one of them used to be silent: the
    image came back (``restored`` with the pid and the fd count), there was no
    image (a reason), or the session never left this worker and was thawed --
    the reply's ``reason`` is empty there, and an empty reason beside
    ``restored: false`` reads like a failure the reader has to guess about. So
    the outcome says which one it was.
    """
    reason = str(reply.get("reason") or "")
    restored = bool(reply.get("restored"))
    if not restored and not reason:
        reason = (
            "nothing was restored from an image: this worker thawed a session "
            "that was still here"
        )
    count = reply.get("unrecoveredFdCount")
    if count is None:
        count = len(reply.get("unrecoveredFds") or [])
    return {
        "restored": restored,
        "reason": reason,
        "pid": reply.get("pid"),
        "unrecoveredFdCount": count,
    }


async def _record_restore_outcome(
    settings: Settings, request: Request, sandbox_id: str, reply: dict
) -> None:
    """Let the read-only query answer for this resume (E3).

    Off the event loop for the same reason the resume itself is: the file lives
    on the network-backed volume. Failure to record is a warning, never a
    change to what the resume just did -- the platform's rule for bookkeeping
    that sits next to a delivery (compare the capture path's own reasons).
    """
    try:
        await asyncio.to_thread(
            record_restore_outcome,
            _registry_workspace_base(request.app.state.runtime_registry, settings),
            sandbox_id,
            _restore_outcome_of(reply),
            state_base=_registry_state_base(
                request.app.state.runtime_registry, settings
            ),
        )
    except Exception:  # noqa: BLE001 - a note cannot fail the thing it describes
        logger.warning(
            "resume of sandbox %s could not record its outcome",
            sandbox_id,
            exc_info=True,
        )


async def _resume_process_tree(
    settings: Settings, request: Request, sandbox_id: str
) -> None:
    """Thaw the session that is here, resume the image that is not (S3/D5).

    Priming the runtime context first is what makes a resume after a *worker
    restart* possible at all: the worker that comes up has no context for a
    sandbox the control plane paused on the worker that went away. Priming
    builds no session by itself (the executor's instance is lazy), so this stays
    cheap for the ordinary thaw of a sandbox that never moved.
    """
    await _prime_runtime_context(request, sandbox_id)
    ctx = request.app.state.runtimes.get(sandbox_id)
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    try:
        reply = await asyncio.to_thread(
            resume_sandbox,
            _registry_workspace_base(request.app.state.runtime_registry, settings),
            ctx,
            sandbox_id,
            owner_uid=getattr(runtime, "host_uid", None),
            state_base=_registry_state_base(
                request.app.state.runtime_registry, settings
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the resume delivery still succeeds
        logger.warning(
            "resume of sandbox %s could not bring its process back from the "
            "checkpoint; it is running with an empty process tree",
            sandbox_id,
            exc_info=True,
        )
        # E3: the failure is recorded too -- "why did the last resume bring
        # nothing back" is the question the read-only query exists for.
        await _record_restore_outcome(
            settings,
            request,
            sandbox_id,
            {"restored": False, "reason": f"{type(exc).__name__}: {exc}"},
        )
        return
    await _record_restore_outcome(settings, request, sandbox_id, reply)
    if reply.get("restored"):
        logger.info(
            "resume of sandbox %s resumed image %s into the session (pid %s); "
            "%s fd(s) could not come back",
            sandbox_id,
            reply.get("image"),
            reply.get("pid"),
            reply.get("unrecoveredFdCount"),
        )
    elif reply.get("staleImageRemoved"):
        logger.info(
            "resume of sandbox %s dropped the checkpoint image its live session "
            "made stale",
            sandbox_id,
        )
    else:
        logger.info(
            "resume of sandbox %s had no image to resume (%s)",
            sandbox_id,
            reply.get("reason") or "nothing was checkpointed",
        )


async def _pause_reason_from_body(request: Request) -> str | None:
    """The optional ``reason`` of a pause push; ``None`` for no/!JSON body.

    Best effort on purpose: the reason is diagnostic text, and a control
    plane that sends a malformed body must still get its sandbox frozen.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an absent or malformed body
        return None
    if not isinstance(body, dict):
        return None
    reason = body.get("reason")
    return reason if isinstance(reason, str) and reason else None


@router.post("/agent/sandboxes/{sandbox_id}/pause", status_code=204)
async def agent_pause_sandbox(sandbox_id: str, request: Request) -> Response:
    """Freeze the sandbox's running exec child groups on this worker."""
    return await _agent_set_paused(request, sandbox_id, paused=True)


@router.post("/agent/sandboxes/{sandbox_id}/resume", status_code=204)
async def agent_resume_sandbox(sandbox_id: str, request: Request) -> Response:
    """Thaw the sandbox's paused exec child groups on this worker."""
    return await _agent_set_paused(request, sandbox_id, paused=False)


@router.post("/agent/sandboxes/{sandbox_id}/checkpoint")
async def agent_checkpoint_sandbox(sandbox_id: str, request: Request) -> Response:
    """Write this sandbox's checkpoint image on this worker (S2/D1-D3).

    Where the engine and the deployment meet: the capture itself happens in the
    sandbox's slot (its process tree belongs there), while the path, the
    ownership and the account the bytes are billed to are this worker's -- see
    :mod:`envd_service.runtime.checkpoint_store`.

    Contract, same delivery shape as the pause/resume pushes:

    * 401 without the internal key; 404 when this worker has no record of the
      sandbox (there is nothing to capture and nothing to bill).
    * 200 with ``captured: true`` and the image path, pid, fd count and the
      platform account's numbers, or ``captured: false`` **with a reason**:
      over the platform's checkpoint account, no live session on this worker,
      an executor with no checkpoint verb, or a slot that refused (a session
      with more than one live child, an older ``sandlock-supervise``). A refusal
      is a normal answer -- the sandbox is left exactly as it was found, which
      for a pause means frozen in place, today's behaviour.
    * 500 only for an unexpected failure, with the reason in the body.
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
    try:
        reply = await asyncio.to_thread(
            capture_checkpoint_image,
            _registry_workspace_base(request.app.state.runtime_registry, settings),
            ctx,
            sandbox_id,
            owner_uid=getattr(runtime, "host_uid", None),
            state_base=_registry_state_base(
                request.app.state.runtime_registry, settings
            ),
        )
    except Exception as exc:  # noqa: BLE001 - reported, never a silent 200
        logger.exception("agent checkpoint failed for sandbox %s", sandbox_id)
        return Response(
            status_code=500, content=f"{type(exc).__name__}: {exc}"[:500]
        )
    return JSONResponse(reply)


@router.get("/agent/sandboxes/{sandbox_id}/checkpoint")
async def agent_checkpoint_status(sandbox_id: str, request: Request) -> Response:
    """Read-only: what this worker knows about the sandbox's image (E3).

    The other half of :func:`agent_checkpoint_sandbox`: that one writes the
    image, this one reports it -- whether there is one, how big it is, when it
    appeared, and how the last resume went (``lastRestore``, which is where a
    user finally sees the fds D6 says a restored process cannot bring back).

    Delivery contract, the same shape as the write endpoint: 401 without the
    internal key, 404 for a sandbox this worker holds no record of (it has no
    image and no restore history to speak about), 200 with the answer
    otherwise. It changes nothing -- no file is written, no state is touched --
    so a control plane may ask it at any time.
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        return Response(status_code=404)
    # Off the event loop: ``imageMB`` is a directory walk, and on the
    # network-backed volume that walk is the expensive part of this answer.
    reply = await asyncio.to_thread(
        checkpoint_status,
        _registry_workspace_base(request.app.state.runtime_registry, settings),
        sandbox_id,
        state_base=_registry_state_base(request.app.state.runtime_registry, settings),
    )
    return JSONResponse(reply)


@router.post("/agent/sandboxes/{sandbox_id}/restore")
async def agent_restore_sandbox(sandbox_id: str, request: Request) -> Response:
    """Resume this sandbox's checkpoint image into a session on this worker (S2/S4).

    The image is consumed on success, and the reply carries the engine's own
    ``unrecoveredFds`` list: a restored process has no sockets, pipes or memfds
    left, so "what could not come back" is part of the result rather than
    something a caller infers from the first failed read (D6).

    A worker that already holds a live session for this sandbox refuses
    (``restored: false``), because resuming an image next to a running process
    would quietly give the sandbox two of them; the resume lifecycle thaws in
    that case instead (see :func:`_resume_process_tree`).
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        return Response(status_code=404)
    # A worker that has just come up has no context yet, and the restore is
    # exactly the case that needs one.
    await _prime_runtime_context(request, sandbox_id)
    ctx = request.app.state.runtimes.get(sandbox_id)
    try:
        reply = await asyncio.to_thread(
            restore_checkpoint_image,
            _registry_workspace_base(request.app.state.runtime_registry, settings),
            ctx,
            sandbox_id,
            owner_uid=getattr(runtime, "host_uid", None),
            state_base=_registry_state_base(
                request.app.state.runtime_registry, settings
            ),
        )
    except Exception as exc:  # noqa: BLE001 - reported, never a silent 200
        logger.exception("agent restore failed for sandbox %s", sandbox_id)
        return Response(
            status_code=500, content=f"{type(exc).__name__}: {exc}"[:500]
        )
    # E3: the restore endpoint is the other way an image comes back (the resume
    # lifecycle is the first), and its outcome is what the read-only query
    # repeats -- so it is recorded here too.
    await _record_restore_outcome(settings, request, sandbox_id, reply)
    return JSONResponse(reply)


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
    # N57: the staging directory is read by the *target* node's agent, so it
    # hangs off the platform namespace root rather than off the tree root.
    migrate_dir = migrate_staging_dir(
        settings.workspace_base, shared_root=settings.shared_volume_root
    )
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

        client = _agent_fileops()
        if client is not None:
            # C3 Task 4: same step, the agent's execution (and the same
            # "no path in the request" rule). Idempotent for the same reason the
            # teardown is: an import is retried, and "there was nothing there"
            # is exactly what this branch is for.
            #
            # ⚠ Off the event loop (third review, I-2): this is a synchronous
            # control-plane round trip whose read budget is the *file-op* one
            # (minutes, because a whole tree is being removed). Inline it parked
            # every heartbeat and every sandbox API on this worker -- the same
            # class as ``/metrics``, and the reason the create/delete handlers
            # already run their work in a thread.
            await asyncio.to_thread(
                _remove_agent_half,
                client,
                sandbox_id,
                path=workspace,
                op="remove_workspace",
                what="tree",
            )
        else:
            await asyncio.to_thread(priv_helpers.remove_tree, workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    # N57: same root as the export side -- this is the directory the control
    # plane staged the archive into from the source node.
    migrate_dir = migrate_staging_dir(
        settings.workspace_base, shared_root=settings.shared_volume_root
    )
    migrate_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = migrate_dir / f"{sandbox_id}.tar.gz"
    try:
        tmp_path.write_bytes(body)
        logger.info(
            "import %s: received %d bytes",
            sandbox_id,
            len(body),
        )
        extract_sandbox_archive(tmp_path, workspace)
    except (ArchiveRefusal, OSError, tarfile.TarError) as e:
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
    # The command log is a platform file: it lives in ``_runtime/<id>/`` beside
    # the tree, not inside it (the sandbox owns the tree and could rewrite or
    # delete anything in there). The in-tree path is the pre-split location and
    # is still read so a rolling upgrade does not lose a sandbox's history.
    registry = request.app.state.runtime_registry
    log_path = sandbox_command_log_path(
        _registry_workspace_base(registry, settings),
        sandbox_id,
        state_base=_registry_state_base(registry, settings),
    )
    if not log_path.is_file():
        # The pre-split location is inside the sandbox's own tree, so this one
        # is a workspace-base question (the state base plays no part in it).
        log_path = sandbox_command_log_path(
            _registry_workspace_base(registry, settings), sandbox_id, legacy=True
        )
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
    agent = getattr(request.app.state, "node_agent", None)
    payload["nodeID"] = getattr(agent, "_declared_node_id", None) or os.getenv(
        "E2B_NODE_ID"
    )
    return payload


def _write_snapshot_tar(src: Path, dst: Path) -> None:
    """Write one tar of a sandbox tree root, aside -> fsync -> rename.

    The same discipline the ``_oci.tar`` payloads and every record file in this
    repo keep, and for the same reason: a payload that is being written must
    look like a temp file, never like a finished snapshot. The caller writes
    ``.complete`` only after this returns, so the two together are what makes
    "the directory exists" mean "the snapshot is complete" (N29).

    The tar's members are the tree root's **own entries** (``workspace/…``),
    not a wrapper directory: the tar *is* the tree root, exactly as the
    exploded ``fs/`` directory was, which is what lets the reader unpack it
    straight into the sandbox's tree.
    """
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with tarfile.open(tmp, "w") as tar:
            for entry in sorted(src.iterdir(), key=lambda item: item.name):
                tar.add(entry, arcname=entry.name, recursive=True)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    # The rename itself has to reach the NAS, not just the page cache: the
    # control plane writes the record (and asks the agent for the payload) from
    # *another* node.
    dir_fd = os.open(dst.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
        snapshot_dir = snapshot_payload_dir(
            settings.workspace_base,
            snapshot_id,
            shared_root=settings.shared_volume_root,
        )
        dst = snapshot_dir / SNAPSHOT_PAYLOAD_TAR_NAME
        legacy = snapshot_dir / SNAPSHOT_PAYLOAD_DIR_NAME
        marker = snapshot_dir / ".complete"
        if not src.is_dir():
            return Response(status_code=404, content=f"Sandbox {sandbox_id} not found")
        if dst.exists() or legacy.exists():
            # N29: idempotent **for a finished copy**. The control plane retries
            # the same id when its own client timed out, and paying a second
            # full copy of a large tree is exactly what that retry must not do.
            # The marker is what makes "the directory exists" mean "complete":
            # without it this is a crashed attempt, and 409 keeps the rule that
            # one id has one live copy (a concurrent duplicate is refused
            # rather than interleaved). ``legacy`` is the pre-tar ``fs/``
            # payload: an id that already has one is a snapshot this worker
            # must not write a second payload beside.
            if marker.is_file():
                return JSONResponse(
                    status_code=200,
                    content={
                        "snapshotID": snapshot_id,
                        "sandboxID": sandbox_id,
                        "status": "completed",
                        "alreadyExists": True,
                    },
                )
            return Response(status_code=409, content="snapshot already exists")
        # Off the event loop: a snapshot reads the whole sandbox tree and writes
        # it to the shared NAS (measured 2026-10-02: 2000 small files cost
        # ~26-29 ms *per entry* where each entry is an NFS round trip, which
        # takes longer than the entry proxy's timeout). Running it inline
        # stalled every heartbeat and every other request on this worker for
        # the whole copy -- the loop stopped answering, which is what made a
        # snapshot look like a worker outage.
        #
        # Task 2: the payload is **one tar**. Reading the tree is still
        # per-entry, but the write side becomes one sequential file, so the
        # cost of the payload itself stops scaling with the entry count.
        try:
            # The store directory is the agent's to make: the control plane
            # writes the record *after* this returns, so nothing created it for
            # us (the old ``copytree`` did it as a side effect of the
            # destination being the payload path).
            await asyncio.to_thread(snapshot_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_write_snapshot_tar, src, dst)
            # Written last, and that is the whole point: until this line lands,
            # the payload is not a snapshot (see the 409 above).
            await asyncio.to_thread(marker.write_text, "complete\n", encoding="utf-8")
        except BaseException:
            # A failed or abandoned write must not leave a half snapshot
            # behind: the record is written by the control plane only on
            # success, so a partial payload would be an orphan nothing ever
            # reclaims (and the next attempt for the same id would answer 409
            # "already exists"). The tar's own temp name has been removed by
            # ``_write_snapshot_tar``; this removes the store directory.
            await asyncio.to_thread(
                shutil.rmtree,
                snapshot_dir,
                True,
            )
            raise
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
    # The payload is a full tree on the shared NAS; removing it here on the
    # loop is the same stall the control plane's own delete had (N32), one
    # level down.
    await asyncio.to_thread(
        shutil.rmtree,
        snapshot_payload_dir(
            settings.workspace_base,
            snapshot_id,
            shared_root=settings.shared_volume_root,
        ),
        True,
    )
    return Response(status_code=204)
