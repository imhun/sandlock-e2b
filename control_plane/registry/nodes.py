"""Compute node registry: capabilities, reservations and health."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from control_plane.scheduler import rank_candidates
from gateway_common.ids import sandbox_id

logger = logging.getLogger(__name__)


@dataclass
class NodeRecord:
    node_id: str
    address: str  # "local://" for the in-process worker, else http(s) URL
    total_memory_mb: int = 0
    total_cpu_percent: int = 0
    total_disk_mb: int = 0
    total_processes: int = 0
    #: N83 phase 2 (D5/D5b, ruling R17): the *promise* side of this node's
    #: sandbox sizing -- the largest values a single sandbox may be configured
    #: to here. This is the **control plane's** policy, not the worker's, and
    #: it is resolved **for this node** (``Settings.sandbox_ceiling_for``): an
    #: explicit ``E2B_MAX_SANDBOX_*``, else this row's own totals, else the
    #: per-sandbox create default. It is therefore not one number shared by
    #: every row -- unequal nodes get unequal ceilings -- and the number here
    #: is written by the control plane at register/heartbeat. A ``0`` is a row
    #: the control plane has not written yet (explicitly built records,
    #: embedders) -- never "unlimited".
    sandbox_cpu_percent_max: int = 0
    sandbox_memory_mb_max: int = 0
    sandbox_processes_max: int = 0
    #: ...and the *physical* side: the kernel's own read of this worker's
    #: container cgroup (``cpu.max``/``memory.max``). ``None`` = the kernel sets
    #: no limit on that dimension (the compose lanes' measured shape) *or* the
    #: worker has not reported it; the two are kept apart by ``heartbeat_at``,
    #: not by a sentinel -- a reported ``max`` is a real answer.
    kernel_cpu_percent: int | None = None
    kernel_memory_mb: int | None = None
    #: N83 phase 2 (Task 5): the kernel's own per-sandbox event counters, as
    #: this node reported them -- ``{sandbox id: {counter: count}}`` from the
    #: ``memory.events``/``pids.events`` inside that sandbox's cgroup. Only
    #: sandboxes that hit a wall are listed, and every counter only ever moves
    #: forward here (``apply_sandbox_events``), because "the count grew" *is*
    #: the event: a sandbox that overran ``memory.max`` was SIGKILLed while its
    #: record still read ``running``.
    sandbox_events: dict[str, dict[str, int]] = field(default_factory=dict)
    used_disk_mb: int = 0
    quota_over_limit: list[int] = field(default_factory=list)
    quota_near_limit: list[int] = field(default_factory=list)
    quota_over_limit_count: int = 0
    quota_near_limit_count: int = 0
    disk_total_mb: int = 0
    disk_warn_count: int = 0
    disk_error_count: int = 0
    #: N8: the worker's MCP gateway port band (61001-65535) usage, shipped by
    #: the heartbeat. ``capacity`` is the band's hard ceiling, so the watermark
    #: an operator watches is ``mcp_ports_in_use / mcp_ports_capacity``.
    mcp_ports_in_use: int = 0
    mcp_ports_capacity: int = 0
    #: S2/D3: the platform's **checkpoint account** as this node sees it --
    #: everything under the shared ``_runtime`` (checkpoint images), billed to
    #: nobody's ``diskMB`` and bounded by ``E2B_PLATFORM_DISK_MB`` on the workers.
    #: ``budget`` of 0 is unlimited, so the pair is read as usage-of-budget the
    #: same way the MCP band is.
    platform_disk_used_mb: int = 0
    platform_disk_budget_mb: int = 0
    reserved_memory_mb: int = 0
    reserved_cpu_percent: int = 0
    reserved_disk_mb: int = 0
    reserved_processes: int = 0
    draining: bool = False
    images: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    #: C3 Task 3 (ruling D9.3): the worker's own pid namespace identity
    #: (``pid:[4026532458]``), reported at register/heartbeat. It is what makes
    #: the agent's container-pid → host-pid lookup unambiguous when one host
    #: runs several workers, and it is refreshed on every heartbeat because a
    #: restarted worker container has a new inode under the *same* node id.
    pid_namespace: str | None = None
    #: C3 Task 4 / ruling D25: the worker's **container identity** (its
    #: hostname, i.e. a prefix of the container id), reported at
    #: register/heartbeat. It is the **file-operation** path's anchor: face B is
    #: root without ``CAP_SYS_PTRACE`` and cannot read another uid's
    #: ``/proc/<pid>/ns/pid``, but a candidate's host-side ``/proc/<pid>/cgroup``
    #: is world-readable and carries this id. Refreshed on every heartbeat for
    #: the same reason ``pid_namespace`` is: a recreated worker container is a
    #: new container id under the same node id.
    container_id: str | None = None
    #: C3 Task 4: the worker's own uid/gid, reported at register/heartbeat the
    #: same way its pid namespace is. Face B's file operations need them --
    #: ``e2b-maint chown --uid X --gid <worker gid>`` puts a sandbox tree in
    #: the group the worker (the data-plane owner) reads it through, and
    #: ``chown --worker`` keeps the owner as the worker itself. They come from
    #: this record, never from the request (hard rule 3): a worker may not name
    #: the identity a privileged step acts as.
    worker_uid: int | None = None
    worker_gid: int | None = None
    status: str = "healthy"
    heartbeat_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    def to_storage_dict(self) -> dict[str, Any]:
        """The record as the shared node view carries it (F11 step 1).

        Every field is here, including the reservations: a replica that reads
        the view must be able to make the same placement decision the writer
        did. Health is *not* stored as a verdict -- readers derive it from
        ``heartbeat_at`` with the shared timeout, which is what keeps two
        replicas from disagreeing.
        """
        return {
            "node_id": self.node_id,
            "address": self.address,
            "total_memory_mb": self.total_memory_mb,
            "total_cpu_percent": self.total_cpu_percent,
            "total_disk_mb": self.total_disk_mb,
            "total_processes": self.total_processes,
            "sandbox_cpu_percent_max": self.sandbox_cpu_percent_max,
            "sandbox_memory_mb_max": self.sandbox_memory_mb_max,
            "sandbox_processes_max": self.sandbox_processes_max,
            "kernel_cpu_percent": self.kernel_cpu_percent,
            "kernel_memory_mb": self.kernel_memory_mb,
            "sandbox_events": {
                sandbox_id: dict(counters)
                for sandbox_id, counters in self.sandbox_events.items()
            },
            "used_disk_mb": self.used_disk_mb,
            "quota_over_limit": list(self.quota_over_limit),
            "quota_near_limit": list(self.quota_near_limit),
            "quota_over_limit_count": self.quota_over_limit_count,
            "quota_near_limit_count": self.quota_near_limit_count,
            "disk_total_mb": self.disk_total_mb,
            "disk_warn_count": self.disk_warn_count,
            "disk_error_count": self.disk_error_count,
            "mcp_ports_in_use": self.mcp_ports_in_use,
            "mcp_ports_capacity": self.mcp_ports_capacity,
            "platform_disk_used_mb": self.platform_disk_used_mb,
            "platform_disk_budget_mb": self.platform_disk_budget_mb,
            "reserved_memory_mb": self.reserved_memory_mb,
            "reserved_cpu_percent": self.reserved_cpu_percent,
            "reserved_disk_mb": self.reserved_disk_mb,
            "reserved_processes": self.reserved_processes,
            "draining": self.draining,
            "images": list(self.images),
            "labels": dict(self.labels),
            "pid_namespace": self.pid_namespace,
            "container_id": self.container_id,
            "worker_uid": self.worker_uid,
            "worker_gid": self.worker_gid,
            "status": self.status,
            "heartbeat_at": self.heartbeat_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "NodeRecord":
        """Inverse of :meth:`to_storage_dict`, tolerant of an older row.

        A missing field reads as the dataclass default (a view written by an
        older replica during a rollout must not take the fleet down).
        """
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def can_fit(self, memory_mb: int, cpu: int, disk_mb: int, processes: int) -> bool:
        return self.blocking_dimension(memory_mb, cpu, disk_mb, processes) is None

    def blocking_dimension(
        self, memory_mb: int, cpu: int, disk_mb: int, processes: int
    ) -> str | None:
        """The dimension that makes this sandbox not fit, or ``None``.

        Same order as :meth:`can_fit` (which is now defined in terms of this),
        so callers can explain a refusal with the dimension the check actually
        stopped on instead of guessing afterwards. The name matches the
        ledger's: ``"memory" | "cpu" | "disk" | "processes"``.
        """
        if self.total_memory_mb > 0 and self.reserved_memory_mb + memory_mb > self.total_memory_mb:
            return "memory"
        if self.total_cpu_percent > 0 and self.reserved_cpu_percent + cpu > self.total_cpu_percent:
            return "cpu"
        if self.total_disk_mb > 0 and self.reserved_disk_mb + disk_mb > self.total_disk_mb:
            return "disk"
        if self.total_processes > 0 and self.reserved_processes + processes > self.total_processes:
            return "processes"
        return None

    def reserve(self, memory_mb: int, cpu: int, disk_mb: int, processes: int) -> None:
        self.reserved_memory_mb += memory_mb
        self.reserved_cpu_percent += cpu
        self.reserved_disk_mb += disk_mb
        self.reserved_processes += processes

    def release(self, memory_mb: int, cpu: int, disk_mb: int, processes: int) -> None:
        self.reserved_memory_mb = max(0, self.reserved_memory_mb - memory_mb)
        self.reserved_cpu_percent = max(0, self.reserved_cpu_percent - cpu)
        self.reserved_disk_mb = max(0, self.reserved_disk_mb - disk_mb)
        self.reserved_processes = max(0, self.reserved_processes - processes)

    def update_usage(
        self,
        *,
        used_disk_mb: int | None = None,
        disk_total_mb: int | None = None,
        quota_over_limit: list[int] | None = None,
        quota_near_limit: list[int] | None = None,
        quota_over_limit_count: int | None = None,
        quota_near_limit_count: int | None = None,
        disk_warn_count: int | None = None,
        disk_error_count: int | None = None,
        mcp_ports_in_use: int | None = None,
        mcp_ports_capacity: int | None = None,
        platform_disk_used_mb: int | None = None,
        platform_disk_budget_mb: int | None = None,
    ) -> None:
        """Store the worker heartbeat's disk/quota usage snapshot."""
        if used_disk_mb is not None:
            self.used_disk_mb = int(used_disk_mb)
        if disk_total_mb is not None:
            self.disk_total_mb = int(disk_total_mb)
        if quota_over_limit is not None:
            self.quota_over_limit = list(quota_over_limit)
        if quota_near_limit is not None:
            self.quota_near_limit = list(quota_near_limit)
        if quota_over_limit_count is not None:
            self.quota_over_limit_count = int(quota_over_limit_count)
        if quota_near_limit_count is not None:
            self.quota_near_limit_count = int(quota_near_limit_count)
        if disk_warn_count is not None:
            self.disk_warn_count = int(disk_warn_count)
        if disk_error_count is not None:
            self.disk_error_count = int(disk_error_count)
        if mcp_ports_in_use is not None:
            self.mcp_ports_in_use = int(mcp_ports_in_use)
        if mcp_ports_capacity is not None:
            self.mcp_ports_capacity = int(mcp_ports_capacity)
        if platform_disk_used_mb is not None:
            self.platform_disk_used_mb = int(platform_disk_used_mb)
        if platform_disk_budget_mb is not None:
            self.platform_disk_budget_mb = int(platform_disk_budget_mb)

    def apply_sandbox_ceiling(self, ceiling: Mapping[str, Any]) -> None:
        """Store the per-sandbox ceiling's two halves -- side by side, one owner
        each (N83 phase 2 / R17).

        The **policy** half (``cpuPercent``/``memoryMB``/``processes``) is the
        control plane's own policy for **this node**, resolved against the
        totals that node reported (``Settings.sandbox_ceiling_for``: explicit
        ``E2B_MAX_SANDBOX_*`` first). The internal API computes it once per
        register/heartbeat and passes it here, so what a create is checked
        against is the deployment's decision and not whatever a worker
        believes. The **kernel** half (``kernelCpuPercent``/``kernelMemoryMB``)
        is the worker's read of its own container cgroup and is passed through
        verbatim; a key that is **absent** leaves the stored value alone, so a
        heartbeat that carries no kernel read (an older worker during a
        rollout) cannot erase one the record already holds. A key that is
        *present but null* is the worker saying "the kernel sets no limit here"
        (the compose lanes' measured shape) and is stored as such.

        They are stored together on purpose: an operator comparing the two sees
        a node whose policy is only bounded by the platform's ledger.
        """
        self.sandbox_cpu_percent_max = int(ceiling["cpuPercent"])
        self.sandbox_memory_mb_max = int(ceiling["memoryMB"])
        self.sandbox_processes_max = int(ceiling["processes"])
        if "kernelCpuPercent" in ceiling:
            kernel_cpu = ceiling["kernelCpuPercent"]
            self.kernel_cpu_percent = None if kernel_cpu is None else int(kernel_cpu)
        if "kernelMemoryMB" in ceiling:
            kernel_memory = ceiling["kernelMemoryMB"]
            self.kernel_memory_mb = None if kernel_memory is None else int(kernel_memory)

    #: How many sandboxes' event counters one node record keeps. Only boxes that
    #: hit a wall are stored at all, so this bounds a node that has lived for
    #: months rather than a busy one; an evicted box that reports again is stored
    #: from zero, which is why the warning below names the absolute count.
    SANDBOX_EVENTS_MAX = 256

    def apply_sandbox_events(self, events: Mapping[str, Mapping[str, int]]) -> None:
        """Store the worker's kernel event counters, monotonic, and name them.

        ``events`` is the heartbeat's ``sandboxEvents``: per sandbox, the
        counters inside that sandbox's own cgroup -- ``memory.events``'s
        ``oom_kill``/``oom_group_kill`` (an over-budget sandbox was SIGKILLed,
        plan D3) and ``pids.events``'s ``max`` (a *task* creation hit
        ``pids.max`` and got ``EAGAIN``; tasks, so threads count, plan D4).

        **The count growing is the event**, and it gets exactly one WARN naming
        the sandbox, the counter and both numbers. That line is the whole point
        of the task (plan Review Focus §4): the sandbox's own record still says
        ``running`` after a kernel kill, so without it the user's report is "the
        process mysteriously disappeared".

        A **smaller** report never lowers what is stored, and is not refused by
        name either (the plan allowed both): this record may simply be newer
        than the worker reporting to it -- a rollout, or a worker restart whose
        cgroup subtree came up fresh -- and failing a heartbeat over a counter
        would cost the node its liveness, which orphans its live sandboxes. It
        is logged at debug rather than warned about, because a worker that keeps
        reporting less would otherwise warn on every single heartbeat.
        """
        for sandbox_id, counters in events.items():
            stored = dict(self.sandbox_events.get(sandbox_id, {}))
            for name, value in counters.items():
                previous = int(stored.get(name, 0))
                if value < previous:
                    logger.debug(
                        "node %s: sandbox %s reported %s=%d, keeping the "
                        "maximum %d already seen",
                        self.node_id,
                        sandbox_id,
                        name,
                        value,
                        previous,
                    )
                    continue
                if value > previous:
                    logger.warning(
                        "sandbox %s on node %s: %s grew from %d to %d -- the "
                        "kernel's own account of this sandbox hitting its cgroup "
                        "wall (memory.events/pids.events; the sandbox record may "
                        "still read 'running')",
                        sandbox_id,
                        self.node_id,
                        name,
                        previous,
                        value,
                    )
                    stored[name] = value
            if stored:
                # Re-insert so eviction below is least-recently-reported first.
                self.sandbox_events.pop(sandbox_id, None)
                self.sandbox_events[sandbox_id] = stored
        while len(self.sandbox_events) > self.SANDBOX_EVENTS_MAX:
            evicted = next(iter(self.sandbox_events))
            del self.sandbox_events[evicted]
            logger.debug(
                "node %s: dropping the event counters of sandbox %s (keeping "
                "the %d most recently reported)",
                self.node_id,
                evicted,
                self.SANDBOX_EVENTS_MAX,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeID": self.node_id,
            "address": self.address,
            "status": self.status,
            "labels": self.labels,
            "images": self.images,
            "totalMemoryMB": self.total_memory_mb,
            "reservedMemoryMB": self.reserved_memory_mb,
            "totalCPUPercent": self.total_cpu_percent,
            "reservedCPUPercent": self.reserved_cpu_percent,
            "totalDiskMB": self.total_disk_mb,
            "usedDiskMB": self.used_disk_mb,
            "diskTotalMB": self.disk_total_mb,
            "diskWarnCount": self.disk_warn_count,
            "diskErrorCount": self.disk_error_count,
            "reservedDiskMB": self.reserved_disk_mb,
            "quotaOverLimit": self.quota_over_limit,
            "quotaNearLimit": self.quota_near_limit,
            "quotaOverLimitCount": self.quota_over_limit_count,
            "quotaNearLimitCount": self.quota_near_limit_count,
            "mcpPortsInUse": self.mcp_ports_in_use,
            "mcpPortsCapacity": self.mcp_ports_capacity,
            "platformDiskUsedMB": self.platform_disk_used_mb,
            "platformDiskBudgetMB": self.platform_disk_budget_mb,
            "totalProcesses": self.total_processes,
            "reservedProcesses": self.reserved_processes,
            "sandboxCPUPercentMax": self.sandbox_cpu_percent_max,
            "sandboxMemoryMBMax": self.sandbox_memory_mb_max,
            "sandboxProcessesMax": self.sandbox_processes_max,
            "kernelCPUPercent": self.kernel_cpu_percent,
            "kernelMemoryMB": self.kernel_memory_mb,
            "sandboxEvents": {
                sandbox_id: dict(counters)
                for sandbox_id, counters in self.sandbox_events.items()
            },
            "draining": self.draining,
        }


#: How recently a node must have heartbeated to be *given new work*.
#:
#: Deliberately separate from ``heartbeat_timeout``, which decides when a node's
#: sandboxes are treated as orphaned -- that window is generous on purpose,
#: because wrongly orphaning a live sandbox takes its slot away (the lesson from
#: the N18 investigation). Handing new work to a node that is actually gone costs
#: the caller a 502 instead, so this window is short: the worker heartbeats every
#: 5s, so 15s is three missed beats. Measured on k0s 2026-09-17 with one shared
#: threshold (300s): restarting a worker left roughly a minute in which the dead
#: node was still "healthy" by the orphan definition, so creates were placed on
#: it and answered ``502 Node <id> unavailable``.
PLACEMENT_MAX_HEARTBEAT_AGE_S = 15.0

class NodeRegistry:
    def __init__(
        self,
        *,
        heartbeat_timeout: float = 15.0,
        redis_client=None,
        namespace: str = "e2b",
    ) -> None:
        self._nodes: dict[str, NodeRecord] = {}
        self._lock = threading.Lock()
        self._heartbeat_timeout = heartbeat_timeout
        self._redis = redis_client
        self._ns = namespace
        self._quota_store = None
        self._view = None
        #: N70: the atomic reservation counters (``e2b:node:res:<id>``), kept
        #: apart from the JSON view row so a heartbeat's read-modify-write of
        #: the row cannot clobber a release made by another replica.
        self._res_store = None
        if redis_client is not None:
            from control_plane.registry.redis_backend import (
                RedisNodeReservationStore,
                RedisNodeStore,
                RedisQuotaStore,
            )

            self._quota_store = RedisQuotaStore(redis_client, f"{namespace}:node")
            # F11 step 1: the view of the fleet lives in the shared store, so
            # every replica reads the same nodes, the same addresses and the
            # same reservations. The process dict stays as a cache for the
            # single-process deployment (no Redis).
            self._view = RedisNodeStore(redis_client, namespace)
            self._res_store = RedisNodeReservationStore(redis_client, namespace)
        #: A view nobody refreshes has to retire itself: the worker is gone,
        #: not quiet. Several heartbeat windows wide, so one lost reply cannot
        #: erase a live node from the fleet's view.
        self._view_ttl_s = max(60, int(self._heartbeat_timeout * 4))

    # -- shared view (F11 step 1) ------------------------------------------

    def _status_of(self, record: NodeRecord) -> NodeRecord:
        """Set ``record.status`` from its heartbeat stamp, and return it.

        Health is *derived*, by every reader, from the same shared timestamp
        and the same timeout -- which is what makes "healthy on replica A,
        unhealthy on replica B" impossible once the view is shared. The
        in-process (``local://``) node never heartbeats itself, so it is
        exempt, exactly as it is in the sweep.
        """
        if record.address != "local://" and (
            time.time() - record.heartbeat_at > self._heartbeat_timeout
        ):
            record.status = "unhealthy"
        return record

    def _load_locked(self, node_id: str) -> NodeRecord | None:
        """A node's record, read through the shared view when there is one.

        Callers hold ``self._lock``. Reading through the store is what makes a
        heartbeat -- or a drain, or a reservation -- work for a node another
        replica registered: this process's dict only ever knows the nodes this
        replica has already seen.
        """
        if self._view is not None:
            payload = self._view.get(node_id)
            if payload is not None:
                record = NodeRecord.from_storage_dict(payload)
                self._overlay_reservations([record])
                self._nodes[node_id] = record
                return record
        return self._nodes.get(node_id)

    def _overlay_reservations(self, records: list[NodeRecord]) -> None:
        """Take ``reserved_*`` from the atomic counter hashes (N70).

        The JSON row also carries ``reserved_*`` -- that is the pre-N70 shape,
        kept so a rolling upgrade reads sensibly in both directions -- but the
        row is one string written whole, so a heartbeat on one replica can
        overwrite a release made on another. The counter hash is the atomic
        copy and therefore wins wherever it exists; a node with no counter yet
        (an older replica's row) keeps the row's own fields as the fallback.

        The counters are read in one round trip, and the in-process
        (``local://``) node is skipped: it is never published to the shared
        store, so it can have no counter there.
        """
        shared = [r for r in records if r.address != "local://"]
        if self._res_store is None or not shared:
            return
        counters = self._res_store.get_many([r.node_id for r in shared])
        for record in shared:
            values = counters.get(record.node_id)
            if values is None:
                continue
            record.reserved_memory_mb = values.get(
                "memory", record.reserved_memory_mb
            )
            record.reserved_cpu_percent = values.get(
                "cpu", record.reserved_cpu_percent
            )
            record.reserved_disk_mb = values.get("disk", record.reserved_disk_mb)
            record.reserved_processes = values.get(
                "processes", record.reserved_processes
            )

    def _bump_reservation_counter(self, node_id: str, dims: dict[str, int]) -> None:
        """Move the atomic reservation counter (N70); no-op without Redis."""
        if self._res_store is None:
            return
        self._res_store.add(node_id, dims)

    def _persist_locked(self, record: NodeRecord) -> None:
        """Publish one node's view. Callers hold ``self._lock``.

        The in-process node is deliberately not published: it is a worker
        embedded in *this* replica, so another replica could only mis-place
        work on it (and every replica's row would collide on the id
        ``local``).
        """
        if self._view is None or record.address == "local://":
            return
        try:
            self._view.put(
                record.node_id, record.to_storage_dict(), ttl=self._view_ttl_s
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "could not publish the node view for %s", record.node_id, exc_info=True
            )

    def register(
        self,
        *,
        node_id: str | None = None,
        address: str,
        total_memory_mb: int,
        total_cpu_percent: int,
        total_disk_mb: int,
        total_processes: int,
        images: list[str] | None = None,
        labels: dict[str, str] | None = None,
        pid_namespace: str | None = None,
        container_id: str | None = None,
        worker_uid: int | None = None,
        worker_gid: int | None = None,
        sandbox_ceiling: Mapping[str, Any] | None = None,
    ) -> NodeRecord:
        with self._lock:
            record = self._load_locked(node_id) if node_id else None
            if record is None:
                node_id = node_id or sandbox_id().replace("sbx_", "node_")
                reserved = self._reserved_from_store(node_id)
                record = NodeRecord(
                    node_id=node_id,
                    address=address,
                    total_memory_mb=total_memory_mb,
                    total_cpu_percent=total_cpu_percent,
                    total_disk_mb=total_disk_mb,
                    total_processes=total_processes,
                    images=list(images or []),
                    labels=dict(labels or {}),
                    pid_namespace=pid_namespace,
                    container_id=container_id,
                    worker_uid=worker_uid,
                    worker_gid=worker_gid,
                    reserved_memory_mb=reserved.get("memory", 0),
                    reserved_cpu_percent=reserved.get("cpu", 0),
                    reserved_disk_mb=reserved.get("disk", 0),
                    reserved_processes=reserved.get("processes", 0),
                )
                self._nodes[node_id] = record
                if sandbox_ceiling is not None:
                    record.apply_sandbox_ceiling(sandbox_ceiling)
            else:
                record.address = address
                record.total_memory_mb = total_memory_mb
                record.total_cpu_percent = total_cpu_percent
                record.total_disk_mb = total_disk_mb
                record.total_processes = total_processes
                record.images = list(images or [])
                record.labels = dict(labels or {})
                # Only ever *set* here: a heartbeat that carries no identity
                # (an older worker during a rollout) must not erase the one the
                # record already holds -- that would make every slot grant on
                # this node fail closed until the next register.
                if pid_namespace is not None:
                    record.pid_namespace = pid_namespace
                # Same rule again for D25's anchor: only ever *set* here, so a
                # rollout of older workers does not erase a container id the
                # record already holds.
                if container_id is not None:
                    record.container_id = container_id
                # Same rule as the pid namespace above: only ever *set* here,
                # so a rollout of older workers does not erase an identity the
                # record already holds.
                if worker_uid is not None and worker_gid is not None:
                    record.worker_uid = worker_uid
                    record.worker_gid = worker_gid
                # N83 phase 2 (R17): the policy half of the ceiling is the
                # control plane's own and is written on every registration --
                # it is what a create is checked against, so a node whose row
                # carried none must not keep looking like an unlimited one.
                if sandbox_ceiling is not None:
                    record.apply_sandbox_ceiling(sandbox_ceiling)
                record.draining = False
            record.heartbeat_at = time.time()
            record.status = "healthy"
            self._persist_locked(record)
            return record

    def _reserved_from_store(self, node_id: str) -> dict[str, int]:
        """Restore reservations from Redis on (re)registration.

        Redis quota keys survive control-plane restarts, so a fresh in-memory
        record must start from the shared ledger; otherwise reservations made
        before the restart become invisible and later releases cannot clear
        them (in-memory/Redis drift, spurious 503s).
        """
        if self._quota_store is None:
            return {}
        try:
            return self._quota_store.get(node_id)
        except Exception:  # pragma: no cover - defensive
            return {}

    def heartbeat(self, node_id: str) -> NodeRecord | None:
        with self._lock:
            record = self._load_locked(node_id)
            if record is None:
                return None
            record.heartbeat_at = time.time()
            record.status = "healthy"
            self._persist_locked(record)
            return record

    def publish(self, record: NodeRecord) -> None:
        """Write a record back to the shared view after an in-place update.

        The heartbeat endpoint updates usage on the record ``heartbeat()``
        returned (disk, quota, MCP band, platform account) and then hands it
        back here, so the numbers every replica places work against are the
        ones the worker just reported.
        """
        with self._lock:
            self._nodes[record.node_id] = record
            self._persist_locked(record)

    def set_draining(self, node_id: str, draining: bool) -> NodeRecord | None:
        """Mark/unmark a node as draining; returns the record or ``None``."""
        with self._lock:
            record = self._load_locked(node_id)
            if record is None:
                return None
            record.draining = draining
            self._persist_locked(record)
            return record

    def set_reserved(
        self,
        node_id: str,
        *,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        processes: int,
    ) -> NodeRecord | None:
        """Restore reservation accounting from the sandbox registry.

        Node reservations live in memory while sandbox records persist in
        Redis; after a control-plane restart the reserved fields start at
        zero until workers re-register. Callers aggregate the node's active
        sandbox records and call this to keep fleet utilization accurate and
        avoid over-committing nodes.
        """
        with self._lock:
            record = self._load_locked(node_id)
            if record is None:
                return None
            record.reserved_memory_mb = max(0, memory_mb)
            record.reserved_cpu_percent = max(0, cpu_percent)
            record.reserved_disk_mb = max(0, disk_mb)
            record.reserved_processes = max(0, processes)
            # N70: the counter hash is the atomic copy of these four fields, so
            # a registration reconciliation has to move it too -- writing only
            # the row would let the next heartbeat's read-modify-write restore
            # the value this call just corrected.
            if self._res_store is not None:
                self._res_store.set(
                    record.node_id,
                    {
                        "memory": record.reserved_memory_mb,
                        "cpu": record.reserved_cpu_percent,
                        "disk": record.reserved_disk_mb,
                        "processes": record.reserved_processes,
                    },
                )
            self._persist_locked(record)
            return record

    def reconcile_quota_ledger(
        self,
        node_id: str,
        *,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        processes: int,
    ) -> dict[str, int]:
        """Bring the **shared** quota ledger for one node back to the records.

        :meth:`set_reserved` heals the in-memory view on re-registration; this
        is the other half (N59): the Redis ledger the other replica also checks
        had no reconciliation path and no TTL, so a leaked reservation could
        only be cleared by hand -- and because ``select_and_reserve`` gives up
        when the store refuses instead of trying the next candidate, a few
        leaked slots turned into a fleet-wide ``503``. Both halves are now
        rebuilt from the same aggregate, in the same call site
        (``control_plane.api.internal._rebuild_node_reservations``).

        Returns the signed per-dimension deltas the store applied ({} when the
        ledger already agreed), so the caller can name a correction instead of
        rewriting a ledger quietly.

        Deliberately **not** merged into ``set_reserved``: that one runs for
        paths that have no shared ledger (a deployment without Redis), and a
        ``None`` store must stay a no-op rather than an error.
        """
        if self._quota_store is None:
            return {}
        try:
            return self._quota_store.reconcile(
                node_id,
                {
                    "memory": max(0, memory_mb),
                    "cpu": max(0, cpu_percent),
                    "disk": max(0, disk_mb),
                    "processes": max(0, processes),
                },
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            # Registration must not fail over the ledger (this runs inside the
            # worker's ``register`` call): a store that is down, or that kept
            # losing its WATCH race, leaves the row at its old value -- the
            # over-counting (safe) direction -- and the next re-registration
            # tries again. Same discipline as ``_reserved_from_store`` and
            # ``_persist_locked``.
            logger.warning(
                "node %s re-registered but the shared quota ledger could not be "
                "reconciled to the records (left at its old value): %s",
                node_id,
                exc,
            )
            return {}

    def get(self, node_id: str) -> NodeRecord | None:
        self._sweep_health()
        with self._lock:
            record = self._load_locked(node_id)
            return None if record is None else self._status_of(record)

    def list(self, *, healthy_only: bool = False) -> list[NodeRecord]:
        self._sweep_health()
        with self._lock:
            if self._view is not None:
                records = [
                    NodeRecord.from_storage_dict(payload)
                    for payload in self._view.list()
                ]
                # This replica's own embedded worker is not in the shared view
                # (see `_persist_locked`), so it is added back here for the
                # local API and the local placement.
                records.extend(
                    r for r in self._nodes.values() if r.address == "local://"
                )
                self._overlay_reservations(records)
                for record in records:
                    self._nodes[record.node_id] = record
                    self._status_of(record)
            else:
                records = list(self._nodes.values())
        if healthy_only:
            records = [r for r in records if r.status == "healthy"]
        return records

    def healthy_totals(self) -> dict[str, int] | None:
        """Σ of the healthy nodes' own ``total_*`` -- or ``None`` when empty.

        N83 phase 2 / Task 9 (the user's ruling, 2026-10-07: "max total 是不是
        没必要了，其实就是 worker 的上限加一起，可以自动计算"): an unset
        ``E2B_MAX_TOTAL_*`` is this sum. It is the *same* numbers the node
        ledger admits against (``NodeRecord.blocking_dimension``), which is
        what keeps the two ledgers from drifting -- and, because a create has
        to fit its node, it also makes the fleet gate unable to refuse first.
        The three derivable dimensions are memory/cpu/processes;
        ``SandboxRegistry._fleet_limits`` is where that list and the reason
        disk is *not* on it are written down.

        ``None`` is "no node has registered" and is deliberately *not* ``0``:
        ``0`` in these ledgers means "this dimension is not policed", while
        ``None`` says "there is nothing to derive from", which the fleet ladder
        then reads as "do not police this dimension here" -- a create in that
        state has nowhere to go and is refused by placement, by name (see
        ``SandboxRegistry._fleet_limits``).

        Read fresh on every call, deliberately -- a registration, a lost
        heartbeat or a node coming back must change the answer on the *next*
        create, not after a cache expires, and a cache is one more thing that
        could disagree with the ledger it is a copy of. Draining nodes are
        still counted: their totals bound the sandboxes already placed on them,
        and this sum may only ever be *generous* -- never smaller than what the
        node ladder can admit.

        One contract worth stating where the sum is computed: **a node's
        ``total_*`` are meant to be positive numbers.** ``0`` in these ledgers
        reads as "this dimension is not policed", and registration has no guard
        against it, so a node that reported ``0`` would leave that dimension
        unpoliced at *both* layers -- the platform's own workers cannot report
        ``0`` (``envd_service/agent.py`` only ever emits the positive numbers it
        read or probed), and "0 = unlimited" is not a supported reading of a
        node total. A registration-time refusal is deliberately not added here.
        """
        records = self.list(healthy_only=True)
        if not records:
            return None
        return {
            "memory": sum(r.total_memory_mb for r in records),
            "cpu": sum(r.total_cpu_percent for r in records),
            "disk": sum(r.total_disk_mb for r in records),
            "processes": sum(r.total_processes for r in records),
        }

    def _sweep_health(self) -> None:
        now = time.time()
        with self._lock:
            self._sweep_health_locked()

    def _sweep_health_locked(self) -> None:
        now = time.time()
        for record in self._nodes.values():
            if record.address == "local://":
                record.status = "healthy"
                continue
            if now - record.heartbeat_at > self._heartbeat_timeout:
                record.status = "unhealthy"

    def try_acquire_sweep(self, *, ttl_s: float) -> bool:
        """Claim this round of the health sweep (F11 step 2).

        One sweeper is enough now that the *view* is shared: the sweep marks
        sandboxes orphaned on nodes that every replica already sees as
        unhealthy, so a second replica running the same round is duplicated
        work -- and a duplicated ``orphaned sandboxes on …`` warning -- rather
        than extra coverage. The claim is a TTL'd key, so a sweeper that dies
        mid-round only costs the fleet one round.

        Without Redis there is nothing to share and the single process is the
        sweeper, so this answers ``True``.
        """
        if self._redis is None:
            return True
        try:
            claimed = self._redis.set(
                f"{self._ns}:node:sweep", str(time.time()), nx=True, ex=max(1, int(ttl_s))
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("health-sweep claim failed; sweeping anyway", exc_info=True)
            return True
        return bool(claimed)

    def reap_unhealthy(self, sandbox_registry) -> list[str]:
        """Mark sandboxes on unhealthy remote nodes as orphaned (E6.1).

        Called periodically from the control-plane lifespan. Local (in-
        process) nodes are never considered. Returns the node ids whose
        sandbox records were marked, for logging/metrics.

        The same pass drops a node row that has become *empty and stale*: an
        unhealthy node with no reservations and no records left cannot affect
        placement (it is not healthy), accounting (nothing reserved) or the
        fleet-wide enumeration the workers use to fence their sweeps (it
        contributes no ids) -- it is only a row an operator has to read past.
        Without this, every worker whose node id changed (a Deployment-era pod,
        before N20) left one behind for the life of the process.
        """
        with self._lock:
            # The verdict is a function of the *shared* stamp, not of this
            # replica's dict. A replica only learns a heartbeat when it
            # happens to serve one (the worker's 5 s cadence is split across
            # replicas), so judging from the cache alone declares a node dead
            # the moment 30 s pass without *this* replica handling any of its
            # heartbeats -- measured on the live cluster 2026-10-03: 30.6 s
            # gap on the round's winner while the worker was heartbeating
            # every 5 s into the shared row, and that node's live sandboxes
            # were orphaned. ``get()``/``list()`` already read through the
            # view (``_status_of`` is the same timeout on the same stamp);
            # this is the one path that could disagree with them, and it is
            # the destructive one.
            for node_id in list(self._nodes):
                self._load_locked(node_id)
            self._sweep_health_locked()
            node_ids = [
                n.node_id
                for n in self._nodes.values()
                if n.status == "unhealthy" and n.address != "local://"
            ]
        marked_nodes: list[str] = []
        for node_id in node_ids:
            if sandbox_registry.mark_orphaned(node_id):
                marked_nodes.append(node_id)
        pruned = self._prune_empty_unhealthy(node_ids, sandbox_registry)
        if pruned:
            logger.info(
                "node health sweep: dropped %d empty node row(s) quiet for "
                "more than %d heartbeat windows: %s",
                len(pruned),
                self._PRUNE_AFTER_WINDOWS,
                ", ".join(sorted(pruned)),
            )
        return marked_nodes

    #: How many heartbeat windows a node row stays after it went quiet *and* ran
    #: out of everything it was holding. Deliberately generous: the row is only
    #: cosmetic by then, so there is no reason to race an operator who is reading
    #: the fleet view to see what was lost.
    _PRUNE_AFTER_WINDOWS = 10

    def _prune_empty_unhealthy(
        self, node_ids: list[str], sandbox_registry
    ) -> list[str]:
        """Drop node rows that hold nothing and have been gone for a long time."""
        now = time.time()
        pruned: list[str] = []
        with self._lock:
            for node_id in node_ids:
                record = self._nodes.get(node_id)
                if record is None or record.status != "unhealthy":
                    continue
                if any(
                    (
                        record.reserved_memory_mb,
                        record.reserved_cpu_percent,
                        record.reserved_disk_mb,
                        record.reserved_processes,
                    )
                ):
                    continue
                # N70: the counter hash is the authoritative copy of exactly
                # those four fields, so a row that reads empty must not be
                # dropped while the counters still hold something.
                if self._res_store is not None:
                    counters = self._res_store.get(node_id)
                    if counters is not None and any(counters.values()):
                        continue
                if now - record.heartbeat_at <= (
                    self._PRUNE_AFTER_WINDOWS * self._heartbeat_timeout
                ):
                    continue
                if sandbox_registry.list_by_node(node_id):
                    continue
                self._nodes.pop(node_id, None)
                if self._view is not None:
                    # The row is gone for good: retire it from the shared view
                    # too instead of leaving it for the TTL to sweep.
                    try:
                        self._view.delete(node_id)
                    except Exception:  # pragma: no cover - defensive
                        pass
                if self._res_store is not None:
                    try:
                        self._res_store.delete(node_id)
                    except Exception:  # pragma: no cover - defensive
                        pass
                pruned.append(node_id)
        return pruned

    def remove(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)
            if self._view is not None:
                try:
                    self._view.delete(node_id)
                except Exception:  # pragma: no cover - defensive
                    logger.warning(
                        "could not retire the node view for %s", node_id, exc_info=True
                    )
            if self._res_store is not None:
                # N70: the counters go with the row, so a later re-registration
                # under the same id starts from the ledger and the records
                # rather than from a counter nobody withdrew.
                try:
                    self._res_store.delete(node_id)
                except Exception:  # pragma: no cover - defensive
                    logger.warning(
                        "could not retire the node reservation counter for %s",
                        node_id,
                        exc_info=True,
                    )

    def add_local_node(
        self,
        *,
        node_id: str = "local",
        total_memory_mb: int,
        total_cpu_percent: int,
        total_disk_mb: int,
        total_processes: int,
        sandbox_cpu_percent_max: int = 0,
        sandbox_memory_mb_max: int = 0,
        sandbox_processes_max: int = 0,
    ) -> NodeRecord:
        """The in-process worker's own node row.

        N83 phase 2: the per-sandbox ceilings ride along from the control
        plane's ``Settings`` (its ``local://`` node has no heartbeat to carry
        them, and no kernel of its own to read), so a reader of this row sees
        the same "what may one sandbox ask for" answer a remote node's row
        carries. The kernel half stays ``None``: this process is not a worker
        container.
        """
        with self._lock:
            record = NodeRecord(
                node_id=node_id,
                address="local://",
                total_memory_mb=total_memory_mb,
                total_cpu_percent=total_cpu_percent,
                total_disk_mb=total_disk_mb,
                total_processes=total_processes,
                sandbox_cpu_percent_max=sandbox_cpu_percent_max,
                sandbox_memory_mb_max=sandbox_memory_mb_max,
                sandbox_processes_max=sandbox_processes_max,
                labels={"node-type": "local"},
            )
            self._nodes[node_id] = record
            return record

    def _placeable_candidates_locked(
        self, exclude_node_id: str | None
    ) -> list[NodeRecord]:
        """Nodes that could be given work at all, before any capacity check.

        Callers must hold ``self._lock``. Both :meth:`select_and_reserve` and
        :meth:`refusal_dimension` start here so an explanation is always about
        the same node set the placement actually considered.
        """
        if self._view is not None:
            # Placement sees the *fleet*, not just the nodes this replica has
            # heard from: the shared view is refreshed on every read, and the
            # admission decision itself stays atomic in the quota store below.
            fresh = [
                NodeRecord.from_storage_dict(payload)
                for payload in self._view.list()
            ]
            self._overlay_reservations(fresh)
            for record in fresh:
                self._nodes[record.node_id] = record
        self._sweep_health_locked()
        now = time.time()
        return [
            n
            for n in self._nodes.values()
            if n.status == "healthy"
            and n.node_id != exclude_node_id
            and not n.draining
            # Fresh enough to be given work. The in-process node never
            # heartbeats itself, so it is exempt like it is in the sweep.
            and (
                n.address == "local://"
                or now - n.heartbeat_at <= PLACEMENT_MAX_HEARTBEAT_AGE_S
            )
        ]

    def refusal(
        self,
        *,
        exclude_node_id: str | None = None,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        processes: int,
    ) -> dict[str, int | str] | None:
        """Why no placeable node could take the sandbox, when there is one reason.

        Only for explaining a refusal that already happened: every placeable
        node fails on at least one dimension, and when they all fail on the
        same one that dimension is the honest answer. A mixed picture (one
        node short of memory, another short of disk) returns ``None`` so the
        caller keeps the neutral wording instead of picking a winner. So does
        an empty fleet -- there is no dimension to blame.

        ``disk_reserved_mb``/``disk_limit_mb`` are summed over exactly those
        placeable nodes, so an explanation of the workspace gate quotes the
        aggregate the nodes enforce rather than one arbitrary node's slice.
        """
        with self._lock:
            nodes = self._placeable_candidates_locked(exclude_node_id)
            blocking = set()
            disk_reserved = 0
            disk_limit = 0
            for node in nodes:
                dim = node.blocking_dimension(
                    memory_mb, cpu_percent, disk_mb, processes
                )
                if dim is not None:
                    blocking.add(dim)
                disk_reserved += node.reserved_disk_mb
                disk_limit += node.total_disk_mb
        if len(blocking) != 1:
            return None
        return {
            "dimension": next(iter(blocking)),
            "disk_reserved_mb": disk_reserved,
            "disk_limit_mb": disk_limit,
        }

    def _pin_miss_reason_locked(
        self,
        node_id: str,
        *,
        exclude_node_id: str | None,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        processes: int,
    ) -> str:
        """Why the pinned node is not among the placeable candidates (N65).

        The order mirrors ``_placeable_candidates_locked``'s filters, so the
        named gate is the first one that actually kept the node out of
        placement. Callers hold ``self._lock`` and have just refreshed the
        fleet into ``self._nodes``, so a missing record really means "no such
        node".
        """
        record = self._nodes.get(node_id)
        if record is None:
            return "there is no such node in the fleet"
        if record.status != "healthy":
            return "it is unhealthy"
        if node_id == exclude_node_id:
            return "it is excluded from this placement"
        if record.draining:
            return "it is draining"
        if (
            record.address != "local://"
            and time.time() - record.heartbeat_at > PLACEMENT_MAX_HEARTBEAT_AGE_S
        ):
            return "it has not heartbeated recently enough for new work"
        dimension = record.blocking_dimension(
            memory_mb, cpu_percent, disk_mb, processes
        )
        if dimension is not None:
            return f"it does not fit the sandbox ({dimension})"
        return "it is not placeable"

    def select_and_reserve(
        self,
        *,
        base_image: str | None,
        volume_node_id: str | None = None,
        exclude_node_id: str | None = None,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        processes: int,
    ) -> NodeRecord | None:
        """Atomically pick a capable healthy node and reserve its quota.

        The capacity check and the reservation happen under the registry lock,
        so concurrent create requests cannot both pass the check and
        over-commit a node.

        The ranked candidates are tried **in order** (N60): a candidate the
        quota store refuses hands the placement to the next one instead of
        failing the whole fleet -- one node's full ledger used to answer
        ``503 No resources available`` for every create while another node sat
        empty. Each skip is named (node + all four dimensions) so the decision
        stays visible, and every candidate being refused is named as such.
        Only the winner is reserved in memory (and published), never a skipped
        candidate.

        A ``volume_node_id`` turns that off: a pin is a **requirement**, not a
        preference (the caller wants the node the non-shared volume lives on,
        and ``migrate`` passes one for the same reason), so a store refusal on
        the pinned node keeps today's ``503`` -- it is named as a *pinned*
        refusal instead of quietly placing the sandbox off its volume.

        N65: a pin that names a node **no candidate can be** used to be dropped
        with no trace -- ``rank_candidates`` simply ranked the rest and the
        sandbox landed somewhere else. That is still the placement decision
        (a nominal pin must not refuse a legal create, so this is deliberately
        not a ``503``), but it is now named: the pinned node, the gate that
        kept it out, and the fallback are one WARNING.
        """
        with self._lock:
            ranked = rank_candidates(
                self._placeable_candidates_locked(exclude_node_id),
                base_image=base_image,
                volume_node_id=volume_node_id,
                memory_mb=memory_mb,
                cpu_percent=cpu_percent,
                disk_mb=disk_mb,
                processes=processes,
            )
            if volume_node_id is not None and ranked and not any(
                n.node_id == volume_node_id for n in ranked
            ):
                logger.warning(
                    "volume pin to node %s could not be honoured (%s); the pin "
                    "is ignored and placement falls back to the ranked "
                    "candidates",
                    volume_node_id,
                    self._pin_miss_reason_locked(
                        volume_node_id,
                        exclude_node_id=exclude_node_id,
                        memory_mb=memory_mb,
                        cpu_percent=cpu_percent,
                        disk_mb=disk_mb,
                        processes=processes,
                    ),
                )
            #: Without a pin, every ranked candidate is a legitimate answer, so
            #: they are tried in order. With one, the only acceptable answer is
            #: the pinned node (or, when the pin cannot fit at all, the same
            #: single best candidate this function always used): the loop has
            #: one iteration and no hand-over.
            trial = ranked if volume_node_id is None else ranked[:1]
            for index, node in enumerate(trial):
                if self._quota_store is not None:
                    ok = self._quota_store.reserve(
                        node.node_id,
                        {
                            "memory": node.total_memory_mb,
                            "cpu": node.total_cpu_percent,
                            "disk": node.total_disk_mb,
                            "processes": node.total_processes,
                        },
                        {
                            "memory": memory_mb,
                            "cpu": cpu_percent,
                            "disk": disk_mb,
                            "processes": processes,
                        },
                    )
                    if not ok:
                        if volume_node_id is None:
                            logger.warning(
                                "quota store refused node %s for memory=%s "
                                "cpu=%s disk=%s processes=%s; trying the next "
                                "candidate (%s left)",
                                node.node_id,
                                memory_mb,
                                cpu_percent,
                                disk_mb,
                                processes,
                                len(ranked) - index - 1,
                            )
                        else:
                            logger.warning(
                                "quota store refused node %s for memory=%s "
                                "cpu=%s disk=%s processes=%s; this placement "
                                "is pinned to volume node %s, so no other "
                                "candidate is tried and it answers 503",
                                node.node_id,
                                memory_mb,
                                cpu_percent,
                                disk_mb,
                                processes,
                                volume_node_id,
                            )
                        continue
                node.reserve(memory_mb, cpu_percent, disk_mb, processes)
                self._bump_reservation_counter(
                    node.node_id,
                    {
                        "memory": memory_mb,
                        "cpu": cpu_percent,
                        "disk": disk_mb,
                        "processes": processes,
                    },
                )
                self._persist_locked(node)
                return node
            # Reached only when the loop above never returned, i.e. when the
            # candidate set really is exhausted: a refusal that still has a
            # candidate behind it ``continue``s and cannot fall through to
            # here. So a placement that hands over successfully logs the skip
            # line(s) alone -- no "every candidate" line -- and this line only
            # ever follows the last skip. ``ranked`` being empty is the other
            # way in, and that is "nothing fits", not "everyone refused", so it
            # stays silent; a *pinned* refusal never gets here either, because
            # the other candidates were deliberately not tried and the refusal
            # above already said so.
            if volume_node_id is None and ranked:
                logger.warning(
                    "quota store refused every candidate for memory=%s cpu=%s "
                    "disk=%s processes=%s; this placement answers 503",
                    memory_mb,
                    cpu_percent,
                    disk_mb,
                    processes,
                )
            return None

    def reserve_node(
        self,
        node_id: str,
        *,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        processes: int,
    ) -> NodeRecord | None:
        """Reserve quota on one explicit healthy node, or ``None`` if it
        cannot fit (or is missing/unhealthy)."""
        with self._lock:
            self._sweep_health_locked()
            record = self._nodes.get(node_id)
            if record is None or record.status != "healthy":
                return None
            if not record.can_fit(memory_mb, cpu_percent, disk_mb, processes):
                return None
            if self._quota_store is not None:
                ok = self._quota_store.reserve(
                    record.node_id,
                    {
                        "memory": record.total_memory_mb,
                        "cpu": record.total_cpu_percent,
                        "disk": record.total_disk_mb,
                        "processes": record.total_processes,
                    },
                    {
                        "memory": memory_mb,
                        "cpu": cpu_percent,
                        "disk": disk_mb,
                        "processes": processes,
                    },
                )
                if not ok:
                    return None
            record.reserve(memory_mb, cpu_percent, disk_mb, processes)
            self._bump_reservation_counter(
                record.node_id,
                {
                    "memory": memory_mb,
                    "cpu": cpu_percent,
                    "disk": disk_mb,
                    "processes": processes,
                },
            )
            self._persist_locked(record)
            return record

    def release_quota(
        self,
        node_id: str,
        *,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        processes: int,
    ) -> None:
        with self._lock:
            record = self._nodes.get(node_id)
            if record is None:
                # Named, not silent: this is now N59's *only* release point, so
                # a skip lands in the over-counting direction (a slot that
                # never comes back). The shared row is deliberately left
                # alone: without the node record this replica cannot know
                # whether the ledger entries under it are this sandbox's or a
                # live one's, and crediting a node we cannot see is the
                # under-counting direction that over-sells.
                logger.warning(
                    "release_quota for node %s found no node record: "
                    "memory=%s cpu=%s disk=%s processes=%s stay reserved "
                    "(the shared ledger row is left alone)",
                    node_id,
                    memory_mb,
                    cpu_percent,
                    disk_mb,
                    processes,
                )
                return
            if self._quota_store is not None:
                self._quota_store.release(
                    node_id,
                    {
                        "memory": memory_mb,
                        "cpu": cpu_percent,
                        "disk": disk_mb,
                        "processes": processes,
                    },
                )
            record.release(memory_mb, cpu_percent, disk_mb, processes)
            # N70: give the reservation back in the atomic counter too. A value
            # that lands below zero is named by the store, not clamped here.
            self._bump_reservation_counter(
                node_id,
                {
                    "memory": -memory_mb,
                    "cpu": -cpu_percent,
                    "disk": -disk_mb,
                    "processes": -processes,
                },
            )
            self._persist_locked(record)
