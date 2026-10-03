"""E9.1 x E9.2: pause a sandbox nobody is using (``E2B_IDLE_PAUSE_AFTER_S``).

A sandbox is a short-lived object: it starts fast, and the interesting question
is not "when does its caller's timeout expire" but "is anyone using it". A
sandbox that has been quiet for the threshold is *frozen* (SIGSTOP on the
worker) and its admission reservation is returned to the pools -- the session
survives, the capacity does not stay held. ``Sandbox.connect`` thaws it and
buys the capacity back.

Three things make this safe to ship, and each is a decision rather than a
detail:

* **``0`` means off, and off is inert.** No task is started and no claim is
  taken, so merging this changes nothing until a deployment sets the value.
  The action is user-visible (their sandbox stops running), so the default has
  to be "do nothing", not "be gentle".
* **The action is the endpoint's chain, not a parallel one.** Pausing is
  ``registry.pause`` (global/tenant) + park the node reservation + push the
  freeze to the worker, and ``idle_pause`` calls
  ``api.sandboxes.pause_record_for_platform``, which is literally that chain
  with a request-free door. A sweeper that "paused" records on its own would
  eventually mean something different by the word.
* **Activity is measured where the traffic is.** The worker reports
  per-sandbox activity (commands, file access, proxied requests, and CPU above
  ``E2B_CPU_ACTIVITY_PERCENT``) on its heartbeat, and an open stream keeps
  re-stamping it for as long as it is open. So "idle" here means idle -- not
  "the control plane did not see a request".

The selection also refuses two shapes on purpose: a record that is already
past ``end_at`` belongs to the TTL sweep (the deadline is explicit and the
caller asked for it), and a record that dies within ``min_remaining_s`` is not
worth the churn of pausing and immediately deleting.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from gateway_common.timeutil import utcnow

logger = logging.getLogger(__name__)

#: ``E2B_IDLE_PAUSE_AFTER_S``: seconds of measured inactivity before a running
#: sandbox is paused. ``0`` (the default) disables the sweep entirely.
IDLE_PAUSE_ENV = "E2B_IDLE_PAUSE_AFTER_S"

#: Sandbox metadata key that opts one sandbox out of the sweep.
IDLE_PAUSE_METADATA_KEY = "e2b_pause_on_idle"
_OPT_OUT_VALUES = frozenset({"0", "false", "no", "off"})

#: A record that would be reaped by its own ``end_at`` within this many seconds
#: is left to the TTL sweep: pausing it would be a freeze immediately followed
#: by a teardown.
_DEFAULT_MIN_REMAINING_S = 60.0


def idle_pause_after_seconds(settings: Any = None) -> float:
    """The configured idle threshold in seconds; ``0`` = disabled."""
    configured = getattr(settings, "idle_pause_after_s", None)
    if configured is not None:
        return max(0.0, float(configured))
    raw = os.getenv(IDLE_PAUSE_ENV)
    if raw is None or raw.strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number; the idle pause sweep stays off",
            IDLE_PAUSE_ENV,
            raw,
        )
        return 0.0


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def idle_pause_exempt(record) -> bool:
    """Whether ``record`` asked not to be paused for idleness.

    The opt-out is metadata, so it travels with the sandbox the caller created
    (a long batch job that burns no CPU and holds no stream open has no other
    way to say "I am working on it").
    """
    metadata = getattr(record, "metadata", None) or {}
    raw = metadata.get(IDLE_PAUSE_METADATA_KEY)
    if raw is None:
        return False
    return str(raw).strip().lower() in _OPT_OUT_VALUES


def idle_candidates(
    records: Iterable[object],
    *,
    after_s: float,
    now: datetime,
    min_remaining_s: float = _DEFAULT_MIN_REMAINING_S,
) -> list[tuple[object, float]]:
    """The ``(record, idle_seconds)`` pairs this round would pause, oldest first.

    ``after_s <= 0`` is the disabled switch and selects nothing -- including
    when an operator asks this function directly with a huge idle age in hand.
    """
    if after_s <= 0:
        return []
    moment = _as_utc(now)
    due: list[tuple[object, float]] = []
    for record in records:
        if idle_pause_exempt(record):
            continue
        end_at = _as_utc(record.end_at)
        if moment >= end_at:
            # Already expired: the TTL sweep owns it (and its teardown order).
            continue
        if (end_at - moment).total_seconds() < min_remaining_s:
            continue
        idle_s = record.idle_seconds(moment)
        if idle_s <= after_s:
            continue
        due.append((record, idle_s))
    due.sort(key=lambda pair: pair[1], reverse=True)
    return due


class IdlePauseSweeper:
    """Pause running sandboxes whose measured inactivity passed the threshold.

    ``after_s <= 0`` (the default) means the sweep is disabled: no task is
    started and no claim is taken, so nothing about the control plane's
    behaviour changes until a deployment decides otherwise.
    """

    def __init__(
        self,
        *,
        after_s: float,
        on_idle: Callable[[object, float], Any],
        interval_seconds: float = 15.0,
        claim: Callable[[], bool] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._after_s = max(0.0, float(after_s))
        self._on_idle = on_idle
        self._interval = interval_seconds
        #: ``F11`` step 4, the protocol every periodic job here uses: a TTL'd
        #: key is the whole claim, so a replica that dies mid-round costs the
        #: fleet one round and there is no lock to release. ``None`` is one
        #: process, which is the sweeper by definition.
        self._claim = claim
        self._now = now
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        """Whether the switch is on (``E2B_IDLE_PAUSE_AFTER_S > 0``)."""
        return self._after_s > 0

    def due(self, registry) -> list[tuple[object, float]]:
        """The idle sandboxes this round would pause."""
        if not self.enabled:
            return []
        return idle_candidates(
            registry.list(state_filter=["running"]),
            after_s=self._after_s,
            now=self._now(),
        )

    async def run_round(self, registry) -> int:
        """One round: take the fleet-wide claim, then pause what is due.

        Returns how many sandboxes this round actually paused. A record whose
        pause fails (a worker that refuses, a push that times out) is named in
        a WARNING and skipped -- one bad sandbox must never stop the round, and
        the next round retries it anyway.
        """
        if not self.enabled:
            return 0
        if self._claim is not None and not self._claim():
            return 0
        # The listing is a shared-store scan with a read per record (N61 moved
        # the same call off the loop for the TTL sweep); it leaves the loop.
        due = await asyncio.to_thread(self.due, registry)
        paused = 0
        for record, idle_s in due:
            try:
                result = self._on_idle(record, idle_s)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one record, not the round
                logger.warning(
                    "idle pause: sandbox %s failed to pause: %s",
                    record.sandbox_id,
                    exc,
                )
                continue
            paused += 1
            logger.info(
                "idle pause: sandbox %s idle %.0fs (>= %.0fs); paused",
                record.sandbox_id,
                idle_s,
                self._after_s,
            )
        return paused

    def start(self, registry) -> None:
        if not self.enabled:
            # Off is inert, not merely quiet: nothing is claimed and no task
            # exists to pause anything later in the process's life.
            logger.info(
                "idle pause sweep is off (%s=0): idle sandboxes keep running",
                IDLE_PAUSE_ENV,
            )
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(registry))

    async def _loop(self, registry) -> None:
        while True:
            try:
                await self.run_round(registry)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("idle pause sweep failed")
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
