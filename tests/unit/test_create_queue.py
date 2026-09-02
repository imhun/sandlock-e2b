"""E9.4 unit tests: CreateQueue waiting / wakeup / bounds in isolation.

The queue itself is pure asyncio (no registry / API), so every behaviour is
asserted here with exact equality: outcome enums, probe call counts, queue
depth, and elapsed-time bounds for the real-timer case.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import control_plane.queue as queue_module
from control_plane.queue import CreateQueue, QueueOutcome


async def _spin_until_waiting(queue: CreateQueue) -> None:
    """Yield until the queue registered the waiter (never busy-loops)."""
    while queue.stats()["waiting"] == 0:
        await asyncio.sleep(0.001)


async def test_zero_timeout_returns_disabled_without_probing():
    queue = CreateQueue(timeout_s=0, max_waiters=2)
    probes = 0

    async def probe() -> bool:
        nonlocal probes
        probes += 1
        return True

    outcome = await queue.wait_for_capacity(probe, timeout_s=0, max_waiters=2)
    assert outcome is QueueOutcome.DISABLED
    assert probes == 0
    assert queue.stats()["waiting"] == 0

    # Per-call timeout_s=0 also disables a queue that was configured on.
    configured = CreateQueue(timeout_s=30, max_waiters=2)
    outcome = await configured.wait_for_capacity(
        probe, timeout_s=0, max_waiters=2
    )
    assert outcome is QueueOutcome.DISABLED
    assert probes == 0


async def test_real_timeout_after_small_deadline():
    """probe never succeeds: TIMEOUT after roughly the configured 0.05s."""
    queue = CreateQueue(timeout_s=0.05, max_waiters=1, tick_s=0.01)

    async def probe() -> bool:
        return False

    started = time.monotonic()
    outcome = await queue.wait_for_capacity(probe, timeout_s=0.05, max_waiters=1)
    elapsed = time.monotonic() - started
    assert outcome is QueueOutcome.TIMEOUT
    assert elapsed >= 0.04
    assert elapsed < 1.0
    assert queue.stats()["waiting"] == 0


async def test_fake_clock_timeout_accounting(monkeypatch):
    """Deadline math with an injected clock: 2.5s = 1.0 + 1.0 + 0.5 tick."""
    fake_now = [0.0]
    monkeypatch.setattr(queue_module, "_monotonic", lambda: fake_now[0])

    async def instant_tick(event: asyncio.Event, timeout: float) -> None:
        fake_now[0] += timeout

    monkeypatch.setattr(queue_module, "_wait_event_or_timeout", instant_tick)
    queue = CreateQueue(timeout_s=30, max_waiters=1)
    probes = 0

    async def probe() -> bool:
        nonlocal probes
        probes += 1
        return False

    outcome = await queue.wait_for_capacity(
        probe, timeout_s=2.5, max_waiters=1
    )
    assert outcome is QueueOutcome.TIMEOUT
    assert fake_now[0] == 2.5
    assert probes == 3


async def test_notify_capacity_wakes_waiter_and_admits():
    queue = CreateQueue(timeout_s=30, max_waiters=2)
    attempts = 0

    async def probe() -> bool:
        nonlocal attempts
        attempts += 1
        return attempts >= 2

    waiter = asyncio.create_task(
        queue.wait_for_capacity(probe, timeout_s=5, max_waiters=2)
    )
    await _spin_until_waiting(queue)
    queue.notify_capacity()

    outcome = await waiter
    assert outcome is QueueOutcome.ADMITTED
    assert attempts == 2
    assert queue.stats()["waiting"] == 0


async def test_notify_from_other_thread_wakes_waiter():
    """release_quota may fire outside the loop (TTL removal chain)."""
    queue = CreateQueue(timeout_s=30, max_waiters=2)
    state = {"free": False}

    async def probe() -> bool:
        return state["free"]

    waiter = asyncio.create_task(
        queue.wait_for_capacity(probe, timeout_s=5, max_waiters=2)
    )
    await _spin_until_waiting(queue)
    state["free"] = True

    def _release_from_thread() -> None:
        queue.notify_capacity()

    thread = threading.Thread(target=_release_from_thread)
    thread.start()
    thread.join(timeout=5)

    outcome = await waiter
    assert outcome is QueueOutcome.ADMITTED
    assert queue.stats()["waiting"] == 0


async def test_fallback_tick_succeeds_before_timeout_without_notify():
    """A lost/absent release signal cannot strand a waiter: the 0.02s tick
    re-probes and admits well before the deadline."""
    queue = CreateQueue(timeout_s=1.0, max_waiters=1, tick_s=0.02)
    state = {"free": False}

    async def probe() -> bool:
        if state["free"]:
            return True
        state["free"] = True
        return False

    started = time.monotonic()
    outcome = await queue.wait_for_capacity(
        probe, timeout_s=1.0, max_waiters=1
    )
    elapsed = time.monotonic() - started
    assert outcome is QueueOutcome.ADMITTED
    assert elapsed < 0.8
    assert queue.stats()["waiting"] == 0


async def test_max_waiters_full_is_immediate_and_cancel_releases_slot():
    queue = CreateQueue(timeout_s=30, max_waiters=1)
    gate = asyncio.Event()

    async def blocking_probe() -> bool:
        await gate.wait()
        return False

    first = asyncio.create_task(
        queue.wait_for_capacity(blocking_probe, timeout_s=30, max_waiters=1)
    )
    await _spin_until_waiting(queue)

    # The queue is full: the second caller must not probe (its probe would
    # block) and returns FULL immediately.
    outcome = await queue.wait_for_capacity(
        blocking_probe, timeout_s=30, max_waiters=1
    )
    assert outcome is QueueOutcome.FULL
    assert queue.stats()["waiting"] == 1

    # Cancelling a waiter (client disconnect) returns its slot.
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert queue.stats()["waiting"] == 0

    # The freed slot is usable by a new waiter.
    second = asyncio.create_task(
        queue.wait_for_capacity(blocking_probe, timeout_s=30, max_waiters=1)
    )
    await _spin_until_waiting(queue)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert queue.stats()["waiting"] == 0


async def test_zero_max_waiters_means_immediate_full():
    queue = CreateQueue(timeout_s=30, max_waiters=0)
    outcome = await queue.wait_for_capacity(
        _never_succeeds, timeout_s=5, max_waiters=0
    )
    assert outcome is QueueOutcome.FULL


async def test_probe_error_releases_slot_and_propagates():
    queue = CreateQueue(timeout_s=30, max_waiters=1)

    async def probe() -> bool:
        raise ValueError("boom")

    with pytest.raises(ValueError) as excinfo:
        await queue.wait_for_capacity(probe, timeout_s=5, max_waiters=1)
    assert str(excinfo.value) == "boom"
    assert queue.stats()["waiting"] == 0


async def test_stats_reflects_config_and_live_depth():
    queue = CreateQueue(timeout_s=7, max_waiters=3)
    assert queue.stats() == {"waiting": 0, "timeout_s": 7.0, "max": 3}

    gate = asyncio.Event()

    async def blocking_probe() -> bool:
        await gate.wait()
        return False

    waiter = asyncio.create_task(
        queue.wait_for_capacity(blocking_probe, timeout_s=30, max_waiters=3)
    )
    await _spin_until_waiting(queue)
    assert queue.stats()["waiting"] == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert queue.stats()["waiting"] == 0


async def _never_succeeds() -> bool:
    raise AssertionError("probe must not run when the queue is full/disabled")


def test_settings_defaults_match_the_design_doc(monkeypatch):
    """Docs pin 30s / 100 waiters; a silent default change would alter how a
    saturated fleet answers every create."""
    for name in (
        "E2B_CREATE_QUEUE_TIMEOUT_S",
        "E2B_CREATE_QUEUE_MAX",
    ):
        monkeypatch.delenv(name, raising=False)
    from control_plane.config import Settings

    settings = Settings(api_keys=("local-key",))
    assert settings.create_queue_timeout_s == 30
    assert settings.create_queue_max == 100
