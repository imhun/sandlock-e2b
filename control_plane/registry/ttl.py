"""TTL sweeper that reaps expired sandboxes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Callable

from control_plane.registry.manager import UnknownSandboxError

logger = logging.getLogger(__name__)

#: N61: how many candidate ids a round's INFO line names before it truncates.
_MAX_LOGGED_CANDIDATES = 10

#: N61 裁定 B: the periodic "is this replica actually sweeping?" line comes
#: every ~30 rounds or ~30 s, whichever is first. At the 1 s cadence those are
#: the same order; what the line buys is that the question can be answered
#: without a line per round.
_SUMMARY_EVERY_ROUNDS = 30
_SUMMARY_EVERY_S = 30.0

#: N61 裁定 B: the claim's TTL is the cadence on purpose (裁定 A: ``try_claim``
#: has no release path, so a longer TTL slows the whole fleet down), which means
#: a round this many times longer than the cadence ran with an expired claim --
#: a peer may have started its own round meanwhile. That is the suspect shape
#: behind N61 and it now names itself.
_OVERRUN_FACTOR = 3.0


def _candidate_ids(records) -> str:
    """The candidate ids, first ten then ``…(+N more)`` (N61)."""
    ids = [record.sandbox_id for record in records]
    if len(ids) > _MAX_LOGGED_CANDIDATES:
        tail = len(ids) - _MAX_LOGGED_CANDIDATES
        return ", ".join(ids[:_MAX_LOGGED_CANDIDATES]) + f"…(+{tail} more)"
    return ", ".join(ids)


class TTLSweeper:
    """Periodically removes expired sandboxes and releases their quotas."""

    def __init__(
        self,
        *,
        interval_seconds: float = 1.0,
        on_expired: Callable[[object], None] | None = None,
        claim: Callable[[], bool] | None = None,
        overrun_after_s: float | None = None,
        summary_every_rounds: int = _SUMMARY_EVERY_ROUNDS,
        summary_every_s: float = _SUMMARY_EVERY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval_seconds
        self._on_expired = on_expired
        #: F11 step 4: a round is single-flight across replicas. ``remove_expired``
        #: is driven by wall-clock deadlines on *shared* records, so two replicas
        #: sweeping the same window expire the same sandboxes -- duplicated work,
        #: duplicated teardown calls and a duplicated ``TTL expired`` line. The
        #: claim is a TTL'd key; ``None`` means "one process, no need to ask".
        self._claim = claim
        #: N61 裁定 B: two read-only signals, neither of which changes what a
        #: round does. Every round that outlives ``overrun_after_s`` (3 x the
        #: cadence by default) ran past its own claim and says so, with how
        #: long it took and how many records it reaped; and every ~30 rounds /
        #: ~30 s this replica reports how many of those rounds it actually ran,
        #: how long the last one it ran took (``0.00s`` until it has run one),
        #: and how many records it reaped in the window. Losing a round to the
        #: *peer's* claim is normal (``SET NX`` with no release), so counting
        #: lost rounds as "starved" would have cried wolf on a healthy pair.
        self._overrun_after_s = (
            overrun_after_s
            if overrun_after_s is not None
            else _OVERRUN_FACTOR * interval_seconds
        )
        self._summary_every_rounds = summary_every_rounds
        self._summary_every_s = summary_every_s
        self._clock = clock
        self._attempts = 0
        self._wins = 0
        self._reaped = 0
        self._last_round_s = 0.0
        self._summary_since: float | None = None
        self._task: asyncio.Task | None = None

    def start(self, registry) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(registry))

    def _note_round(
        self, *, won: bool, seconds: float, reaped: int, now: float
    ) -> None:
        """Report one round's shape -- and nothing about what it does next."""
        self._attempts += 1
        if won:
            self._wins += 1
            self._reaped += reaped
            self._last_round_s = seconds
            if seconds > self._overrun_after_s:
                # N61 minor: the factor comes from the threshold itself, so a
                # retuned ``overrun_after_s`` cannot make this sentence lie
                # (there used to be a second truth source: a hardcoded "3 x").
                # ``>`` matches the strict trigger above, not ``>=``.
                factor = (
                    self._overrun_after_s / self._interval
                    if self._interval
                    else 0.0
                )
                logger.warning(
                    "TTL sweep: a round took %.2fs (> %.1fx the %.1fs cadence) "
                    "and reaped %d record(s); the fleet-wide claim expired "
                    "while it ran, so a peer may have started its own round "
                    "too -- this round's teardown is not lost",
                    seconds,
                    factor,
                    self._interval,
                    reaped,
                )
        if self._summary_since is None:
            self._summary_since = now
        if (
            self._attempts >= self._summary_every_rounds
            or now - self._summary_since >= self._summary_every_s
        ):
            logger.info(
                "TTL sweep: this replica ran %d of the last %d rounds; last "
                "round took %.2fs; %d candidate(s) reaped",
                self._wins,
                self._attempts,
                self._last_round_s,
                self._reaped,
            )
            self._attempts = 0
            self._wins = 0
            self._reaped = 0
            self._summary_since = now

    async def _loop(self, registry) -> None:
        while True:
            started = self._clock_safely()
            won = False
            reaped = 0
            try:
                # N61 裁定 B: a lost round is still a *round* -- it is counted,
                # not skipped, so the summary can say how many of the last N
                # this replica ran. Losing one to the peer's claim is normal.
                won = self._claim is None or self._claim()
                if won:
                    # N61: both of these used to run straight on the loop. The
                    # listing is a shared-store scan plus a read per record,
                    # and the cleanup is an ``rmtree`` measured at 17.1 s for a
                    # single sandbox -- together they froze every other task
                    # for the length of a round (the shape N32 measured as a
                    # 76 s stall). They leave the loop; the record still goes
                    # only after the teardown returned.
                    expired = await asyncio.to_thread(registry.expired_candidates)
                    if expired:
                        logger.info(
                            "TTL sweep: %d expired candidate(s): %s",
                            len(expired),
                            _candidate_ids(expired),
                        )
                    for record in expired:
                        logger.info("TTL expired sandbox %s", record.sandbox_id)
                        if self._on_expired is not None:
                            try:
                                # A callback may be async: the node teardown has
                                # to `await` its HTTP call, or the only way to
                                # reach the worker would be a blocking call on
                                # this loop -- which is what N32 measured as a
                                # 76 s stall that looked like two dead nodes.
                                result = self._on_expired(record)
                                if inspect.isawaitable(result):
                                    await result
                            except Exception:  # pragma: no cover - defensive
                                logger.exception(
                                    "TTL cleanup failed for %s", record.sandbox_id
                                )
                        # N53: release the record only *after* the teardown ran.
                        # The worker's teardown removes the tree through a
                        # ``remove-workspace`` file operation, which the control
                        # plane authorizes against these records; releasing
                        # first answered it 404 and left the tree (and the
                        # worker's own runtime record) behind. The window where
                        # an expired record is still visible is the teardown
                        # itself.
                        try:
                            registry.delete(record.sandbox_id)
                        except UnknownSandboxError:  # pragma: no cover - gone
                            pass
                        await asyncio.to_thread(registry.cleanup_workspace, record)
                        reaped += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("TTL sweep failed")
            if started is not None:
                finished = self._clock_safely()
                if finished is not None:
                    self._note_round_safely(
                        won=won,
                        seconds=finished - started,
                        reaped=reaped,
                        now=finished,
                    )
            await asyncio.sleep(self._interval)

    def _clock_safely(self) -> float | None:
        """``self._clock()`` guarded: the *metric* must not kill the sweep.

        The clock used to be read outside the round's ``try``. A clock that
        raises -- an injected one, or (once a log sink is a metric sink) a
        pathological one -- escaped ``_loop``, so the task died and *nothing*
        was reaped from then on, silently. A failed read is one named WARNING
        and one skipped note; the reaping continues.
        """
        try:
            return self._clock()
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "TTL sweep: the round's metric failed (_clock raised); the "
                "sweep continues"
            )
            return None

    def _note_round_safely(
        self, *, won: bool, seconds: float, reaped: int, now: float
    ) -> None:
        """``_note_round`` guarded: a broken log sink is not a broken sweep."""
        try:
            self._note_round(won=won, seconds=seconds, reaped=reaped, now=now)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "TTL sweep: the round's metric failed (_note_round raised); "
                "the sweep continues"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
