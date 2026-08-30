"""Scheduler: filtering, scoring and volume affinity."""

from __future__ import annotations

from control_plane.registry.nodes import NodeRegistry
from control_plane.scheduler import select_node


def _node_registry():
    registry = NodeRegistry()
    registry.register(
        node_id="a",
        address="http://a:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
        images=["python:3.11-slim"],
    )
    registry.register(
        node_id="b",
        address="http://b:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=2048,
        total_processes=128,
        images=[],
    )
    return registry


def test_select_prefers_image_affinity():
    registry = _node_registry()
    node = select_node(
        registry.list(healthy_only=True),
        base_image="python:3.11-slim",
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node.node_id == "a"


def test_select_balances_without_image():
    registry = _node_registry()
    node = select_node(
        registry.list(healthy_only=True),
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node.node_id in ("a", "b")


def test_select_volume_affinity():
    registry = _node_registry()
    node = select_node(
        registry.list(healthy_only=True),
        base_image=None,
        volume_node_id="b",
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node.node_id == "b"


def test_select_none_when_full():
    registry = _node_registry()
    nodes = registry.list(healthy_only=True)
    for n in nodes:
        n.reserve(1024, 200, 2048, 128)
    node = select_node(
        nodes,
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node is None


def test_select_filters_draining():
    registry = _node_registry()
    registry.set_draining("a", True)
    node = select_node(
        registry.list(healthy_only=True),
        base_image="python:3.11-slim",
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node is not None
    assert node.node_id == "b"
