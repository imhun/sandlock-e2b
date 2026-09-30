"""Autoscaler reconcile loop: scale up/down, cooldowns and drain lifecycle."""

from __future__ import annotations

from autoscaler.loop import AutoscalerLoop
from autoscaler.policy import PolicyConfig
from autoscaler.state import InMemoryLoopState
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
    def __init__(self, current: int = 2, retire_pick: str = "first"):
        self.current_n = current
        self.scaled: list[int] = []
        self.removed: list[str] = []
        #: ``"first"`` = the loop's own choice stands (a Deployment), ``"refuse"``
        #: = this workload cannot take any of them (a StatefulSet whose top
        #: ordinal is busy), or a node id = the victim the backend would take.
        self.retire_pick = retire_pick

    def current(self):
        return self.current_n

    def scale_to(self, replicas):
        self.scaled.append(replicas)
        self.current_n = replicas

    def remove_node(self, node_id):
        self.removed.append(node_id)
        self.current_n -= 1

    def retire_victim(self, candidates):
        if not candidates:
            return None
        if self.retire_pick == "first":
            return candidates[0]
        if self.retire_pick == "refuse":
            return None
        return self.retire_pick if self.retire_pick in candidates else None


def _loop(control, backend, **cfg):
    state = cfg.pop("state", None)
    return AutoscalerLoop(
        control=control,
        backend=backend,
        policy=PolicyConfig(**cfg),
        state=state,
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


async def test_warm_pool_floor_enforced_when_idle():
    backend = FakeBackend(current=0)
    control = FakeControl(
        [_payload([], fleet_util=0.0, active=0)]
    )
    loop = _loop(control, backend, min_replicas=1)
    await loop.tick()
    assert backend.scaled == [1]


async def test_orphaned_draining_node_is_retired():
    class NodeBackend(FakeBackend):
        def has_node(self, node_id):
            return True

    backend = NodeBackend(current=2)
    control = FakeControl(
        [
            _payload(
                [_node("zombie", draining=True), _node("ok")],
                fleet_util=0.1,
            )
        ]
    )
    loop = _loop(control, backend, min_replicas=1, scale_down_util=0.4)
    await loop.tick()
    assert backend.removed == ["zombie"]


async def test_stale_503_latch_does_not_block_scale_down():
    backend = FakeBackend(current=3)
    control = FakeControl(
        [
            _payload(
                [_node("a"), _node("b"), _node("c")],
                fleet_util=0.1,
                recent503=5,
            )
        ]
    )
    loop = _loop(control, backend, min_replicas=1, scale_down_util=0.4)
    await loop.tick()
    assert control.drained == ["a"]
    assert backend.removed == ["a"]


async def test_cooldown_is_shared_between_replicas():
    """Two control-plane replicas run two loops; the marks are not theirs.

    The merged (k8s) shape hosts the loop in the control plane, so which
    replica wins the tick claim changes from tick to tick. A replica that has
    never ticked must still honour its peer's scale-up cooldown -- otherwise
    the fleet grows once per replica per cooldown window.
    """
    payload = _payload([_node("a"), _node("b")], fleet_util=0.8, active=8)
    store = InMemoryLoopState()
    backend = FakeBackend(current=2)

    first = _loop(
        FakeControl([payload]),
        backend,
        warmup_buffer=1,
        scale_up_cooldown_s=60,
        state=store,
    )
    await first.tick()
    second = _loop(
        FakeControl([payload]),
        backend,
        warmup_buffer=1,
        scale_up_cooldown_s=60,
        state=store,
    )
    await second.tick()

    assert backend.scaled == [3]


async def test_an_in_flight_drain_is_shared_between_replicas():
    """A drain the peer started must be finished, not duplicated.

    The loop holds the fleet at one drain at a time by returning early while a
    node drains (step 1). When the loop is restarted -- a control-plane
    rollout, which the merged shape makes routine -- that in-memory "am I
    draining" answer is gone, and the next tick would start draining a second
    node while the first still holds live sandboxes.
    """

    class DelayedControl(FakeControl):
        def drain(self, node_id):
            self.drained.append(node_id)
            return {"nodeID": node_id, "activeSandboxes": 3}

    store = InMemoryLoopState()
    backend = FakeBackend(current=3)
    first_control = DelayedControl(
        [_payload([_node("a"), _node("b"), _node("c")], fleet_util=0.1)]
    )
    first = _loop(
        first_control,
        backend,
        min_replicas=1,
        scale_down_util=0.4,
        state=store,
    )
    await first.tick()
    assert first_control.drained == ["a"]

    second_control = DelayedControl(
        [
            _payload(
                [_node("a", draining=True, active=3), _node("b"), _node("c")],
                fleet_util=0.1,
            )
        ]
    )
    second = _loop(
        second_control,
        backend,
        min_replicas=1,
        scale_down_util=0.4,
        state=store,
    )
    await second.tick()

    assert second_control.drained == []
    assert backend.removed == []


async def test_scale_down_waits_when_the_victim_would_be_another_pod():
    """N51: the loop may only shrink when the backend can take *its* node.

    The backend answers with the pod this workload's controller would actually
    delete; `None` means "not one of the idle nodes" -- and then shrinking this
    tick would kill a pod the loop never chose (on a StatefulSet: the highest
    ordinal, which may be running sandboxes).
    """
    backend = FakeBackend(current=3, retire_pick="refuse")
    control = FakeControl(
        [_payload([_node("a"), _node("b"), _node("c")], fleet_util=0.1)]
    )
    loop = _loop(control, backend, min_replicas=1, scale_down_util=0.4)

    await loop.tick()

    assert control.drained == []
    assert backend.removed == []
    assert backend.current_n == 3


async def test_the_drained_node_is_the_one_the_backend_would_delete():
    """And when the backend names a different idle node, that is the one."""
    backend = FakeBackend(current=3, retire_pick="c")
    control = FakeControl(
        [_payload([_node("a"), _node("b"), _node("c")], fleet_util=0.1)]
    )
    loop = _loop(control, backend, min_replicas=1, scale_down_util=0.4)

    await loop.tick()

    assert control.drained == ["c"]
    assert backend.removed == ["c"]


async def test_an_orphaned_drain_is_not_retired_below_the_warm_floor():
    """The orphan sweep asks the same question the main branch does.

    It used to retire any draining, idle, healthy node it found -- without
    checking whether the fleet could afford to lose a replica (N51, second
    half). An operator's `drain` on a fleet already at `min_replicas` shrank it
    below the floor, and the floor branch grew it back on the next tick.
    """

    class NodeBackend(FakeBackend):
        def has_node(self, node_id):
            return True

    backend = NodeBackend(current=1)
    control = FakeControl(
        [_payload([_node("zombie", draining=True), _node("ok")], fleet_util=0.1)]
    )
    loop = _loop(control, backend, min_replicas=1, scale_down_util=0.4)

    await loop.tick()

    assert backend.removed == []
