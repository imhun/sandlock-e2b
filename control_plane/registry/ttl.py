"""TTL sweeper that reaps expired sandboxes."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class TTLSweeper:
    """Periodically removes expired sandboxes and releases their quotas."""

    def __init__(
        self,
        *,
        interval_seconds: float = 1.0,
        on_expired: Callable[[object], None] | None = None,
        claim: Callable[[], bool] | None = None,
    ) -> None:
        self._interval = interval_seconds
        self._on_expired = on_expired
        #: F11 step 4: a round is single-flight across replicas. ``remove_expired``
        #: is driven by wall-clock deadlines on *shared* records, so two replicas
        #: sweeping the same window expire the same sandboxes -- duplicated work,
        #: duplicated teardown calls and a duplicated ``TTL expired`` line. The
        #: claim is a TTL'd key; ``None`` means "one process, no need to ask".
        self._claim = claim
        self._task: asyncio.Task | None = None

    def start(self, registry) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(registry))

    async def _loop(self, registry) -> None:
        while True:
            try:
                if self._claim is not None and not self._claim():
                    await asyncio.sleep(self._interval)
                    continue
                expired = registry.remove_expired()
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
                    registry.cleanup_workspace(record)
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
