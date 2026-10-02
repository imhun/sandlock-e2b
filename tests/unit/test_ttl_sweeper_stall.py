"""N61: the TTL sweep must stay observable and off the event loop.

Measured 2026-10-02: 8 records with ``state: running`` and an ``endAt`` ~65 min
in the past were never reaped by a sweeper whose cadence is 1 s. The mechanism
(``_ttl_reapable`` collects ``running`` records the moment they are overdue)
does not explain the observation, so this batch does not guess a root cause: it
only makes the sweep *visible* (candidate line, starvation watchdog, a bad
round that stays a bad round) and *non-blocking* (the Redis scan and the
``rmtree`` leave the loop), plus a read-only probe the controller can run on
the live cluster.

The nails below are that visibility: the two blocking ones go red on today's
synchronous calls, the candidate line / overrun line / periodic summary ones go
red for not existing at all, the "one bad round" one is a guard that already
held, and the last one pins the probe's refusal plus its read-only sampling.

Two signals were ruled in on 2026-10-03 (N61 裁定 B) after the first draft's
starvation watchdog turned out to fire on a healthy fleet: ``try_claim`` is
``SET NX`` and *nothing releases the key*, so "this replica never won a round"
is the normal half of a healthy pair, not a fault. What is worth naming is a
round that outlives its own claim, and what is worth saying periodically is
whether this replica is sweeping at all -- both read-only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
from pathlib import Path

import pytest

from control_plane.registry.manager import SandboxRecord
from control_plane.registry.ttl import TTLSweeper

SANDBOX_OK = "sb-ok"
SANDBOX_AFTER_FAILURE = "sb-after-failure"
SUMMARY_PREFIX = "TTL sweep: this replica ran "
PROBE_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "scripts"
    / "acceptance"
    / "probe_ttl_sweep_reap.py"
)


class _StepClock:
    """A monotonic clock that advances a fixed step per read.

    Every measured duration is then a whole number of steps, so a message that
    formats one (``%.2fs``) is asserted **verbatim** instead of "about a
    fifth of a second".
    """

    def __init__(self, step: float) -> None:
        self._step = step
        self._reads = 0

    def __call__(self) -> float:
        value = self._reads * self._step
        self._reads += 1
        return value


class _ScriptedClaim:
    """A claim whose answers are scripted; the last answer repeats."""

    def __init__(self, answers: list[bool]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def __call__(self) -> bool:
        index = min(self.calls, len(self._answers) - 1)
        self.calls += 1
        return self._answers[index]


class _BlockingRegistry:
    """A registry whose work is synchronous and slow, like the real one.

    ``expired_candidates`` is the shared-store listing plus the per-record
    read; a round of teardown ends in ``cleanup_workspace`` (``shutil.rmtree``,
    measured at 17.1 s for one sandbox on the fleet). Both are plain blocking
    calls here -- the sweeper is the side that has to move them off the loop.
    """

    def __init__(
        self,
        *,
        records: list[SandboxRecord] | None = None,
        scan_s: float = 0.0,
        cleanup_s: float = 0.0,
        fail_scans: int = 0,
    ) -> None:
        self._records = list(records or [])
        self._scan_s = scan_s
        self._cleanup_s = cleanup_s
        self._fail_scans = fail_scans
        self.deleted: list[str] = []
        self.cleaned: list[str] = []
        self.round_done = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def expired_candidates(self):
        if self._scan_s:
            time.sleep(self._scan_s)
        if self._fail_scans > 0:
            self._fail_scans -= 1
            raise RuntimeError("scan failed")
        return list(self._records)

    def delete(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)

    def cleanup_workspace(self, record: SandboxRecord) -> None:
        if self._cleanup_s:
            time.sleep(self._cleanup_s)
        self.cleaned.append(record.sandbox_id)
        if len(self.cleaned) == len(self._records):
            # The measurement is "ticks before the round ended", so the signal
            # has to come from the last step of the round -- and, once the
            # cleanup runs in a worker thread, from that thread.
            self._loop.call_soon_threadsafe(self.round_done.set)


def _record(sandbox_id: str) -> SandboxRecord:
    return SandboxRecord(template_id="base", sandbox_id=sandbox_id, client_id="c")


async def _tick_until(done: asyncio.Event) -> int:
    """Counts 10 ms ticks until ``done`` -- the loop's own pulse."""
    ticks = 0
    while not done.is_set():
        await asyncio.sleep(0.01)
        ticks += 1
    return ticks


def _messages(caplog, *, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def _summaries(caplog) -> list[str]:
    """The periodic summary lines, selected by their fixed opening."""
    return [
        message
        for message in _messages(caplog, level=logging.INFO)
        if message.startswith(SUMMARY_PREFIX)
    ]


def _load_probe():
    """Load the probe by path: importing it must not do anything itself."""
    spec = importlib.util.spec_from_file_location("probe_ttl_sweep_reap", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_a_slow_candidate_scan_does_not_block_the_event_loop():
    """A 0.2 s scan must cost the loop ~0.2 s of its pulse, not all of it.

    Today ``expired_candidates()`` is called straight from the loop, so a scan
    that blocks (SCAN, then a read per record on NFS) freezes every other task
    -- the same shape N32 measured as a 76 s stall that looked like two dead
    workers.
    """
    registry = _BlockingRegistry(records=[_record(SANDBOX_OK)], scan_s=0.2)
    sweeper = TTLSweeper(interval_seconds=100.0, claim=lambda: True)
    sweeper.start(registry)
    try:
        ticks = await asyncio.wait_for(_tick_until(registry.round_done), timeout=5)
    finally:
        await sweeper.stop()
    assert ticks >= 8, "the candidate scan ran on the loop: the pulse stopped"


@pytest.mark.asyncio
async def test_cleanup_runs_off_the_loop():
    """The teardown's ``rmtree`` (17.1 s measured) must not freeze the loop.

    The candidate scan is instant here, so the only blocking call in the round
    is the cleanup -- which is the half of N32 that was left synchronous.
    """
    registry = _BlockingRegistry(records=[_record(SANDBOX_OK)], cleanup_s=0.2)
    sweeper = TTLSweeper(interval_seconds=100.0, claim=lambda: True)
    sweeper.start(registry)
    try:
        ticks = await asyncio.wait_for(_tick_until(registry.round_done), timeout=5)
    finally:
        await sweeper.stop()
    assert ticks >= 8, "the cleanup ran on the loop: the pulse stopped"


@pytest.mark.asyncio
async def test_the_candidate_line_names_the_ids_and_truncates(caplog):
    """A round has to say *which* records it is about to collect (N61).

    The observation is "8 overdue ``running`` records, never reaped"; without
    the candidate line the log cannot even answer "did the sweep see them?".
    Beyond ten ids the line truncates, so a fleet-wide backlog does not put
    every id in every round's line.
    """
    registry = _BlockingRegistry(
        records=[_record(f"sb-{index:02d}") for index in range(12)]
    )
    caplog.set_level(logging.INFO, logger="control_plane.registry.ttl")
    sweeper = TTLSweeper(interval_seconds=100.0, claim=lambda: True)
    sweeper.start(registry)
    try:
        await asyncio.wait_for(registry.round_done.wait(), timeout=5)
    finally:
        await sweeper.stop()
    assert _messages(caplog, level=logging.INFO) == [
        "TTL sweep: 12 expired candidate(s): sb-00, sb-01, sb-02, sb-03, sb-04, "
        "sb-05, sb-06, sb-07, sb-08, sb-09…(+2 more)",
        *[f"TTL expired sandbox sb-{index:02d}" for index in range(12)],
    ]


@pytest.mark.asyncio
async def test_a_round_that_outlives_three_intervals_is_named(caplog):
    """A round longer than 3 x the cadence is the suspect shape -- name it.

    The claim's TTL is the cadence on purpose (N61 裁定 A: ``try_claim`` has no
    release, so a longer TTL slows the whole fleet down), so a round that runs
    past 3 x the interval is running *without* a claim: a peer may have started
    its own round in the meantime. Naming that pair -- the real duration, the
    records this round reaped, and the fact that its own teardown still
    completed -- is the direct evidence for the "long round + expired claim"
    suspicion, without changing what the sweep does.
    """
    registry = _BlockingRegistry(records=[_record(SANDBOX_OK)])
    sweeper = TTLSweeper(
        interval_seconds=1.0, claim=lambda: True, clock=_StepClock(4.0)
    )
    caplog.set_level(logging.WARNING, logger="control_plane.registry.ttl")
    sweeper.start(registry)
    try:
        for _ in range(200):
            if _messages(caplog, level=logging.WARNING):
                break
            await asyncio.sleep(0.005)
    finally:
        await sweeper.stop()
    assert _messages(caplog, level=logging.WARNING) == [
        "TTL sweep: a round took 4.00s (>= 3 x the 1.0s cadence) and reaped 1 "
        "record(s); the fleet-wide claim expired while it ran, so a peer may "
        "have started its own round too -- this round's teardown is not lost"
    ]


@pytest.mark.asyncio
async def test_the_periodic_summary_says_whether_this_replica_sweeps(caplog):
    """One line per ~30 rounds that answers "did *this* replica sweep?".

    Losing a round to the peer's claim is normal, so the number that matters is
    not "how long since we won" but "how many of the last N rounds did we run,
    how long was the last one, how many records did we reap". Two windows are
    asserted, so the counters are pinned to *reset* rather than accumulate.
    """
    registry = _BlockingRegistry(records=[_record(SANDBOX_OK)])
    sweeper = TTLSweeper(
        interval_seconds=0.05,
        claim=_ScriptedClaim([True, True, False, True, True, False]),
        clock=_StepClock(0.25),
        overrun_after_s=1000.0,
        summary_every_rounds=3,
        summary_every_s=1000.0,
    )
    caplog.set_level(logging.INFO, logger="control_plane.registry.ttl")
    sweeper.start(registry)
    try:
        for _ in range(400):
            if len(_summaries(caplog)) >= 2:
                break
            await asyncio.sleep(0.005)
    finally:
        await sweeper.stop()
    assert _summaries(caplog) == [
        "TTL sweep: this replica ran 2 of the last 3 rounds; last round took "
        "0.25s; 2 candidate(s) reaped",
        "TTL sweep: this replica ran 2 of the last 3 rounds; last round took "
        "0.25s; 2 candidate(s) reaped",
    ]


@pytest.mark.asyncio
async def test_the_sweep_still_reaps_after_a_candidate_scan_failure(caplog):
    """One bad round must be exactly one bad round.

    A scan that raises today skips the whole round with a single ``TTL sweep
    failed``; the next round has to pick the same records up, or a transient
    Redis error would park every overdue sandbox until an operator noticed.
    """
    registry = _BlockingRegistry(
        records=[_record(SANDBOX_AFTER_FAILURE)], fail_scans=1
    )
    caplog.set_level(logging.WARNING, logger="control_plane.registry.ttl")
    sweeper = TTLSweeper(interval_seconds=0.01, claim=lambda: True)
    sweeper.start(registry)
    try:
        for _ in range(500):
            if registry.cleaned:
                break
            await asyncio.sleep(0.01)
    finally:
        await sweeper.stop()
    assert _messages(caplog, level=logging.ERROR) == ["TTL sweep failed"]
    assert registry.deleted == [SANDBOX_AFTER_FAILURE]
    assert registry.cleaned == [SANDBOX_AFTER_FAILURE]


def test_the_sweep_claim_still_expires_with_the_cadence():
    """N61 裁定 A（2026-10-03）：claim 的 TTL 不许超过节奏。

    ``try_claim`` 是 ``SET key 1 NX EX ttl``，且全仓库**没有任何**释放路径
    （``redis_backend.try_claim`` 自己的 docstring 写着 "there is no lock to
    release"）。所以把 claim 的 TTL 设成比节奏长，舰队级扫描周期就变成那个
    TTL —— 现场实测（真实 ``try_claim`` + 假 client，``ttl_s=60``）：A 第一轮
    ``True``、A 第二轮 ``False``、B 也 ``False``、key 的 TTL = 60。1 s 的节奏
    ⇒ 60 s 的周期，是行为回退。

    长轮次（> 1 s）会让 claim 在轮内过期、peer 可能同时开一轮：这是**已知代价**，
    不是本批要改的语义；要真正消除它需要带所有权校验的释放（要改
    ``redis_backend.try_claim`` 的 token 形状），不在本任务写集内。
    """
    from control_plane import app
    from control_plane.registry.redis_backend import try_claim

    assert app._TTL_SWEEP_INTERVAL_S == 1.0
    assert not hasattr(app, "_TTL_SWEEP_CLAIM_TTL_S"), (
        "claim 的 TTL 必须留在节奏上：没有释放路径，拉长就是让整个舰队变慢"
    )

    class _KeyValueStore:
        def __init__(self) -> None:
            self.entries: dict[str, int] = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.entries:
                return None
            self.entries[key] = ex
            return True

    client = _KeyValueStore()
    ttl_s = int(app._TTL_SWEEP_INTERVAL_S)
    assert try_claim(client, "e2b:ttl:sweep", ttl_s=ttl_s) is True
    # 持有者自己第二轮也拿不到；换成 60 就是 60 s 内整支舰队都拿不到。
    assert try_claim(client, "e2b:ttl:sweep", ttl_s=ttl_s) is False
    assert client.entries == {"e2b:ttl:sweep": 1}


class _FakeClaimClient:
    """A Redis stand-in that only has the two read commands N61 allows."""

    def __init__(self, samples: list[tuple[int, int]]) -> None:
        self._samples = list(samples)
        self._current = (0, 0)
        self.calls: list[str] = []

    def exists(self, key: str) -> int:
        self.calls.append("EXISTS")
        self._current = self._samples.pop(0)
        return self._current[0]

    def ttl(self, key: str) -> int:
        self.calls.append("TTL")
        return self._current[1]


def test_the_probe_refuses_without_redis_and_samples_read_only(monkeypatch, capsys):
    """The probe must be importable, refuse by name, and read only.

    It settles the N61 question on the live cluster, so the two things the
    controller has to be able to trust are: it never writes (only ``EXISTS`` /
    ``TTL`` / the registry's own reads) and a store it cannot reach is a
    refusal, not an empty report that looks like a healthy fleet.
    """
    probe = _load_probe()

    monkeypatch.delenv("E2B_REDIS_URL", raising=False)
    assert probe.main([]) == 2
    assert capsys.readouterr().err.splitlines() == [probe.NO_REDIS_URL]

    client = _FakeClaimClient([(1, 57), (0, -2), (1, -1)])
    assert probe.sample_claim(
        client, probe.CLAIM_KEY, samples=3, interval_s=1.0, sleep=lambda _s: None
    ) == [(1, 57), (0, -2), (1, -1)]
    assert client.calls == ["EXISTS", "TTL"] * 3

    assert probe.candidate_summary([f"sb-{i:02d}" for i in range(12)]) == (
        "sb-00, sb-01, sb-02, sb-03, sb-04, sb-05, sb-06, sb-07, sb-08, sb-09"
        "…(+2 more)"
    )
