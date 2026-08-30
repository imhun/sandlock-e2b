"""Reconcile loop: poll fleet metrics, decide, scale, drain and retire."""

from __future__ import annotations

import asyncio
import logging
import time

from autoscaler.control import ControlPlaneClient
from autoscaler.policy import (
    FleetSnapshot,
    PolicyConfig,
    desired_for_demand,
    parse_snapshot,
    scale_down_candidates,
    scale_up_triggered,
)

logger = logging.getLogger(__name__)


class AutoscalerLoop:
    def __init__(
        self,
        *,
        control: ControlPlaneClient,
        backend,
        policy: PolicyConfig,
        poll_s: float = 5.0,
        clock=time.monotonic,
    ) -> None:
        self._control = control
        self._backend = backend
        self._policy = policy
        self._poll_s = poll_s
        self._clock = clock
        self._last_scale_up: float = float("-inf")
        self._last_scale_down: float = float("-inf")
        self._draining_node_id: str | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("autoscaler tick failed")
            await asyncio.sleep(self._poll_s)

    async def tick(self) -> None:
        payload = self._control.metrics()
        snapshot = parse_snapshot(payload)
        now = self._clock()

        # 1) Finish a pending drain: retire the node once it has no sandboxes.
        if self._draining_node_id is not None:
            node = self._find(snapshot, self._draining_node_id)
            if node is None:
                logger.info("drained node %s is gone", self._draining_node_id)
                self._draining_node_id = None
                self._last_scale_down = now
            elif node.active_sandboxes == 0:
                await self._retire(self._draining_node_id)
                self._draining_node_id = None
                self._last_scale_down = now
            else:
                logger.info(
                    "node %s draining, %s active sandboxes",
                    self._draining_node_id,
                    node.active_sandboxes,
                )
                return

        current = self._backend.current()

        # 2) Scale up on utilization / 503 pressure.
        if (
            scale_up_triggered(snapshot, self._policy)
            and now - self._last_scale_up >= self._policy.scale_up_cooldown_s
        ):
            desired = max(
                current,
                desired_for_demand(snapshot, self._policy),
            )
            if desired > current:
                await asyncio.to_thread(self._backend.scale_to, desired)
                self._last_scale_up = now
                logger.info(
                    "scaled up %s -> %s (util=%.2f, 503=%s)",
                    current,
                    desired,
                    snapshot.peak_fleet_utilization(),
                    snapshot.recent503_count,
                )

        # 3) Scale down: drain one idle node, retire on the next tick.
        elif (
            now - self._last_scale_down >= self._policy.scale_down_cooldown_s
            and current > self._policy.min_replicas
        ):
            candidates = scale_down_candidates(snapshot, self._policy)
            if candidates:
                target = candidates[0]
                result = self._control.drain(target.node_id)
                self._draining_node_id = target.node_id
                if result.get("activeSandboxes", 0) == 0:
                    await self._retire(target.node_id)
                    self._draining_node_id = None
                    self._last_scale_down = now
                else:
                    logger.info(
                        "draining node %s (%s active sandboxes)",
                        target.node_id,
                        result.get("activeSandboxes"),
                    )

    async def _retire(self, node_id: str) -> None:
        await asyncio.to_thread(self._backend.remove_node, node_id)
        logger.info("retired drained node %s", node_id)

    @staticmethod
    def _find(snapshot: FleetSnapshot, node_id: str):
        for node in snapshot.nodes:
            if node.node_id == node_id:
                return node
        return None
