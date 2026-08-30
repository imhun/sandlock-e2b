"""Node registry: registration, heartbeat, health and reservations."""

from __future__ import annotations

import time

from control_plane.registry.nodes import NodeRegistry


def _node(total_memory_mb=1024, total_cpu=200, total_disk=2048, total_procs=128):
    registry = NodeRegistry(heartbeat_timeout=1.0)
    return registry, registry.register(
        node_id="node_a",
        address="http://127.0.0.1:49983",
        total_memory_mb=total_memory_mb,
        total_cpu_percent=total_cpu,
        total_disk_mb=total_disk,
        total_processes=total_procs,
        images=["python:3.11-slim"],
        labels={"node-type": "container"},
    )


def test_register_and_heartbeat():
    registry, record = _node()
    assert registry.get("node_a") is record
    assert record.status == "healthy"
    assert registry.heartbeat("node_a") is record
    assert registry.heartbeat("missing") is None


def test_heartbeat_timeout_marks_unhealthy():
    registry, record = _node()
    record.heartbeat_at = time.time() - 5
    assert registry.get("node_a").status == "unhealthy"
    assert registry.list(healthy_only=True) == []
    registry.heartbeat("node_a")
    assert registry.get("node_a").status == "healthy"


def test_reservation_and_release():
    registry, record = _node(total_memory_mb=1024)
    assert record.can_fit(512, 100, 1024, 64)
    record.reserve(512, 100, 1024, 64)
    assert not record.can_fit(600, 100, 1024, 64)
    assert record.can_fit(256, 50, 512, 32)
    record.release(512, 100, 1024, 64)
    assert record.can_fit(512, 100, 1024, 64)


def test_local_node_never_unhealthy():
    registry = NodeRegistry(heartbeat_timeout=0.1)
    registry.add_local_node(
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    time.sleep(0.15)
    assert registry.get("local").status == "healthy"


def test_select_and_reserve_never_overcommits():
    """Atomic select+reserve never over-commits a node: once it is full,
    the next request returns None instead of reserving beyond capacity."""
    registry = NodeRegistry()
    registry.register(
        node_id="node_x",
        address="http://x:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    first = registry.select_and_reserve(
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert first is not None
    assert first.reserved_memory_mb == 512
    second = registry.select_and_reserve(
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert second is not None  # 512 + 512 == 1024 exactly fits
    third = registry.select_and_reserve(
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert third is None  # 1024 + 512 > 1024: rejected, no over-commit
    assert first.reserved_memory_mb == 1024


def test_select_and_reserve_respects_health():
    registry = NodeRegistry(heartbeat_timeout=1.0)
    registry.register(
        node_id="dead",
        address="http://dead:49983",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
    )
    registry.get("dead").heartbeat_at = time.time() - 10
    node = registry.select_and_reserve(
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node is None  # unhealthy nodes are filtered inside the lock


def test_release_quota_under_lock():
    registry = NodeRegistry()
    registry.register(
        node_id="node_y",
        address="http://y:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    node = registry.select_and_reserve(
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    registry.release_quota(
        "node_y",
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node.reserved_memory_mb == 0
    assert node.reserved_processes == 0


def test_set_draining_excludes_node():
    registry, record = _node()
    assert registry.set_draining("missing", True) is None
    assert registry.set_draining("node_a", True) is record
    assert record.draining is True
    assert registry.get("node_a").draining is True
    assert "draining" in record.to_dict()
    # Healthy but draining nodes are not schedulable.
    picked = registry.select_and_reserve(
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert picked is None


def test_register_clears_draining():
    registry, record = _node()
    registry.set_draining("node_a", True)
    registry.register(
        node_id="node_a",
        address="http://127.0.0.1:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    assert record.draining is False


def test_set_reserved_restores_accounting():
    registry, record = _node(total_memory_mb=1024)
    restored = registry.set_reserved(
        "node_a",
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert restored is record
    assert record.reserved_memory_mb == 512
    assert record.reserved_cpu_percent == 100
    assert not record.can_fit(600, 100, 1024, 64)
    assert record.can_fit(256, 50, 512, 32)
    assert registry.set_reserved(
        "missing",
        memory_mb=0,
        cpu_percent=0,
        disk_mb=0,
        processes=0,
    ) is None
