"""C3 Task 6: the agent's periodic inventory scan -- the eyes (C3 §11.1 item 5).

The worker's own orphan sweep is off in the agent shape (Task 4's named
warning): reclaiming a tree needed ``chown --worker``, the privilege escalation
§14.3 measured. The replacement keeps the division of labour the plan chose --
**(e) agent 巡检 → CP 决策 → agent 执行**:

* the **agent** is the eyes. It is on the node, it mounts the shared
  workspace, and it can read the whole disk *without* asking a worker; a
  compromised agent lying about what it sees cannot make the control plane
  delete a *recorded* tree, because the records are the control plane's;
* the **control plane** is the brain. It is the only holder of the
  authoritative records, so it is the only component that may decide "no record
  anywhere claims this tree" (``control_plane/self_heal.py``);
* the agent is the executor again -- it is *instructed*, per tree, with the
  path the control plane derived.

What this module deliberately does **not** do:

* it does not name a path, a uid or a verb. The report is
  ``{"sandboxes": [<id>, ...]}`` and nothing else (hard rules 1/3, §14.4);
* it does not decide anything. A tree it reports is only *evidence of what is
  on the disk*; whether it may go is the control plane's answer, and a
  deferred answer means the round changed nothing;
* it holds no authorization table and no TTL: the scan is a directory read of
  its own mount, and the retry schedule below is a timer, not a permission.

The scan itself is ``os.scandir`` over ``<workspace base>`` filtered by the
**shared** predicate every workspace scan in this repo uses
(:func:`gateway_common.paths.is_sandbox_workspace_dir`), so the agent cannot
drift from the worker's (retired) sweep about what a sandbox tree is. Names the
platform owns (``state``, ``_images`` …) are excluded by the platform's own
:func:`is_reserved_platform_namespace`: ``state`` spells a legal sandbox id, and
the control plane refuses it on the way back in as the second layer.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from deploy.c3_agent.config import Settings
from gateway_common.paths import (
    is_reserved_platform_namespace,
    is_sandbox_workspace_dir,
)

logger = logging.getLogger(__name__)


class InventoryReportError(RuntimeError):
    """A named, fail-closed refusal from the agent→control-plane hop.

    Every failure of the hop is one of these -- unreachable, refused, a
    non-JSON answer -- so the round can report *why* it did not converge
    instead of looking like a round that found nothing.
    """


@dataclass(frozen=True)
class ScanSchedule:
    """When the next scan happens (Task 6's trigger/period knobs).

    ``initial_delay_s`` (30) keeps the agent out of the control plane's own
    startup; ``interval_s`` (120) is the steady cadence -- so a worker that
    crashed and never restarts leaves an orphan tree on the disk for **2–3
    minutes** (30 s + 120 s), which is the brief's "N 分钟". A round the control
    plane deferred, or that could not be reported at all, is retried on a
    doubling schedule capped at ``backoff_max_s`` (600), so a persistent
    deferral cannot turn into a fleet-wide poll and cannot go silent either.
    """

    initial_delay_s: float = 30.0
    interval_s: float = 120.0
    backoff_max_s: float = 600.0

    @property
    def first_delay_s(self) -> float:
        # A negative delay is "start now", never "start in the past" (which
        # ``wait_for`` would treat as an immediate timeout anyway).
        return max(0.0, float(self.initial_delay_s))

    def delay_after(self, *, consecutive_deferrals: int) -> float:
        """The delay before the next round, given how many just did not decide."""
        if consecutive_deferrals <= 0:
            return float(self.interval_s)
        return float(
            min(
                self.interval_s * (2 ** consecutive_deferrals),
                self.backoff_max_s,
            )
        )


@dataclass(frozen=True)
class ScanRound:
    """What one scan + report round did, for the log line and for tests."""

    scanned: tuple[str, ...]
    answer: dict[str, Any] | None
    failure: str | None
    next_delay_s: float

    @property
    def deferred(self) -> str | None:
        """The control plane's named reason, when it deferred the sweep."""
        if not self.answer:
            return None
        deferred = self.answer.get("deferred")
        return deferred if isinstance(deferred, str) and deferred else None


class InventoryReporter(Protocol):
    def report(self, sandboxes: list[str]) -> Any:  # pragma: no cover - protocol
        """Send one inventory; return the control plane's answer."""
        ...


class HttpInventoryReporter:
    """The production hop: one POST of the ids the scan saw.

    The body is exactly ``{"sandboxes": [...]}`` -- no path, no uid, no verb,
    no "please delete" (§14.4). Authentication is ``X-Internal-Key`` with the
    agent's own credential, which the control plane accepts **only** on this
    surface (``control_plane/auth.py::verify_agent_key``): the agent token is
    not a general internal key, and the control plane proves the caller's
    network position separately (source-IP second factor, §11.1 item 9's layer
    4).
    """

    def __init__(
        self,
        *,
        url: str,
        node_id: str,
        token: str,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = (url or "").rstrip("/")
        self._node_id = node_id
        self._token = token
        self._timeout_s = float(timeout_s)
        self._transport = transport

    async def report(self, sandboxes: list[str]) -> dict[str, Any]:
        endpoint = f"{self._url}/internal/nodes/{self._node_id}/agent/inventory"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                resp = await client.post(
                    endpoint,
                    json={"sandboxes": list(sandboxes)},
                    headers={"X-Internal-Key": self._token},
                )
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise InventoryReportError(
                f"the control plane at {self._url} is unreachable: {detail}"
            ) from exc
        if resp.status_code >= 300:
            # The control plane's own words, not a paraphrase: a refusal is the
            # only place an operator learns *which* layer refused (401/403/503
            # are three different deployment problems).
            detail: Any = None
            with suppress(ValueError):
                detail = resp.json().get("message")
            detail = detail if isinstance(detail, str) and detail else resp.text
            raise InventoryReportError(
                f"the control plane refused the inventory report "
                f"(status {resp.status_code}): {detail}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise InventoryReportError(
                "the control plane answered the inventory report with a "
                "non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise InventoryReportError(
                "the control plane answered the inventory report with a "
                f"{type(payload).__name__}, not an inventory answer"
            )
        return payload


class InventoryScanner:
    """One periodic task: scan the workspace base, report the ids, log the round.

    Failed and deferred rounds are named and retried on the schedule's
    doubling backoff; a round that got a decision resets the schedule. The
    scanner never touches a tree -- it has no ``rm`` of its own (the agent's
    only removal path is ``e2b-maint``, driven by a control-plane instruction).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        reporter: InventoryReporter,
        schedule: ScanSchedule | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._reporter = reporter
        self._schedule = schedule or ScanSchedule(
            initial_delay_s=settings.scan_initial_delay_s,
            interval_s=settings.scan_interval_s,
            backoff_max_s=settings.scan_backoff_max_s,
        )
        self._sleep = sleep
        self._deferrals = 0

    @property
    def schedule(self) -> ScanSchedule:
        return self._schedule

    def scan_once(self) -> list[str]:
        """The ids of the sandbox-shaped trees under the workspace base."""
        base = Path(self._settings.workspace_base)
        try:
            entries = sorted(base.iterdir(), key=lambda entry: entry.name)
        except FileNotFoundError:
            # An empty answer is the honest one (there is nothing to reclaim),
            # but a deployment that moved the base has to see why its sweep is
            # quiet forever -- so this is a named line, not silence.
            logger.warning(
                "c3-agent inventory: the workspace base %s does not exist: "
                "nothing to report",
                base,
            )
            return []
        except OSError as exc:
            logger.warning(
                "c3-agent inventory: cannot scan %s: %s", base, exc
            )
            return []
        return [
            entry.name
            for entry in entries
            if is_sandbox_workspace_dir(entry)
            and not is_reserved_platform_namespace(entry.name)
        ]

    async def round(self) -> ScanRound:
        """One scan + one report; never raises (the loop must survive)."""
        scanned = self.scan_once()
        try:
            answer = await self._reporter.report(list(scanned))
        except InventoryReportError as exc:
            delay = self._backoff()
            logger.warning(
                "c3-agent inventory: could not report %d tree(s) to the control "
                "plane: %s (retrying in %.0fs)",
                len(scanned),
                exc,
                delay,
            )
            return ScanRound(tuple(scanned), None, str(exc), delay)
        deferred = answer.get("deferred")
        if isinstance(deferred, str) and deferred:
            delay = self._backoff()
            logger.warning(
                "c3-agent inventory: the sweep was deferred by the control "
                "plane (%d tree(s) reported): %s",
                len(scanned),
                deferred,
            )
            return ScanRound(tuple(scanned), answer, None, delay)
        self._deferrals = 0
        logger.info(
            "c3-agent inventory: node=%s scanned=%d protected=%d orphans=%d "
            "removed=%d failed=%d deferred=-",
            self._settings.node_id,
            len(scanned),
            len(answer.get("protected") or []),
            len(answer.get("orphans") or []),
            len(answer.get("removed") or []),
            len(answer.get("failed") or []),
        )
        return ScanRound(
            tuple(scanned),
            answer,
            None,
            self._schedule.delay_after(consecutive_deferrals=0),
        )

    def _backoff(self) -> float:
        self._deferrals += 1
        return self._schedule.delay_after(consecutive_deferrals=self._deferrals)

    async def run(self, stop: asyncio.Event) -> None:
        """The periodic loop: first scan after the initial delay, then per round."""
        delay = self._schedule.first_delay_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            else:
                return
            round_ = await self.round()
            delay = round_.next_delay_s


def scanner_for(settings: Settings) -> InventoryScanner | None:
    """The scanner this container should run, or ``None`` with the reason logged.

    Off means one of two things, and they are not the same fact:

    * this container was not asked to scan (face A does not mount the
      workspaces at all, so it *cannot* -- the manifests enable the scan on
      face B by name);
    * the container was asked to scan but has no control plane to report to,
      which is a deployment error and says so.
    """
    if not settings.scan_enabled:
        logger.info(
            "c3-agent inventory: the scan is not enabled in this container "
            "(E2B_C3_AGENT_SCAN); only the face that mounts the workspaces can "
            "report them",
        )
        return None
    if not settings.control_plane_url:
        logger.warning(
            "c3-agent inventory: E2B_C3_AGENT_SCAN is on but "
            "E2B_CONTROL_PLANE_URL is empty: this container cannot report what "
            "it sees, so the sweep is inert here",
        )
        return None
    if settings.scan_interval_s <= 0:
        # A zero/negative cadence would turn the loop into a poll of the whole
        # fleet (and of the control plane): refused by name, not clamped.
        logger.warning(
            "c3-agent inventory: E2B_C3_AGENT_SCAN_INTERVAL_S must be positive "
            "(got %s): the sweep is inert here",
            settings.scan_interval_s,
        )
        return None
    return InventoryScanner(
        settings=settings,
        reporter=HttpInventoryReporter(
            url=settings.control_plane_url,
            node_id=settings.node_id,
            token=settings.token,
            timeout_s=settings.report_timeout_s,
        ),
    )
