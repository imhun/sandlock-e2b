"""E7: the platform-account alert (``used``/``budget`` per node).

The platform account (``E2B_PLATFORM_DISK_MB`` on the worker, "the whole
``_runtime``", checkpoint images included) is a **soft** ledger: the size of an
image is only known after it is written, and every worker decides against the
copy of the account it can see, so concurrent captures may overshoot for a
moment. The decision behind that shape was "accept soft, and expose the
numbers" (``docs/checkpoint-restore-e2b-half.md`` §6(f)), and the second half of
it landed -- every worker heartbeat publishes ``platformDiskUsedMB`` /
``platformDiskBudgetMB`` onto the node view.

What was missing is the *consumer*: a node whose account had filled up was only
noticed when a capture was refused. This module is that consumer, and it is a
scan rather than a metric because the fleet has no metrics pipeline
(``rg 'alert|PrometheusRule' deploy/`` -- none): one WARNING per crossing, read
straight off the node view every deployment already has.

**Spam policy: enter/exit, one line each.** A node above the ratio gets exactly
one WARNING when it crosses, silence while it stays there, and one INFO when it
comes back under; crossing again warns again. The alternatives -- a line every
scan, or every N minutes while over -- were rejected because "still over" is
not new information and the fleet's next action (a refused capture, a paused
TTL reap) surfaces on its own. The crossing is the event.

**Single flight, same protocol as the other sweeps** (``F11`` step 4): the node
view is shared, so every replica computes the same crossings and would log the
same lines. ``try_claim`` on a TTL'd key means one replica alerts per interval.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: ``E2B_PLATFORM_LEDGER_ALERT_RATIO``: warn when a node's platform account is
#: at or above this fraction of its budget. ``0`` disables alerting; the
#: default (``0.8``) warns while there is still room to react -- a fifth of the
#: account left is roughly one more sandbox-sized image on the production
#: shape (512 MiB sandboxes, a few hundred MiB per image).
LEDGER_ALERT_RATIO_ENV = "E2B_PLATFORM_LEDGER_ALERT_RATIO"
DEFAULT_LEDGER_ALERT_RATIO = 0.8


def ledger_alert_ratio(settings: Any = None) -> float:
    """The configured platform-account alert ratio; ``0`` = disabled."""
    configured = getattr(settings, "ledger_alert_ratio", None)
    if configured is not None:
        return max(0.0, float(configured))
    raw = os.getenv(LEDGER_ALERT_RATIO_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_LEDGER_ALERT_RATIO
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using %s",
            LEDGER_ALERT_RATIO_ENV,
            raw,
            DEFAULT_LEDGER_ALERT_RATIO,
        )
        return DEFAULT_LEDGER_ALERT_RATIO


def ledger_ratio(record) -> float | None:
    """``used / budget`` for one node, or ``None`` when it has no budget.

    A budget of ``0`` is "unlimited" throughout this repo (the worker's own
    ``E2B_PLATFORM_DISK_MB`` and the manager's pools all read it that way), so
    a node with no budget is never over one -- and must never be alerted on.
    """
    budget = int(getattr(record, "platform_disk_budget_mb", 0) or 0)
    if budget <= 0:
        return None
    used = int(getattr(record, "platform_disk_used_mb", 0) or 0)
    return used / budget


class PlatformLedgerAlerter:
    """Warns once when a node's platform account crosses the threshold."""

    def __init__(
        self,
        *,
        threshold_ratio: float,
        interval_seconds: float = 30.0,
        claim: Callable[[], bool] | None = None,
    ) -> None:
        self._threshold = float(threshold_ratio)
        self._interval = interval_seconds
        self._claim = claim
        #: The nodes currently above the ratio. This set *is* the hysteresis:
        #: a node is logged when it enters and when it leaves, never in
        #: between.
        self._over: set[str] = set()
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        """Whether alerting is on (``ratio > 0``)."""
        return self._threshold > 0

    def scan(self, nodes) -> None:
        """One round: read the node view and log the crossings."""
        lister = getattr(nodes, "list", None)
        if not callable(lister):  # a registry with no view to read
            return
        # One read per round: the view is what every other reader places work
        # against, and re-listing to format a line would double the cost and
        # could report numbers from a different instant than the ratio did.
        current: dict[str, Any] = {
            record.node_id: record for record in lister()
        }
        over: dict[str, float] = {}
        for node_id, record in current.items():
            ratio = ledger_ratio(record)
            if ratio is not None and ratio >= self._threshold:
                over[node_id] = ratio
        for node_id in sorted(over.keys() - self._over):
            record = current[node_id]
            logger.warning(
                "platform ledger over budget: node %s used %d MiB of %d MiB "
                "(ratio %.2f >= %.2f)",
                node_id,
                int(getattr(record, "platform_disk_used_mb", 0) or 0),
                int(getattr(record, "platform_disk_budget_mb", 0) or 0),
                over[node_id],
                self._threshold,
            )
        for node_id in sorted(self._over - set(over)):
            record = current.get(node_id)
            # A node that left the view entirely (worker retired, view entry
            # expired) is not a recovery: nothing is under budget, it is
            # simply not there. Only a node we can still read gets a line.
            if record is None:
                continue
            ratio = ledger_ratio(record)
            if ratio is None:
                # The budget itself went away (``0`` = unlimited now), so
                # there is no "under" to report -- the account stopped being
                # measured.
                logger.info(
                    "platform ledger is no longer budgeted: node %s had used "
                    "%d MiB",
                    node_id,
                    int(getattr(record, "platform_disk_used_mb", 0) or 0),
                )
                continue
            logger.info(
                "platform ledger back under budget: node %s used %d MiB of %d "
                "MiB (ratio %.2f < %.2f)",
                node_id,
                int(getattr(record, "platform_disk_used_mb", 0) or 0),
                int(getattr(record, "platform_disk_budget_mb", 0) or 0),
                ratio,
                self._threshold,
            )
        self._over = set(over)

    def start(self, nodes) -> None:
        if not self.enabled:
            logger.info(
                "platform ledger alerting is off (%s=0)",
                LEDGER_ALERT_RATIO_ENV,
            )
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(nodes))

    async def _loop(self, nodes) -> None:
        while True:
            try:
                if self._claim is not None and not self._claim():
                    await asyncio.sleep(self._interval)
                    continue
                self.scan(nodes)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("platform ledger alert scan failed")
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
