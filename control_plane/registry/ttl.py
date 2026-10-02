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
        claim_key: str = "e2b:ttl:sweep",
        starve_after_s: float = 30.0,
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
        #: N61: the claim nobody releases has to be *named*, not just quietly
        #: skipped a round at a time -- a sweeper that never wins a round reaps
        #: nothing, and before this said nothing either. The default names the
        #: ordinary TTL sweep's key; a caller with its own names its own.
        self._claim_key = claim_key
        self._starve_after_s = starve_after_s
        self._clock = clock
        self._starved_since: float | None = None
        self._starve_reported = False
        self._task: asyncio.Task | None = None

    def start(self, registry) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(registry))

    def _note_starved(self) -> None:
        """Time a run of lost claims and name it once, after the grace period."""
        now = self._clock()
        if self._starved_since is None:
            self._starved_since = now
            return
        if self._starve_reported:
            return
        held = now - self._starved_since
        if held >= self._starve_after_s:
            self._starve_reported = True
            logger.warning(
                "TTL sweep starved: the fleet-wide claim %s has been held for "
                "%.1fs; no expired record can be reaped while this lasts",
                self._claim_key,
                held,
            )

    def _note_claimed(self) -> None:
        """Clear the starvation clock; say so when the outage was named."""
        if self._starved_since is None:
            return
        held = self._clock() - self._starved_since
        self._starved_since = None
        if self._starve_reported:
            self._starve_reported = False
            logger.info(
                "TTL sweep: the fleet-wide claim %s is free again after "
                "%.1fs; reaping resumes",
                self._claim_key,
                held,
            )

    async def _loop(self, registry) -> None:
        while True:
            try:
                if self._claim is not None and not self._claim():
                    self._note_starved()
                    await asyncio.sleep(self._interval)
                    continue
                self._note_claimed()
                # N61: both of these used to run straight on the loop. The
                # listing is a shared-store scan plus a read per record, and
                # the cleanup is an ``rmtree`` measured at 17.1 s for a single
                # sandbox -- together they froze every other task for the
                # length of a round (the shape N32 measured as a 76 s stall).
                # They leave the loop; the record still goes only after the
                # teardown returned.
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
                            # A callback may be async: the node teardown has to
                            # `await` its HTTP call, or the only way to reach the
                            # worker would be a blocking call on this loop --
                            # which is what N32 measured as a 76 s stall that
                            # looked like two dead nodes.
                            result = self._on_expired(record)
                            if inspect.isawaitable(result):
                                await result
                        except Exception:  # pragma: no cover - defensive
                            logger.exception("TTL cleanup failed for %s", record.sandbox_id)
                    # N53: release the record only *after* the teardown ran.
                    # The worker's teardown removes the tree through a
                    # ``remove-workspace`` file operation, which the control
                    # plane authorizes against these records; releasing first
                    # answered it 404 and left the tree (and the worker's own
                    # runtime record) behind. The window where an expired
                    # record is still visible is the teardown itself.
                    try:
                        registry.delete(record.sandbox_id)
                    except UnknownSandboxError:  # pragma: no cover - already gone
                        pass
                    await asyncio.to_thread(registry.cleanup_workspace, record)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("TTL sweep failed")
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
