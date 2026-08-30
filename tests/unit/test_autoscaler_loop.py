"""Autoscaler reconcile loop: scale up/down, cooldowns and drain lifecycle."""

from __future__ import annotations

from autoscaler.control import ControlPlaneClient
from autoscaler.loop import AutoscalerLoop
from autoscaler.policy import PolicyConfig
from tests.unit.test_autoscaler_policy import _node, _payload


class FakeControl:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.drained: list[str] = []
        self._i = 0

    def metrics(self):
        payload = self.payloads[min(self._i, len(self.payloads) - 1)]
        self._i += 1
        return payload

    def drain(self, node_id):
        self.drained.append(node_id)
        return {"nodeID": node_id, "activeSandboxes": 0}


class FakeBackend:
    def __init__(self, current: int = 2):
        self.current_n = current
        self.scaled: list[int] = []
        self.removed: list[str] = []

    def current(self):
        return self.current_n

    def scale_to(self, replicas):
        self.scaled.append(replicas)
        self.current_n = replicas

    def remove_node(self, node_id):
        self.removed.append(node_id)
        self.current_n -= 1


def _loop(control, backend, **cfg):
    return AutoscalerLoop(
        control=control,
        backend=backend,
        policy=PolicyConfig(**cfg),
    )


async def test_scale_up_on_high_utilization():
    backend = FakeBackend(current=2)
    loop = _loop(
        FakeControl([_payload([_node("a"), _node("b")], fleet_util=0.8, active=8)]),
        backend,
        warmup_buffer=1,
    )
    await loop.tick()
    assert backend.scaled == [3]


async def test_scale_up_cooldown_blocks_second_tick():
    backend = FakeBackend(current=2)
    control = FakeControl(
        [
            _payload([_node("a"), _node("b")], fleet_util=0.8, active=8),
            _payload([_node("a"), _node("b")], fleet_util=0.8, active=8),
        ]
    )
    loop = _loop(control, backend, warmup_buffer=1, scale_up_cooldown_s=60)
    await loop.tick()
    await loop.tick()
    assert backend.scaled == [3]


async def test_scale_down_drains_and_retires_idle_node():
    backend = FakeBackend(current=3)
    control = FakeControl(
        [_payload([_node("a"), _node("b"), _node("c")], fleet_util=0.1)]
    )
    loop = _loop(
        control,
        backend,
        min_replicas=1,
        scale_down_cooldown_s=600,
        scale_down_util=0.4,
    )
    await loop.tick()
    assert control.drained == ["a"]
    assert backend.removed == ["a"]
    assert backend.current_n == 2


async def test_scale_down_waits_while_node_has_active_sandboxes():
    class DelayedControl(FakeControl):
        def drain(self, node_id):
            self.drained.append(node_id)
            return {"nodeID": node_id, "activeSandboxes": 1}

    backend = FakeBackend(current=3)
    control = DelayedControl(
        [
            _payload([_node("a"), _node("b"), _node("c")], fleet_util=0.1),
            _payload([_node("a"), _node("b"), _node("c")], fleet_util=0.1),
        ]
    )
    loop = _loop(control, backend, min_replicas=1, scale_down_util=0.4)
    await loop.tick()
    assert control.drained == ["a"]
    assert backend.removed == []
    # Next tick: node "a" now reports zero active sandboxes -> retire.
    await loop.tick()
    assert backend.removed == ["a"]


async def test_no_scale_down_when_fleet_guard_blocks():
    backend = FakeBackend(current=3)
    control = FakeControl(
        [_payload([_node("a"), _node("b"), _node("c")], fleet_util=0.5)]
    )
    loop = _loop(control, backend, min_replicas=1, scale_down_util=0.4)
    await loop.tick()
    assert control.drained == []
    assert backend.removed == []
