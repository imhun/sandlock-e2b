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
synchronous calls, the candidate line / watchdog / claim-TTL ones go red for
not existing at all, the "one bad round" one is a guard that already held, and
the last one pins the probe's refusal plus its read-only sampling.
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
PROBE_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "scripts"
    / "acceptance"
    / "probe_ttl_sweep_reap.py"
)


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance_to(self, value: float) -> None:
        assert value >= self.now
        self.now = value


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
async def test_a_starved_claim_is_named_after_the_grace_period(caplog):
    """A claim nobody releases has to be named, once, with how long it held.

    Two replicas run with a claim TTL that used to be *shorter* than a round
    (1 s against 17.1 s of ``rmtree``): a peer can then hold the claim round
    after round and every round this replica loses is silence -- the shape the
    8 never-reaped records would produce if their sweeper never won a round.
    The grace period is short here so the test does not wait 30 s.
    """
    clock = _FakeClock()
    claimed = [False]
    sweeper = TTLSweeper(
        interval_seconds=0.01,
        claim=lambda: claimed[0],
        starve_after_s=0.05,
        clock=clock,
    )
    caplog.set_level(logging.INFO, logger="control_plane.registry.ttl")
    sweeper.start(_BlockingRegistry())
    try:
        # Below the grace period: losing a round is normal (the peer is
        # sweeping), and a line per second would be noise, not a signal.
        await asyncio.sleep(0.05)
        assert _messages(caplog, level=logging.WARNING) == []

        clock.advance_to(0.2)
        for _ in range(200):
            if _messages(caplog, level=logging.WARNING):
                break
            await asyncio.sleep(0.01)
        expected = (
            "TTL sweep starved: the fleet-wide claim e2b:ttl:sweep has been "
            "held for 0.2s; no expired record can be reaped while this lasts"
        )
        assert _messages(caplog, level=logging.WARNING) == [expected]

        # Still starved, much longer: one line, not one per round.
        clock.advance_to(60.0)
        await asyncio.sleep(0.05)
        assert _messages(caplog, level=logging.WARNING) == [expected]

        claimed[0] = True
        for _ in range(200):
            if _messages(caplog, level=logging.INFO):
                break
            await asyncio.sleep(0.01)
        assert _messages(caplog, level=logging.INFO) == [
            "TTL sweep: the fleet-wide claim e2b:ttl:sweep is free again "
            "after 60.0s; reaping resumes"
        ]
    finally:
        await sweeper.stop()


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


def test_the_sweep_claim_outlives_a_round_not_just_the_interval():
    """The claim's TTL is bounded by a *round*, not by the cadence (N61).

    A round lists the shared records and tears sandboxes down one at a time --
    one ``rmtree`` measured 17.1 s -- so a claim that expires with the 1 s
    cadence lets the second replica start its own round inside the first one's
    teardown (duplicated teardown calls, duplicated ``TTL expired`` lines).
    The claim's TTL is the upper bound on a round and has to be longer than
    the round it guards.
    """
    from control_plane.app import _TTL_SWEEP_CLAIM_TTL_S, _TTL_SWEEP_INTERVAL_S

    assert _TTL_SWEEP_CLAIM_TTL_S == 60
    assert _TTL_SWEEP_CLAIM_TTL_S > _TTL_SWEEP_INTERVAL_S


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
