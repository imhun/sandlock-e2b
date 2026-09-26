"""The disk report is a background round, not a step of the heartbeat (N25).

The walk is a blocking NFS traversal, and it used to run inline in the pulse:
a round could hold the event loop for its whole 1 s budget, which is what
capped ``E2B_DISK_ENFORCE_INTERVAL_S`` at minutes. These tests pin the shape
that removes the cap -- a worker-thread scan behind a single-flight guard, with
the heartbeat reading whatever the last completed round produced.
"""

from __future__ import annotations

import asyncio
import threading
import time


import pytest

from envd_service.agent import NodeAgent
from envd_service.config import Settings


class _BlockingRegistry:
    """A runtime registry whose scan blocks until the test releases it."""

    def __init__(self, *, release: threading.Event, fail: bool = False) -> None:
        self.release = release
        self.fail = fail
        self.calls = 0

    def disk_usage_snapshot(
        self, *, budget_s: float | None = None, dirty: bool = False
    ) -> dict[str, int]:
        self.calls += 1
        assert budget_s == 1.0
        assert dirty is False, "this fake does not model the dirty path"
        self.release.wait(5)
        if self.fail:
            raise OSError("the base is unreachable")
        return {"sbx_a": 1024}

    def activity_snapshot(self) -> dict[str, float]:
        return {}


class _RecordingRegistry(_BlockingRegistry):
    """A registry that crosses a budget once, then stays over it."""

    def __init__(self, *, release: threading.Event) -> None:
        super().__init__(release=release)
        self._over = False

    def disk_usage_snapshot(
        self, *, budget_s: float | None = None, dirty: bool = False
    ) -> dict[str, int]:
        self.calls += 1
        return {"sbx_cross": 2048}

    def take_budget_crossings(self) -> dict[str, int]:
        if self._over:
            return {}
        self._over = True
        return {"sbx_cross": 2048}


def _agent(registry, workspace, *, interval: float = 0.001) -> NodeAgent:
    settings = Settings(workspace_base=str(workspace))
    settings.disk_enforce_interval_s = interval  # type: ignore[attr-defined]
    agent = NodeAgent(
        settings=settings,
        runtime_registry=registry,
        control_plane_url="http://control",
        node_address="http://127.0.0.1:1",
    )
    # The interval is read once at construction from the environment; override
    # it here so the test does not have to mutate os.environ.
    agent._disk_interval_s = interval
    agent._node_id = "node_a"
    return agent


@pytest.fixture()
def release():
    event = threading.Event()
    yield event
    event.set()


async def test_the_heartbeat_never_waits_for_the_walk(tmp_path, release):
    registry = _BlockingRegistry(release=release)
    agent = _agent(registry, tmp_path)

    started = time.perf_counter()
    first = agent._disk_report_for_heartbeat()
    elapsed = time.perf_counter() - started

    # The walk is blocked in the provider; the pulse returns anyway, with the
    # (empty) report it already had.
    assert elapsed < 0.05, f"the heartbeat waited {elapsed:.3f}s for the walk"
    assert first == {}

    # The round did start, and is now sitting in the provider (a thread).
    for _ in range(200):
        if registry.calls:
            break
        await asyncio.sleep(0.01)
    assert registry.calls == 1
    assert agent._disk_report_for_heartbeat() == {}

    release.set()
    await agent._disk_scan_task
    assert agent._disk_report_for_heartbeat() == {"sbx_a": 1024}


async def test_only_one_round_is_in_flight(tmp_path, release):
    registry = _BlockingRegistry(release=release)
    agent = _agent(registry, tmp_path)

    for _ in range(5):  # five pulses, none of which may start a second walk
        agent._disk_report_for_heartbeat()
        await asyncio.sleep(0)

    # The walk itself runs in a thread (`asyncio.to_thread`), so "one call" only
    # becomes observable once that thread has been scheduled -- and five loop
    # iterations are not always enough for that on a loaded runner (measured
    # 2026-09-25: `0 == 1` inside the gate's container while the same test
    # passed on an idle host). Wait for the count to appear; the assertion is
    # that it never becomes two, which the blocked provider still guarantees.
    for _ in range(200):
        if registry.calls:
            break
        await asyncio.sleep(0.01)
    assert registry.calls == 1
    release.set()
    await agent._disk_scan_task


async def test_the_cadence_is_claimed_even_when_the_walk_fails(tmp_path, release):
    """A failing base must not turn every pulse into another walk."""
    release.set()
    registry = _BlockingRegistry(release=release, fail=True)
    agent = _agent(registry, tmp_path, interval=3600.0)

    agent._disk_report_for_heartbeat()
    await agent._disk_scan_task

    agent._disk_report_for_heartbeat()
    await asyncio.sleep(0)
    assert registry.calls == 1


async def test_stop_cancels_a_round_in_flight(tmp_path, release):
    registry = _BlockingRegistry(release=release)
    agent = _agent(registry, tmp_path)
    agent._disk_report_for_heartbeat()
    assert agent._disk_scan_task is not None

    await agent.stop()

    assert agent._disk_scan_task is None


async def test_a_disabled_interval_never_scans(tmp_path, release):
    registry = _BlockingRegistry(release=release)
    agent = _agent(registry, tmp_path)
    agent._disk_interval_s = 0.0

    assert agent._disk_report_for_heartbeat() == {}
    assert agent._disk_scan_task is None
    assert registry.calls == 0


async def test_a_crossing_is_pushed_without_waiting_for_the_pulse(tmp_path, release):
    """N25: a sandbox that just went over its budget is reported at once.

    Everything else can ride the pulse (it is the metric, and drift); a sandbox
    that has *crossed* its budget is the one case where each second of delay is
    another second of writing, because the control plane's answer is to freeze
    it.
    """
    release.set()
    registry = _RecordingRegistry(release=release)
    agent = _agent(registry, tmp_path)
    pushed: list[dict[str, int]] = []

    async def _record_push(usage) -> None:
        pushed.append(dict(usage))

    agent._push_disk_report = _record_push  # type: ignore[assignment]
    agent._disk_report_for_heartbeat()
    await agent._disk_scan_task
    await asyncio.sleep(0)

    assert pushed == [{"sbx_cross": 2048}]
    assert agent._push_task is not None

    # ...and a sandbox that *stays* over budget is not news: no second push.
    agent._disk_report_for_heartbeat()
    await agent._disk_scan_task
    await asyncio.sleep(0)
    assert pushed == [{"sbx_cross": 2048}]
