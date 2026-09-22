"""N32: a control plane that stalls must not read its own stall as a dead node.

The sweep's evidence is "this node's last heartbeat is older than the window".
While the loop is running on schedule that is exactly right. After a stall --
a blocking call inside a handler, a long pause -- the heartbeats that arrived
during it have not been handled yet, so every node looks lost at once and live
sandboxes are orphaned (measured on the k0s cluster, 2026-09-22: one 2000-file
snapshot, 76.1 s of access-log silence, then `orphaned sandboxes on
e2b-worker-1` and 409 for every later request).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from control_plane.app import _node_health_loop


class _Clock:
    """A scripted monotonic clock: one reading per call, clamped at the end."""

    def __init__(self, script: list[float]) -> None:
        self._script = script
        self._reads = 0
        self.last = script[0]

    def __call__(self) -> float:
        self.last = self._script[min(self._reads, len(self._script) - 1)]
        self._reads += 1
        return self.last


class _Nodes:
    """Records the clock reading at every verdict it is asked for."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.verdicts: list[float] = []

    def reap_unhealthy(self, registry) -> list[str]:
        self.verdicts.append(self._clock.last)
        return []


async def test_a_delayed_round_skips_its_verdict():
    # One scripted reading per round: a normal cadence, then one round that
    # starts 80 s late (the measured stall), then normal again.
    clock = _Clock([0.0, 1.0, 2.0, 82.0, 83.0, 84.0])
    nodes = _Nodes(clock)
    app = SimpleNamespace(state=SimpleNamespace(nodes=nodes))

    task = asyncio.create_task(
        _node_health_loop(
            app, registry=object(), window_s=30.0, interval_s=0.0, clock=clock
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Rounds before the stall made their verdict; the round that *started*
    # 80 s behind must not have made one (82.0 is its reading); the next round
    # (83.0, one second of real cadence) decides again.
    assert 2.0 in nodes.verdicts
    assert 82.0 not in nodes.verdicts, (
        f"the stalled round orphaned on its own stall: {nodes.verdicts}"
    )
    assert 83.0 in nodes.verdicts
