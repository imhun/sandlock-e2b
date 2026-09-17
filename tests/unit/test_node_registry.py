"""Node registry: registration, heartbeat, health and reservations."""

from __future__ import annotations

import time

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry
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


def _reserve(registry):
    return registry.select_and_reserve(
        base_image=None,
        memory_mb=128,
        cpu_percent=10,
        disk_mb=128,
        processes=8,
    )


def test_a_node_that_missed_recent_heartbeats_is_not_given_new_work():
    """Placement freshness is a separate, shorter window than the orphan one.

    The orphan window (``heartbeat_timeout``) has to stay generous -- wrongly
    orphaning a live sandbox takes its slot away (the N18 lesson) -- but a node
    that has gone quiet must stop receiving *new* sandboxes long before that, or
    every create placed on it answers `502 Node ... unavailable` until the wide
    window elapses. Measured on k0s 2026-09-17 with a single 300s threshold:
    restarting a worker produced about a minute of exactly that.
    """
    registry = NodeRegistry(heartbeat_timeout=300.0)  # the wide orphan window
    record = registry.register(
        node_id="node_slow",
        address="http://127.0.0.1:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    assert _reserve(registry) is not None

    # Quiet for a minute: still "healthy" by the orphan definition (so its live
    # sandboxes are safe), but no longer eligible for new work.
    record.heartbeat_at = time.time() - 60
    assert registry.get("node_slow").status == "healthy"
    assert _reserve(registry) is None

    # One heartbeat and it is eligible again.
    registry.heartbeat("node_slow")
    assert _reserve(registry) is not None


def test_the_in_process_node_is_exempt_from_the_placement_window():
    """`local://` never heartbeats itself, so it must not age out of placement."""
    registry = NodeRegistry(heartbeat_timeout=300.0)
    record = registry.register(
        node_id="local",
        address="local://",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    record.heartbeat_at = time.time() - 3600
    assert _reserve(registry) is not None


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


def test_reap_unhealthy_marks_sandboxes_orphaned():
    """E6.1: the periodic health sweep marks sandbox records on unhealthy
    remote nodes as orphaned and never touches healthy nodes or the local
    in-process node."""
    registry = NodeRegistry(heartbeat_timeout=1.0)
    registry.add_local_node(
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    dead = registry.register(
        node_id="node_dead",
        address="http://dead:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )
    alive = registry.register(
        node_id="node_alive",
        address="http://alive:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
    )

    sandboxes = SandboxRegistry(Settings(api_keys=("local-key",)))

    def _on_node(record, node_id):
        record.node_id = node_id
        sandboxes.save(record)

    rec_dead = sandboxes.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    _on_node(rec_dead, dead.node_id)
    rec_alive = sandboxes.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    _on_node(rec_alive, alive.node_id)

    dead.heartbeat_at = time.time() - 10
    assert registry.reap_unhealthy(sandboxes) == ["node_dead"]
    assert sandboxes.get(rec_dead.sandbox_id).state == "orphaned"
    assert sandboxes.get(rec_alive.sandbox_id).state == "running"
    # Local in-process node is never swept; healthy node untouched either.
    assert registry.get("local").status == "healthy"
    assert registry.get("node_alive").status == "healthy"

    # A heartbeat revives the node but the mark persists until the worker
    # reconciles its local runtimes (recovery path).
    registry.heartbeat("node_dead")
    assert registry.get("node_dead").status == "healthy"
    assert sandboxes.get(rec_dead.sandbox_id).state == "orphaned"
