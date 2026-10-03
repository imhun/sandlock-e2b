"""The platform-initiated pause: same chain as ``POST /sandboxes/{id}/pause``.

The idle->pause sweep runs from the control-plane lifespan, where there is no
``Request``. ``pause_record_for_platform`` is the request-free entry point into
the endpoint's own chain -- ``registry.pause`` (global/tenant), park the node
reservation, push the freeze to the worker -- so the two paths cannot drift
into two different meanings of "paused".

The tests here pin the three outcomes the sweep depends on: a delivered pause
returns every reservation and leaves the shared runtime alone; a refused push
rolls the record all the way back to ``running`` with its admission re-booked;
a node the registry no longer knows is best-effort (the in-process runtime
registry carries the state instead).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from control_plane.api import sandboxes
from control_plane.api.errors import OfficialError
from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=0,
        default_disk_mb=0,
        default_max_processes=0,
        max_total_memory_mb=1024,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry) -> object:
    return registry.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
        sandbox_id="sbx_a",
    )


class _FakeNodes:
    """The two node-registry calls the pause/resume chain makes."""

    def __init__(self, node_id: str | None = "worker-1") -> None:
        self.node_id = node_id
        self.released: list[tuple[str, dict]] = []
        self.reserved: list[tuple[str, dict]] = []

    def get(self, node_id: str):
        if self.node_id is None or node_id != self.node_id:
            return None
        return SimpleNamespace(node_id=self.node_id)

    def release_quota(self, node_id: str, **dims) -> None:
        self.released.append((node_id, dims))

    def reserve_node(self, node_id: str, **dims):
        self.reserved.append((node_id, dims))
        return SimpleNamespace(node_id=node_id)


class _FakeRuntimeRegistry:
    def __init__(self) -> None:
        self.states: list[tuple[str, str]] = []

    def set_state(self, sandbox_id: str, state: str) -> None:
        self.states.append((sandbox_id, state))


def _state(nodes, runtime_registry, settings, registry) -> SimpleNamespace:
    return SimpleNamespace(
        nodes=nodes,
        runtime_registry=runtime_registry,
        settings=settings,
        registry=registry,
    )


def test_pausing_returns_the_node_reservation(monkeypatch):
    settings = _settings()
    registry = SandboxRegistry(settings)
    record = _create(registry)
    record.node_id = "worker-1"
    nodes = _FakeNodes()
    runtime = _FakeRuntimeRegistry()

    async def delivered(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(sandboxes, "_push_pause_state", delivered)

    result = asyncio.run(
        sandboxes.pause_record_for_platform(
            _state(nodes, runtime, settings, registry), record, reason="idle 301s"
        )
    )

    assert result == "paused"
    assert record.state == "paused"
    assert record.pause_reason == "idle 301s"
    assert record.paused_at is not None
    assert nodes.released == [
        ("worker-1", {"memory_mb": 512, "cpu_percent": 100, "disk_mb": 0, "processes": 0})
    ]
    assert registry._reserved_memory == 0
    assert runtime.states == []


def test_a_refused_worker_push_rolls_the_record_back(monkeypatch):
    settings = _settings()
    registry = SandboxRegistry(settings)
    record = _create(registry)
    record.node_id = "worker-1"
    nodes = _FakeNodes()
    runtime = _FakeRuntimeRegistry()

    async def refused(*_args, **_kwargs) -> bool:
        raise OfficialError(502, "Node worker-1 failed to pause sandbox sbx_a")

    monkeypatch.setattr(sandboxes, "_push_pause_state", refused)

    with pytest.raises(OfficialError) as excinfo:
        asyncio.run(
            sandboxes.pause_record_for_platform(
                _state(nodes, runtime, settings, registry), record, reason="idle 301s"
            )
        )

    assert excinfo.value.code == 502
    assert record.state == "running"
    assert record.pause_reason is None
    assert record.paused_at is None
    assert registry._reserved_memory == 512
    assert nodes.released == [
        ("worker-1", {"memory_mb": 512, "cpu_percent": 100, "disk_mb": 0, "processes": 0})
    ]
    assert nodes.reserved == [
        ("worker-1", {"memory_mb": 512, "cpu_percent": 100, "disk_mb": 0, "processes": 0})
    ]
    assert runtime.states == [("sbx_a", "running")]


def test_a_missing_node_falls_back_to_the_shared_runtime():
    settings = _settings()
    registry = SandboxRegistry(settings)
    record = _create(registry)
    record.node_id = "worker-gone"
    nodes = _FakeNodes(node_id=None)
    runtime = _FakeRuntimeRegistry()

    result = asyncio.run(
        sandboxes.pause_record_for_platform(
            _state(nodes, runtime, settings, registry), record, reason="idle 301s"
        )
    )

    assert result == "paused"
    assert record.state == "paused"
    assert registry._reserved_memory == 0
    assert nodes.released == []
    assert runtime.states == [("sbx_a", "paused")]
