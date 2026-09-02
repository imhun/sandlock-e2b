"""Event-driven create queue for capacity-constrained sandbox creation (E9.4).

Pure asyncio, no global state: one :class:`CreateQueue` lives per
control-plane process (wired in ``control_plane/app.py``), is woken by the
sandbox registry's quota-release hook, and is used by ``POST /sandboxes``
when an eviction round (E9.3) still left no room.

Known limitations (documented in docs/resource-contention.md §3.5 / §8):
waiters are woken by release broadcasts with **no ordering or fairness
guarantee**, and queue state is per control-plane replica — a multi-replica
fleet does not share one queue, so capacity released on replica A may be
claimed by a create arriving at replica B.
"""

from __future__ import annotations

import asyncio
import enum
import threading
import time
from collections.abc import Awaitable, Callable

#: Fallback poll interval when no capacity-release signal arrives. A waiter
#: re-probes at least this often, so a lost wakeup can never strand it until
#: its deadline (the release signal is an optimization, the tick a guard).
_DEFAULT_TICK_S = 1.0

#: Module-level so unit tests can substitute a fake clock (see
#: tests/unit/test_create_queue.py) without waiting real time.
_monotonic = time.monotonic


class QueueOutcome(enum.Enum):
    """Result of one :meth:`CreateQueue.wait_for_capacity` call."""

    ADMITTED = "admitted"  # probe() reported success (capacity consumed)
    TIMEOUT = "timeout"  # probe() never succeeded before the deadline
    FULL = "full"  # max_waiters already queued; do not wait
    DISABLED = "disabled"  # timeout_s <= 0: queueing is turned off


async def _wait_event_or_timeout(event: asyncio.Event, timeout: float) -> None:
    """Wait until ``event`` is set or ``timeout`` seconds elapse.

    Module-level seam so tests can swap in a fake clock/tick without real
    waiting (see tests/unit/test_create_queue.py).
    """
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


class CreateQueue:
    """Bound capacity queue for ``POST /sandboxes`` (E9.4).

    ``wait_for_capacity`` runs a caller-supplied async ``probe`` (the full
    admission path) whenever capacity may have been released: immediately on
    entry, when :meth:`notify_capacity` fires (a real registry quota
    release), and on a bounded fallback tick of at most
    ``min(tick_s, remaining time)``. Waiters never busy-poll and never
    over-sell: every probe is the same atomic admission used outside the
    queue, so one released slot admits exactly one waiter per release.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 30.0,
        max_waiters: int = 100,
        tick_s: float = _DEFAULT_TICK_S,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._max_waiters = int(max_waiters)
        self._tick_s = max(0.0, float(tick_s))
        self._waiters = 0
        self._waiter_events: set[asyncio.Event] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def stats(self) -> dict[str, int | float]:
        """Live queue depth plus the configured bounds (observability)."""
        with self._lock:
            waiting = self._waiters
        return {
            "waiting": waiting,
            "timeout_s": self._timeout_s,
            "max": self._max_waiters,
        }

    def notify_capacity(self) -> None:
        """Wake every waiter; safe to call from any thread.

        ``SandboxRegistry.add_on_quota_released`` fires this after a real
        quota release (pause / delete / expiry). That path may run outside
        the asyncio event loop (the TTL removal chain does synchronous
        httpx), and asyncio primitives are not thread-safe, so events are
        set through ``loop.call_soon_threadsafe``.
        """
        with self._lock:
            loop = self._loop
            events = list(self._waiter_events)
        if loop is None or not events:
            # No waiter has registered a loop yet (or none is waiting):
            # a later waiter's first probe makes the missed release moot.
            return
        for event in events:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # pragma: no cover - loop shutting down
                # Waiters get cancelled by their caller anyway; the bounded
                # tick remains the liveness fallback.
                pass

    async def wait_for_capacity(
        self,
        probe: Callable[[], Awaitable[bool]],
        *,
        timeout_s: float | None = None,
        max_waiters: int | None = None,
    ) -> QueueOutcome:
        """Wait up to ``timeout_s`` for ``probe()`` to succeed.

        ``probe`` is the caller's full admission attempt: return ``True``
        only when it actually consumed capacity (the create succeeded), and
        ``False`` when capacity is still unavailable. Non-capacity errors
        raised by ``probe`` propagate unchanged after the queue slot is
        released. A waiting request holds no node quota / pending marker
        (its probe rolled back before queueing), so an idempotent
        ``X-Sandbox-Id`` retry is never stuck behind its own marker.

        ``timeout_s <= 0`` returns :attr:`QueueOutcome.DISABLED` without
        probing (queueing off). More than ``max_waiters`` concurrent waiters
        return :attr:`QueueOutcome.FULL` immediately. Cancelling the caller
        (client disconnect) releases the queue slot.
        """
        timeout = float(self._timeout_s if timeout_s is None else timeout_s)
        if timeout <= 0:
            return QueueOutcome.DISABLED
        limit = int(self._max_waiters if max_waiters is None else max_waiters)
        event = self._register_waiter(max(0, limit))
        if event is None:
            return QueueOutcome.FULL
        tick_s = self._tick_s
        try:
            deadline = _monotonic() + timeout
            while True:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    return QueueOutcome.TIMEOUT
                # Capacity may already be free (released between the last
                # failed attempt and this call): probe before sleeping.
                if await probe():
                    return QueueOutcome.ADMITTED
                # Still short: sleep until the next release signal or the
                # bounded tick (min(1s, remaining)) — never a busy loop.
                event.clear()
                wait_for = min(tick_s, remaining) if tick_s > 0 else remaining
                await _wait_event_or_timeout(event, wait_for)
        finally:
            self._unregister_waiter(event)

    def _register_waiter(self, limit: int) -> asyncio.Event | None:
        """Claim one queue slot; ``None`` when the queue is already full."""
        with self._lock:
            if self._waiters >= limit:
                return None
            self._waiters += 1
            if self._loop is None:
                self._loop = asyncio.get_running_loop()
            event = asyncio.Event()
            self._waiter_events.add(event)
            return event

    def _unregister_waiter(self, event: asyncio.Event) -> None:
        """Return a queue slot (success, timeout, error, or cancellation)."""
        with self._lock:
            self._waiter_events.discard(event)
            self._waiters = max(0, self._waiters - 1)
