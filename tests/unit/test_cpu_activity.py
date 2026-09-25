"""E9.1 blind spot 2: a sandbox that only burns CPU is not idle.

The idle signal is "requests that crossed envd" (plus lifecycle calls on the
control plane), so a sandbox running a long CPU-bound task with no requests and
no egress looked exactly like an empty one -- eviction paused it and released
its reservation while it was working (`docs/resource-contention.md` §6).

These pin the two halves of the answer: the CPU reading itself (one `/proc`
pass, summed per owning uid) and the decision (a *percentage of one core* over
the sampling window, not "there was a delta"). The last test walks the marking
through to the heartbeat payload, because the point of the design is that it
rides the existing `sandboxActivity` channel.
"""

from __future__ import annotations

import os
from pathlib import Path

from envd_service.agent import NodeAgent
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.cpu_activity import (
    CpuActivityTracker,
    parse_stat_cpu,
    sample_cpu_ticks,
)
from envd_service.runtime.registry import RuntimeRegistry

#: utime=100, stime=50 -> 150 ticks. The process name deliberately contains a
#: space *and* a parenthesis: `/proc` readers have to split on the **last** `)`.
SAMPLE_STAT = (
    "1234 (weird ) name) S 1 1234 1234 0 -1 4194560 100 0 0 0 "
    "100 50 0 0 20 0 1 0 12345 0 0\n"
)


def test_a_stat_line_is_read_from_the_last_paren() -> None:
    assert parse_stat_cpu(SAMPLE_STAT) == 150


def _fake_proc(tmp_path: Path, procs: dict[int, tuple[int, int]]) -> Path:
    """A `/proc` with one directory per pid: `{pid: (uid, ticks)}`."""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, (uid, ticks) in procs.items():
        entry = root / str(pid)
        entry.mkdir()
        # utime = ticks, stime = 0
        (entry / "stat").write_text(
            f"{pid} (proc) S 1 {pid} {pid} 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0 0 0 0\n",
            encoding="utf-8",
        )
        (entry / "uid").write_text(str(uid), encoding="utf-8")
    return root


def test_one_pass_sums_ticks_by_owning_uid(tmp_path: Path) -> None:
    """Two processes of one sandbox, one of another, plus noise to skip."""
    root = _fake_proc(tmp_path, {10: (1001, 30), 11: (1001, 12), 12: (1002, 7)})
    (root / "self").mkdir()  # a non-numeric entry: /proc always has these
    (root / "13").mkdir()  # a process that died before its stat was read

    ticks = sample_cpu_ticks(
        root,
        owner_uid=lambda path: int((path / "uid").read_text(encoding="utf-8")),
    )

    assert ticks == {1001: 42, 1002: 7}


def test_the_tracker_reports_percent_of_one_core() -> None:
    tracker = CpuActivityTracker(percent_threshold=5.0, ticks_per_second=100)

    assert tracker.observe({1001: 500}, now=1000.0) == {}, (
        "the first sample has nothing to compare against"
    )

    # 0.5 CPU-second over a 5-second window = 10% of one core.
    percents = tracker.observe({1001: 550}, now=1005.0)
    assert percents == {1001: 10.0}
    assert tracker.busy(percents) == {1001}

    # A process that woke once is not work: 0.05% is far below the threshold.
    quiet = tracker.observe({1001: 550 + 0}, now=1010.0)
    assert quiet == {}, "no delta, no percentage"


def test_a_uid_that_went_backwards_is_not_activity() -> None:
    """A recycled pid/uid must not produce a negative or a bogus percentage."""
    tracker = CpuActivityTracker(percent_threshold=5.0, ticks_per_second=100)
    tracker.observe({1001: 900}, now=1000.0)

    percents = tracker.observe({1001: 10}, now=1005.0)

    assert percents == {}
    assert tracker.busy(percents) == set()


class _FakeRegistry:
    """The two calls the CPU round makes: list the sandboxes, mark one active."""

    def __init__(self, records) -> None:
        self._records = records
        self.marked: list[str] = []

    def list(self):
        return list(self._records)

    def mark_active(self, sandbox_id: str) -> None:
        self.marked.append(sandbox_id)


class _Record:
    def __init__(self, sandbox_id: str, host_uid: int | None, state: str = "running"):
        self.sandbox_id = sandbox_id
        self.host_uid = host_uid
        self.state = state


async def _round(agent: NodeAgent, sampler) -> dict[str, float]:
    agent._cpu_sampler = sampler
    return await agent._cpu_activity_round()


async def test_a_burning_sandbox_is_marked_and_an_idle_one_is_not(workspace) -> None:
    registry = _FakeRegistry(
        [
            _Record("sbx_burning", 1001),
            _Record("sbx_idle", 1002),
            _Record("sbx_shared_uid", None),
            _Record("sbx_paused", 1003, state="paused"),
        ]
    )
    agent = NodeAgent(
        settings=EnvdSettings(workspace_base=workspace),
        runtime_registry=registry,
        control_plane_url="http://127.0.0.1:9",
        node_address="http://127.0.0.1:9",
    )
    agent._cpu_tracker = CpuActivityTracker(
        percent_threshold=5.0, ticks_per_second=100
    )

    await _round(agent, lambda: {1001: 1000, 1002: 500, 1003: 900})
    # The paused sandbox gets a *delta* as well, so the state filter (not an
    # absence of CPU) is what keeps it out.
    marked = await _round(agent, lambda: {1001: 1000 + 900, 1002: 500, 1003: 900 + 900})

    # 900 ticks = 9 CPU-seconds over a window that is milliseconds wide: far
    # above the threshold, and the assertion does not depend on the wall clock.
    assert list(marked) == ["sbx_burning"], (
        "only the burning sandbox: not the idle one, not the shared-uid one "
        "(its CPU cannot be told apart from the worker's), not the paused one"
    )
    assert registry.marked == ["sbx_burning"]
    assert marked["sbx_burning"] >= 5.0


async def test_cpu_activity_reaches_the_heartbeat_payload(workspace) -> None:
    """The whole point: it rides `sandboxActivity`, not a new wire field."""
    registry = RuntimeRegistry(workspace)
    sandbox_dir = workspace / "sbx_cpu"
    sandbox_dir.mkdir()
    (sandbox_dir / "workspace").mkdir()
    registry.register(
        sandbox_id="sbx_cpu",
        access_token="tok",
        workspace_dir=str(sandbox_dir),
        host_uid=os.geteuid(),
    )
    agent = NodeAgent(
        settings=EnvdSettings(workspace_base=workspace),
        runtime_registry=registry,
        control_plane_url="http://127.0.0.1:9",
        node_address="http://127.0.0.1:9",
    )
    agent._cpu_tracker = CpuActivityTracker(
        percent_threshold=5.0, ticks_per_second=100
    )

    await _round(agent, lambda: {os.geteuid(): 100})
    await _round(agent, lambda: {os.geteuid(): 100 + 500})

    snapshot = registry.activity_snapshot()
    assert list(snapshot) == ["sbx_cpu"], (
        "the CPU sample has to land in the registry's activity map, which is "
        "what the heartbeat ships"
    )
    assert snapshot["sbx_cpu"] > 0
