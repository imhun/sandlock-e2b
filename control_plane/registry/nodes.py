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
    reserved_memory_mb: int = 0
    reserved_cpu_percent: int = 0
    reserved_disk_mb: int = 0
    reserved_processes: int = 0
    draining: bool = False
    images: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    status: str = "healthy"
    heartbeat_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

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
        self._quota_store = None
        if redis_client is not None:
            from control_plane.registry.redis_backend import RedisQuotaStore

            self._quota_store = RedisQuotaStore(redis_client, f"{namespace}:node")

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
    ) -> NodeRecord:
        with self._lock:
            record = self._nodes.get(node_id) if node_id else None
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
                record.draining = False
            record.heartbeat_at = time.time()
            record.status = "healthy"
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
            record = self._nodes.get(node_id)
            if record is None:
                return None
            record.heartbeat_at = time.time()
            record.status = "healthy"
            return record

    def set_draining(self, node_id: str, draining: bool) -> NodeRecord | None:
        """Mark/unmark a node as draining; returns the record or ``None``."""
        with self._lock:
            record = self._nodes.get(node_id)
            if record is None:
                return None
            record.draining = draining
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
            record = self._nodes.get(node_id)
            if record is None:
                return None
            record.reserved_memory_mb = max(0, memory_mb)
            record.reserved_cpu_percent = max(0, cpu_percent)
            record.reserved_disk_mb = max(0, disk_mb)
            record.reserved_processes = max(0, processes)
            return record

    def get(self, node_id: str) -> NodeRecord | None:
        self._sweep_health()
        with self._lock:
            return self._nodes.get(node_id)

    def list(self, *, healthy_only: bool = False) -> list[NodeRecord]:
        self._sweep_health()
        with self._lock:
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
                pruned.append(node_id)
        return pruned

    def remove(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)

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
