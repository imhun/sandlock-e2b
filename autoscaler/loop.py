"""Reconcile loop: read fleet metrics, decide, scale, drain and retire.

Since 2026-09-30 this loop is a task *of the control plane* on the k8s path
(``control_plane/autoscaler_service.py``), not a Deployment of its own, so the
same code now runs once per control-plane replica. Two things follow, and both
are handled outside the decision logic:

* who acts in an interval is settled by a shared claim (the control plane's
  ``try_claim``), never by "I am the autoscaler pod";
* what the fleet has already done -- cooldowns, the in-flight drain -- is
  settled by :mod:`autoscaler.state`, read and written around each tick, so a
  replica that has never ticked still honours its peer's cooldown and a
  restarted loop still finishes the drain it started.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Protocol

from autoscaler.policy import (
    FleetSnapshot,
    PolicyConfig,
    desired_for_demand,
    parse_snapshot,
    scale_down_candidates,
    scale_up_triggered,
)
from autoscaler.state import InMemoryLoopState, LoopMarks, LoopState

logger = logging.getLogger(__name__)


class FleetControl(Protocol):
    """The loop's view of the fleet -- its only input, and its only action.

    The standalone autoscaler satisfied this with HTTP calls to the control
    plane; the merged one satisfies it with the control plane's own functions
    (:class:`control_plane.autoscaler_service.InProcessFleetControl`). Two
    methods, because the loop reads one view and asks for one thing.
    """

    def metrics(self) -> dict[str, Any]: ...

    def drain(self, node_id: str) -> dict[str, Any]: ...


class AutoscalerLoop:
    def __init__(
        self,
        *,
        control: FleetControl,
        backend,
        policy: PolicyConfig,
        poll_s: float = 5.0,
        # Epoch seconds, not ``time.monotonic``: the cooldowns are compared
        # against marks another process wrote (``autoscaler.state``), and a
        # monotonic clock has no meaning outside the process that read it.
        clock=time.time,
        state: LoopState | None = None,
    ) -> None:
        self._control = control
        self._backend = backend
        self._policy = policy
        self._poll_s = poll_s
        self._clock = clock
        self._state: LoopState = state or InMemoryLoopState()
        #: Draining nodes we could not retire, already reported once (an
        #: operator's drain on a node this workload cannot shrink from would
        #: otherwise print once per interval, forever).
        self._held_reported: set[str] = set()

    @property
    def control(self) -> FleetControl:
        """The fleet view and drain action this loop acts on (its only input)."""
        return self._control

    async def tick(self) -> None:
        """One reconcile round, with the marks read and written around it.

        The write is skipped when the tick changed nothing (the common case on
        an idle fleet), so a no-op round costs no store round trip.
        """
        marks = self._state.read()
        before = replace(marks)
        try:
            await self._reconcile(marks)
        finally:
            if marks != before:
                # The difference, not the snapshot: a peer replica may have
                # written its own marks while this tick was working (see
                # `autoscaler/state.py`).
                self._state.write(before, marks)

    async def _reconcile(self, marks: LoopMarks) -> None:
        payload = self._control.metrics()
        snapshot = parse_snapshot(payload)
        now = self._clock()

        # 1) Finish a pending drain: retire the node once it has no sandboxes.
        if marks.draining_node_id is not None:
            node = self._find(snapshot, marks.draining_node_id)
            if node is None:
                logger.info("drained node %s is gone", marks.draining_node_id)
                marks.draining_node_id = None
                marks.last_scale_down = now
            elif node.active_sandboxes == 0:
                await self._retire(marks.draining_node_id)
                marks.draining_node_id = None
                marks.last_scale_down = now
            else:
                logger.info(
                    "node %s draining, %s active sandboxes",
                    marks.draining_node_id,
                    node.active_sandboxes,
                )
                return

        current = self._backend.current()

        # 2) Enforce the warm-pool floor (min_replicas) regardless of load.
        floor = desired_for_demand(snapshot, self._policy)
        if current < floor:
            await asyncio.to_thread(self._backend.scale_to, floor)
            marks.last_scale_up = now
            logger.info("scaled up to warm-pool floor %s -> %s", current, floor)

        # 3) Reconcile drains this loop did not start: since the marks are
        #    shared (autoscaler/state.py), a drain of ours survives a restart;
        #    what lands here is a node an operator drained through the internal
        #    API, or one whose mark was lost. Same two questions as step 5 --
        #    can the fleet afford it, and is this node the one the backend can
        #    actually retire -- because retiring the wrong node takes a pod the
        #    loop did not choose (N51).
        for node in snapshot.nodes:
            if (
                node.draining
                and node.active_sandboxes == 0
                and node.status == "healthy"
                and node.node_id != marks.draining_node_id
            ):
                if current <= self._policy.min_replicas:
                    continue
                if not await asyncio.to_thread(self._backend.has_node, node.node_id):
                    continue
                victim = await asyncio.to_thread(
                    self._backend.retire_victim, [node.node_id]
                )
                if victim is None:
                    self._report_held(node.node_id)
                    continue
                self._held_reported.discard(node.node_id)
                logger.info("reconciling orphaned drained node %s", node.node_id)
                await self._retire(node.node_id)
                marks.last_scale_down = now

        current = self._backend.current()

        # 4) Scale up further on utilization / 503 pressure.
        if (
            scale_up_triggered(snapshot, self._policy)
            and now - marks.last_scale_up >= self._policy.scale_up_cooldown_s
        ):
            desired = max(
                current,
                desired_for_demand(snapshot, self._policy),
            )
            if desired > current:
                await asyncio.to_thread(self._backend.scale_to, desired)
                marks.last_scale_up = now
                logger.info(
                    "scaled up %s -> %s (util=%.2f, 503=%s)",
                    current,
                    desired,
                    snapshot.peak_fleet_utilization(),
                    snapshot.recent503_count,
                )

        # 5) Scale down: drain one idle node, retire on the next tick. This
        #    is an independent branch: a stale recent503 latch (5-minute
        #    window) must not block scale-down once demand has cleared. The
        #    scale-up cooldown guard prevents same-tick scale-up + scale-down.
        if (
            now - marks.last_scale_down >= self._policy.scale_down_cooldown_s
            and now - marks.last_scale_up >= self._policy.scale_up_cooldown_s
            and current > self._policy.min_replicas
        ):
            candidates = scale_down_candidates(snapshot, self._policy)
            if candidates:
                # Ask the backend which of them it can actually retire: with a
                # StatefulSet the answer is "the highest ordinal, or nobody"
                # (N51). None = shrinking now would take a pod the loop did not
                # choose, so this interval does nothing.
                victim = await asyncio.to_thread(
                    self._backend.retire_victim,
                    [candidate.node_id for candidate in candidates],
                )
                if victim is None:
                    self._report_held(candidates[0].node_id)
                else:
                    self._held_reported.discard(victim)
                    target = self._find(snapshot, victim)
                    result = self._control.drain(target.node_id)
                    marks.draining_node_id = target.node_id
                    if result.get("activeSandboxes", 0) == 0:
                        await self._retire(target.node_id)
                        marks.draining_node_id = None
                        marks.last_scale_down = now
                    else:
                        logger.info(
                            "draining node %s (%s active sandboxes)",
                            target.node_id,
                            result.get("activeSandboxes"),
                        )

    async def _retire(self, node_id: str) -> None:
        await asyncio.to_thread(self._backend.remove_node, node_id)
        logger.info("retired drained node %s", node_id)

    def _report_held(self, node_id: str) -> None:
        """Say once that a draining node cannot be retired right now (N51)."""
        if node_id in self._held_reported:
            return
        self._held_reported.add(node_id)
        logger.warning(
            "scale-down held: this workload would delete a different pod than "
            "the idle node %s (it shrinks from the top), so the fleet waits "
            "instead of taking a worker the loop did not choose",
            node_id,
        )

    @staticmethod
    def _find(snapshot: FleetSnapshot, node_id: str):
        for node in snapshot.nodes:
            if node.node_id == node_id:
                return node
        return None
