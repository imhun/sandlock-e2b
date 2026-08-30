"""Pure scale decisions: when to scale up, how many replicas, and which
idle nodes are safe to drain on scale-down."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PolicyConfig:
    min_replicas: int = 1
    max_replicas: int = 16
    util_threshold: float = 0.70
    scale_up_cooldown_s: float = 60.0
    scale_down_cooldown_s: float = 600.0
    scale_down_util: float = 0.40
    node_scale_down_util: float = 0.0
    warmup_buffer: int = 1


@dataclass
class NodeMetrics:
    node_id: str
    status: str
    draining: bool
    active_sandboxes: int
    utilization: dict[str, float] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)

    def peak_utilization(self) -> float:
        values = [v for v in self.utilization.values() if v >= 0]
        return max(values) if values else 0.0


@dataclass
class FleetSnapshot:
    nodes: list[NodeMetrics]
    fleet_utilization: dict[str, float]
    active_sandboxes: int
    recent503_count: int
    standard_dims: dict[str, int] = field(default_factory=dict)
    remaining_sandbox_capacity: int | None = None

    def peak_fleet_utilization(self) -> float:
        values = [v for v in self.fleet_utilization.values() if v >= 0]
        return max(values) if values else 0.0


def fleet_peak_utilization(metrics: dict[str, Any]) -> float:
    values = [
        float(v.get("utilization", 0.0))
        for v in metrics.get("fleet", {}).values()
    ]
    return max(values) if values else 0.0


def per_node_capacity(snapshot: FleetSnapshot) -> int | None:
    """Standard sandboxes one node can host (min over bounded dims)."""
    if not snapshot.standard_dims or not snapshot.nodes:
        return None
    dims = snapshot.standard_dims
    best: int | None = None
    for node in snapshot.nodes:
        totals = node.totals
        capacities: list[int] = []
        for key, demand in dims.items():
            total = totals.get(key, 0)
            capacities.append(total // demand if demand > 0 and total > 0 else 0)
        node_cap = min(capacities) if capacities else 0
        if node_cap > 0:
            best = node_cap if best is None else min(best, node_cap)
    return best


def parse_snapshot(payload: dict[str, Any]) -> FleetSnapshot:
    """Build a :class:`FleetSnapshot` from the fleet metrics payload."""
    nodes: list[NodeMetrics] = []
    for raw in payload.get("nodes", []):
        utilization: dict[str, float] = {}
        totals: dict[str, int] = {}
        for key, value in (raw.get("utilization") or {}).items():
            if isinstance(value, dict):
                utilization[key] = float(value.get("utilization", 0.0))
                totals[key] = int(value.get("total", 0))
        nodes.append(
            NodeMetrics(
                node_id=raw.get("nodeID", ""),
                status=raw.get("status", "unhealthy"),
                draining=bool(raw.get("draining", False)),
                active_sandboxes=int(raw.get("activeSandboxes", 0)),
                utilization=utilization,
                totals=totals,
            )
        )
    fleet_utilization = {
        key: float(value.get("utilization", 0.0))
        for key, value in (payload.get("fleet") or {}).items()
        if isinstance(value, dict)
    }
    return FleetSnapshot(
        nodes=nodes,
        fleet_utilization=fleet_utilization,
        active_sandboxes=int(payload.get("activeSandboxes", 0)),
        recent503_count=int(payload.get("recent503Count", 0)),
        standard_dims={
            str(k): int(v)
            for k, v in (payload.get("standardSandboxDims") or {}).items()
        },
        remaining_sandbox_capacity=payload.get("remainingSandboxCapacity"),
    )


def scale_up_triggered(snapshot: FleetSnapshot, cfg: PolicyConfig) -> bool:
    if snapshot.recent503_count > 0:
        return True
    return snapshot.peak_fleet_utilization() >= cfg.util_threshold


def desired_for_demand(snapshot: FleetSnapshot, cfg: PolicyConfig) -> int:
    """Replicas needed for the current sandbox load plus warm-up buffer."""
    if snapshot.active_sandboxes <= 0:
        return cfg.min_replicas
    per_node = per_node_capacity(snapshot)
    if per_node is None or per_node <= 0:
        return cfg.min_replicas
    needed = math.ceil(snapshot.active_sandboxes / per_node)
    desired = needed + cfg.warmup_buffer
    return max(cfg.min_replicas, min(cfg.max_replicas, desired))


def scale_down_candidates(
    snapshot: FleetSnapshot, cfg: PolicyConfig
) -> list[NodeMetrics]:
    """Idle, non-draining nodes that are safe to retire.

    Empty when fleet utilization is still at/above the scale-down guard, so
    the pool does not shrink while overall demand is high (no thrash).
    """
    if snapshot.peak_fleet_utilization() >= cfg.scale_down_util:
        return []
    candidates = [
        n
        for n in snapshot.nodes
        if n.status == "healthy"
        and not n.draining
        and n.active_sandboxes == 0
        and n.peak_utilization() <= cfg.node_scale_down_util
    ]
    return candidates


def clamp(desired: int, cfg: PolicyConfig) -> int:
    return max(cfg.min_replicas, min(cfg.max_replicas, desired))
