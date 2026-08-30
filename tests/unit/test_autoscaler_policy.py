"""Autoscaler policy: scale triggers, desired count and safe scale-down."""

from __future__ import annotations

from autoscaler.policy import (
    PolicyConfig,
    desired_for_demand,
    parse_snapshot,
    per_node_capacity,
    scale_down_candidates,
    scale_up_triggered,
)


def _node(
    node_id: str,
    *,
    total: int = 2048,
    active: int = 0,
    draining: bool = False,
    status: str = "healthy",
    util: float = 0.0,
) -> dict:
    memory_total = total
    cpu_total = 400
    disk_total = 4096
    processes_total = 256
    return {
        "nodeID": node_id,
        "status": status,
        "draining": draining,
        "activeSandboxes": active,
        "utilization": {
            "memory": {
                "reserved": int(memory_total * util),
                "total": memory_total,
                "utilization": util,
            },
            "cpu": {
                "reserved": int(cpu_total * util),
                "total": cpu_total,
                "utilization": util,
            },
            "disk": {
                "reserved": int(disk_total * util),
                "total": disk_total,
                "utilization": util,
            },
            "processes": {
                "reserved": int(processes_total * util),
                "total": processes_total,
                "utilization": util,
            },
        },
    }


def _payload(
    nodes,
    *,
    fleet_util: float = 0.0,
    active: int = 0,
    recent503: int = 0,
) -> dict:
    return {
        "nodes": nodes,
        "fleet": {
            "memory": {
                "reserved": 0,
                "total": sum(n["utilization"]["memory"]["total"] for n in nodes),
                "utilization": fleet_util,
            }
        },
        "standardSandboxDims": {
            "memory": 512,
            "cpu": 100,
            "disk": 1024,
            "processes": 64,
        },
        "activeSandboxes": active,
        "recent503Count": recent503,
        "remainingSandboxCapacity": 10,
    }


def _cfg(**overrides):
    defaults = dict(
        min_replicas=1,
        max_replicas=16,
        util_threshold=0.70,
        scale_up_cooldown_s=60,
        scale_down_cooldown_s=600,
        scale_down_util=0.40,
        node_scale_down_util=0.0,
        warmup_buffer=1,
    )
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def test_scale_up_triggered_on_utilization_and_503():
    cfg = _cfg(util_threshold=0.70)
    snapshot = parse_snapshot(_payload([_node("a")], fleet_util=0.8))
    assert scale_up_triggered(snapshot, cfg) is True
    snapshot = parse_snapshot(_payload([_node("a")], fleet_util=0.5))
    assert scale_up_triggered(snapshot, cfg) is False
    snapshot = parse_snapshot(_payload([_node("a")], fleet_util=0.2, recent503=3))
    assert scale_up_triggered(snapshot, cfg) is True


def test_desired_for_demand_uses_capacity_and_buffer():
    cfg = _cfg(warmup_buffer=1)
    snapshot = parse_snapshot(
        _payload([_node("a", total=2048), _node("b", total=2048)], active=8)
    )
    # 512MB standard sandbox, 2048MB/node -> 4/node; 8 active -> 2 + 1 buffer.
    assert per_node_capacity(snapshot) == 4
    assert desired_for_demand(snapshot, cfg) == 3


def test_desired_clamped_to_max():
    cfg = _cfg(warmup_buffer=1, max_replicas=3)
    snapshot = parse_snapshot(_payload([_node("a", total=2048)], active=64))
    assert desired_for_demand(snapshot, cfg) == 3


def test_desired_at_least_min_when_idle():
    cfg = _cfg(min_replicas=2)
    snapshot = parse_snapshot(_payload([_node("a")], active=0))
    assert desired_for_demand(snapshot, cfg) == 2


def test_scale_down_blocked_by_fleet_guard():
    cfg = _cfg(scale_down_util=0.40)
    snapshot = parse_snapshot(
        _payload([_node("a"), _node("b")], fleet_util=0.5, active=0)
    )
    assert scale_down_candidates(snapshot, cfg) == []


def test_scale_down_candidates_only_idle_healthy():
    cfg = _cfg(scale_down_util=0.40, node_scale_down_util=0.0)
    snapshot = parse_snapshot(
        _payload(
            [
                _node("idle"),
                _node("busy", active=2, util=0.5),
                _node("draining", draining=True),
                _node("unhealthy", status="unhealthy"),
            ],
            fleet_util=0.1,
        )
    )
    candidates = scale_down_candidates(snapshot, cfg)
    assert [n.node_id for n in candidates] == ["idle"]


def test_node_utilization_threshold_filters():
    cfg = _cfg(scale_down_util=0.40, node_scale_down_util=0.2)
    snapshot = parse_snapshot(
        _payload(
            [_node("cold", util=0.0), _node("warm", util=0.5)],
            fleet_util=0.1,
        )
    )
    assert [n.node_id for n in scale_down_candidates(snapshot, cfg)] == ["cold"]
