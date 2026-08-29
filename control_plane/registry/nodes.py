"""Compute node registry: capabilities, reservations and health."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from control_plane.scheduler import pick_best
from gateway_common.ids import sandbox_id


@dataclass
class NodeRecord:
    node_id: str
    address: str  # "local://" for the in-process worker, else http(s) URL
    total_memory_mb: int = 0
    total_cpu_percent: int = 0
    total_disk_mb: int = 0
    total_processes: int = 0
    reserved_memory_mb: int = 0
    reserved_cpu_percent: int = 0
    reserved_disk_mb: int = 0
    reserved_processes: int = 0
    images: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    status: str = "healthy"
    heartbeat_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    def can_fit(self, memory_mb: int, cpu: int, disk_mb: int, processes: int) -> bool:
        if self.total_memory_mb > 0 and self.reserved_memory_mb + memory_mb > self.total_memory_mb:
            return False
        if self.total_cpu_percent > 0 and self.reserved_cpu_percent + cpu > self.total_cpu_percent:
            return False
        if self.total_disk_mb > 0 and self.reserved_disk_mb + disk_mb > self.total_disk_mb:
            return False
        if self.total_processes > 0 and self.reserved_processes + processes > self.total_processes:
            return False
        return True

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
            "reservedDiskMB": self.reserved_disk_mb,
            "totalProcesses": self.total_processes,
            "reservedProcesses": self.reserved_processes,
        }


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
                record = NodeRecord(
                    node_id=node_id,
                    address=address,
                    total_memory_mb=total_memory_mb,
                    total_cpu_percent=total_cpu_percent,
                    total_disk_mb=total_disk_mb,
                    total_processes=total_processes,
                    images=list(images or []),
                    labels=dict(labels or {}),
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
            record.heartbeat_at = time.time()
            record.status = "healthy"
            return record

    def heartbeat(self, node_id: str) -> NodeRecord | None:
        with self._lock:
            record = self._nodes.get(node_id)
            if record is None:
                return None
            record.heartbeat_at = time.time()
            record.status = "healthy"
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
            self._sweep_health_locked()
            candidates = [
                n
                for n in self._nodes.values()
                if n.status == "healthy"
                and n.node_id != exclude_node_id
                and n.can_fit(memory_mb, cpu_percent, disk_mb, processes)
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
