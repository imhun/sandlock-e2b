"""Compute node registry: capabilities, reservations and health."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from control_plane.scheduler import pick_best
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
        if redis_client is not None:
            from control_plane.registry.redis_backend import (
                RedisNodeStore,
                RedisQuotaStore,
            )

            self._quota_store = RedisQuotaStore(redis_client, f"{namespace}:node")
            # F11 step 1: the view of the fleet lives in the shared store, so
            # every replica reads the same nodes, the same addresses and the
            # same reservations. The process dict stays as a cache for the
            # single-process deployment (no Redis).
            self._view = RedisNodeStore(redis_client, namespace)
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
                self._nodes[node_id] = record
                return record
        return self._nodes.get(node_id)

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
                    reserved_memory_mb=reserved.get("memory", 0),
                    reserved_cpu_percent=reserved.get("cpu", 0),
                    reserved_disk_mb=reserved.get("disk", 0),
                    reserved_processes=reserved.get("processes", 0),
                )
                self._nodes[node_id] = record
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
            self._persist_locked(record)
            return record

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
                for record in records:
                    self._nodes[record.node_id] = record
                    self._status_of(record)
            else:
                records = list(self._nodes.values())
        if healthy_only:
            records = [r for r in records if r.status == "healthy"]
        return records

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

    def add_local_node(
        self,
        *,
        node_id: str = "local",
        total_memory_mb: int,
        total_cpu_percent: int,
        total_disk_mb: int,
        total_processes: int,
    ) -> NodeRecord:
        with self._lock:
            record = NodeRecord(
                node_id=node_id,
                address="local://",
                total_memory_mb=total_memory_mb,
                total_cpu_percent=total_cpu_percent,
                total_disk_mb=total_disk_mb,
                total_processes=total_processes,
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
            for payload in self._view.list():
                record = NodeRecord.from_storage_dict(payload)
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
        """
        with self._lock:
            candidates = [
                n
                for n in self._placeable_candidates_locked(exclude_node_id)
                if n.can_fit(memory_mb, cpu_percent, disk_mb, processes)
            ]
            node = pick_best(
                candidates,
                base_image=base_image,
                volume_node_id=volume_node_id,
                memory_mb=memory_mb,
                cpu_percent=cpu_percent,
                disk_mb=disk_mb,
                processes=processes,
            )
            if node is not None:
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
                        return None
                node.reserve(memory_mb, cpu_percent, disk_mb, processes)
                self._persist_locked(node)
            return node

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
            if record is not None:
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
                self._persist_locked(record)
