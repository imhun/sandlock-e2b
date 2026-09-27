"""E6: the paused-sandbox TTL sweep (``E2B_PAUSED_TTL_S``, default 0 = off).

Parking a sandbox is supposed to preserve the session, and the ordinary TTL
sweep honours that by skipping ``paused`` records outright
(``SandboxRegistry._ttl_reapable``) -- a parked sandbox holds no admission
reservation, so expiring it would buy capacity while destroying user state.
The other half of that bargain was never written: nothing ever ends a park, so
a sandbox whose owner walked away keeps the platform account (its checkpoint
image is the largest thing the platform holds for a sandbox) forever.

This module is the opt-in that ends it. Two things make it safe to ship:

* **``0`` means off, and off means "no task, no claim, no work".** The switch is
  destructive -- the parked session really is gone -- so the default has to be
  inert, not merely conservative.
* **The cleanup is the one delete runs**, not a parallel one: the caller hands
  :func:`reap_paused_sandbox` the same teardown the ``DELETE
  /sandboxes/{id}`` endpoint uses (the worker's own delete, which is what
  removes ``_runtime/.checkpoints/<id>``), and the record goes after it. A
  sweeper that deleted records on its own would leave images and quota rows
  behind with no owner.

The period is the control plane's existing sweep cadence, and every round is
single-flight across replicas (the same TTL'd-key protocol ``F11`` uses for the
ordinary TTL sweep and the node-health sweep): the selection is made from
*shared* records, so two replicas sweeping the same window would otherwise pick
the same sandboxes and run the teardown twice.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from control_plane.registry.manager import UnknownSandboxError
from gateway_common.timeutil import utcnow

logger = logging.getLogger(__name__)

#: ``E2B_PAUSED_TTL_S``: how long a sandbox may sit ``paused`` before the sweep
#: tears it down. Seconds; ``0`` (the default) disables the sweep entirely.
#:
#: The value is a *policy* decision with an irreversible consequence, so it is
#: read from the environment (or from ``Settings.paused_ttl_s`` when a
#: deployment grows the field) rather than defaulted to anything usable.
PAUSED_TTL_ENV = "E2B_PAUSED_TTL_S"


def paused_ttl_seconds(settings: Any = None) -> float:
    """The configured paused-sandbox TTL in seconds; ``0`` = disabled."""
    configured = getattr(settings, "paused_ttl_s", None)
    if configured is not None:
        return max(0.0, float(configured))
    raw = os.getenv(PAUSED_TTL_ENV)
    if raw is None or raw.strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number; the paused TTL sweep stays off",
            PAUSED_TTL_ENV,
            raw,
        )
        return 0.0


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def paused_for_seconds(record, now: datetime) -> float | None:
    """How long ``record`` has been parked, in seconds.

    The stamp is ``record.paused_at``. A pause the *platform* started (eviction,
    a resume rollback) carries a reason and therefore a stamp; a caller-started
    pause does not, and the record falls back to ``last_active_at`` -- which
    the pause endpoint sets to the pause itself (``record.touch()``, E9.1) --
    so the fallback is the pause moment for the shape that actually happens,
    and an upper bound on it for a record parked by an older build.

    ``None`` means "this record cannot be aged" (never paused, or no stamp at
    all): the sweep skips it, because "unknown age" must never read as
    "ancient".
    """
    if getattr(record, "state", None) != "paused":
        return None
    since = getattr(record, "paused_at", None) or getattr(
        record, "last_active_at", None
    )
    if since is None:
        return None
    age = (_as_utc(now) - _as_utc(since)).total_seconds()
    return max(0.0, age)


def expired_paused_records(
    records: Iterable[object], *, ttl_s: float, now: datetime
) -> list[tuple[object, float]]:
    """The ``(record, age)`` pairs the sweep may reap, oldest first.

    ``ttl_s <= 0`` is the disabled switch and selects nothing -- *including*
    when an operator asks this function directly with a huge age in hand.
    """
    if ttl_s <= 0:
        return []
    due: list[tuple[object, float]] = []
    for record in records:
        age = paused_for_seconds(record, now)
        if age is None or age < ttl_s:
            continue
        due.append((record, age))
    due.sort(key=lambda pair: pair[1], reverse=True)
    return due


async def reap_paused_sandbox(
    record, *, registry, teardown: Callable[[object], Any]
) -> list[str]:
    """Delete one expired paused sandbox the way ``DELETE`` deletes it.

    Order is load-bearing and matches the endpoint: tear the sandbox down where
    it lives *first* (the worker's delete is what removes the runtime, the tree
    and ``_runtime/.checkpoints/<id>``), and drop the record only afterwards --
    a record that goes first leaves a tree nothing references, which is exactly
    the orphan the E4 reconcile then has to clean up.

    The reservation is returned through ``registry.delete`` (the same
    ``_release`` chain the endpoint uses). A sandbox paused by this build
    already gave its reservation back at pause time (E9.2) and the release is
    idempotent, so the only case where it moves anything is a record that still
    held it -- and that is why "quota" is reported only when it really went.

    Returns what was removed, for the caller's one named log line. A peer that
    deleted the record in the window between our read and our write is not an
    error: the teardown is idempotent and the record is already gone, so the
    report stops at what this pass actually did.
    """
    removed = list(await _maybe_await(teardown(record)) or [])
    held_reservation = not bool(getattr(record, "quota_released", False))
    try:
        registry.delete(record.sandbox_id)
    except UnknownSandboxError:
        return removed
    removed.append("record")
    if held_reservation:
        removed.append("quota")
    return removed


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class PausedTTLSweeper:
    """Periodically reaps sandboxes that have been paused past the TTL.

    ``ttl_s <= 0`` (the default) means the sweep is disabled: no task is
    started and no claim is taken, so nothing about the control plane's
    behaviour changes until a deployment decides otherwise.
    """

    def __init__(
        self,
        *,
        ttl_s: float,
        on_expired: Callable[[object, float], Any],
        interval_seconds: float = 1.0,
        claim: Callable[[], bool] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._ttl_s = max(0.0, float(ttl_s))
        self._on_expired = on_expired
        self._interval = interval_seconds
        #: ``F11`` step 4, same protocol as the ordinary TTL sweep: a TTL'd key
        #: is the whole claim, so a replica that dies mid-round costs the fleet
        #: one round and there is no lock to release. ``None`` is one process,
        #: which is the sweeper by definition.
        self._claim = claim
        self._now = now
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        """Whether the switch is on (``E2B_PAUSED_TTL_S > 0``)."""
        return self._ttl_s > 0

    def due(self, registry) -> list[tuple[object, float]]:
        """The expired parked sandboxes this round would reap."""
        if not self.enabled:
            return []
        return expired_paused_records(
            registry.list(state_filter=["paused"]), ttl_s=self._ttl_s, now=self._now()
        )

    def start(self, registry) -> None:
        if not self.enabled:
            # Off is inert, not merely quiet: nothing is claimed and no task
            # exists to reap anything later in the process's life.
            logger.info(
                "paused TTL sweep is off (%s=0): parked sandboxes are kept "
                "until an operator deletes them",
                PAUSED_TTL_ENV,
            )
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(registry))

    async def _loop(self, registry) -> None:
        while True:
            try:
                if self._claim is not None and not self._claim():
                    await asyncio.sleep(self._interval)
                    continue
                for record, paused_for_s in self.due(registry):
                    removed = await _maybe_await(
                        self._on_expired(record, paused_for_s)
                    )
                    logger.warning(
                        "paused TTL: sandbox %s had been paused %.0fs (>= %.0fs); "
                        "removed %s",
                        record.sandbox_id,
                        paused_for_s,
                        self._ttl_s,
                        ", ".join(removed or ["nothing"]),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("paused TTL sweep failed")
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
