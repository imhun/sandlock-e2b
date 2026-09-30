"""The autoscaler, hosted by the control plane (k8s path, 2026-09-30).

The autoscaler used to be its own Deployment with its own image, polling this
same control plane over ``GET /internal/fleet/metrics`` and asking for a drain
through ``POST /internal/nodes/{id}/drain`` with the internal key. Three things
were paid for that separation: a second image to build, publish and roll in
lockstep with this one (``docs/k8s-deployment.md`` §4.5 lists the credential
windows that had to name it), a fleet view that could only be as fresh as the
last HTTP round trip, and a component whose only input came from the same
process that could have answered it directly.

What the split bought was process isolation, and the fleet made that cheap to
give up: the loop's whole outside world is one HTTP client, and its two actions
are already the control plane's own (scale a workload; mark a node draining).
So the loop runs here, and what the network used to provide is provided by
name below:

* **one actor per interval** -- the loop is single-flight through the shared
  store (:func:`autoscaler_tick_claim`), because this app runs two replicas;
* **memory that outlives a process** -- cooldowns and the in-flight drain live
  in the store, not on the loop object (:mod:`autoscaler.state`), because a
  control-plane rollout restarts the loop;
* **one fleet definition** -- the loop reads
  :func:`control_plane.fleet_view.fleet_metrics_payload` and drains through
  :func:`control_plane.fleet_view.drain_node`, the two functions the HTTP
  handlers call. There is no longer a second endpoint to keep in step.

The Docker-pool backend is gone with the split (the local compose stack no
longer autoscales): the only backend left is Kubernetes, which is also why
this can live inside a pod that holds no Docker socket -- the fleet's
"runtime never needs the daemon" rule (``docs/SCALING.md`` §4) still holds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from autoscaler.backends.k8s import KubernetesBackend
from autoscaler.loop import AutoscalerLoop
from autoscaler.policy import PolicyConfig
from autoscaler.state import LoopState, RedisLoopState
from control_plane.fleet_view import drain_node as _drain_node
from control_plane.fleet_view import fleet_metrics_payload
from control_plane.registry.redis_backend import try_claim

logger = logging.getLogger(__name__)

#: The single-flight key for one decision interval. TTL'd and never released:
#: whoever sets it acts this interval, and a winner that dies mid-tick costs
#: the fleet exactly one interval -- the same protocol as the TTL sweep and the
#: node health sweep (F11 step 4).
TICK_CLAIM_KEY = "e2b:autoscaler:tick"


class UnknownNodeError(RuntimeError):
    """A scale-down named a node the node registry does not have.

    The HTTP surface answers this with a 404; in-process the loop lets it out
    of the tick, which the runner logs and the next interval retries -- a node
    that vanished between the metrics read and the drain is a race, not a
    reason to keep the loop from reconciling the rest of the fleet.
    """


class InProcessFleetControl:
    """The loop's fleet view and drain action, read from *this* app's state.

    The standalone autoscaler saw the fleet through the metrics endpoint and
    drained through the drain endpoint. Both were this control plane talking
    to itself, so the merged shape calls the same two functions the handlers
    call (``control_plane/fleet_view.py``) -- no HTTP, no internal key, no
    source-IP second factor, and no way for the loop to act on a fleet view
    that differs from the one the API serves.
    """

    def __init__(self, state: Any) -> None:
        self._state = state

    def metrics(self) -> dict[str, Any]:
        return fleet_metrics_payload(self._state)

    def drain(self, node_id: str) -> dict[str, Any]:
        result = _drain_node(self._state, node_id)
        if result is None:
            raise UnknownNodeError(f"node {node_id} not found")
        return result


def build_policy(settings: Any) -> PolicyConfig:
    """The decision knobs, spelled once from the control plane's settings.

    Every name is the ``E2B_AS_*`` variable the standalone autoscaler read, so
    the values in ``deploy/k8s/control-plane.yaml`` say the same thing they
    said in ``deploy/k8s/autoscaler.yaml``.
    """
    return PolicyConfig(
        min_replicas=settings.autoscaler_min_replicas,
        max_replicas=settings.autoscaler_max_replicas,
        util_threshold=settings.autoscaler_util_threshold,
        scale_up_cooldown_s=settings.autoscaler_scale_up_cooldown_s,
        scale_down_cooldown_s=settings.autoscaler_scale_down_cooldown_s,
        scale_down_util=settings.autoscaler_scale_down_util,
        node_scale_down_util=settings.autoscaler_node_scale_down_util,
        warmup_buffer=settings.autoscaler_warmup_buffer,
    )


def build_loop(
    app: Any,
    *,
    backend: Any | None = None,
    state_store: LoopState | None = None,
) -> AutoscalerLoop:
    """The loop this app hosts: in-process control, k8s backend, shared marks.

    ``backend`` is injectable for the same reason the C3 agent client is: the
    tests (and any embedder) must be able to pin the loop's only way out of the
    process without a cluster. Production builds the Kubernetes backend from
    ``E2B_AS_K8S_*``; a bad ``kind`` is refused at construction rather than
    404ing on every tick.
    """
    settings = app.state.settings
    if backend is None:
        backend = KubernetesBackend(
            namespace=settings.autoscaler_k8s_namespace,
            deployment=settings.autoscaler_k8s_deployment,
        )
    if state_store is None:
        redis_client = getattr(app.state, "redis_client", None)
        state_store = RedisLoopState(redis_client) if redis_client is not None else None
    return AutoscalerLoop(
        control=InProcessFleetControl(app.state),
        backend=backend,
        policy=build_policy(settings),
        poll_s=settings.autoscaler_poll_s,
        state=state_store,
    )


def autoscaler_tick_claim(redis_client: Any, *, poll_s: float) -> bool:
    """Whether *this* replica owns this decision interval.

    TTL is one interval, so the claim cannot outlive the round it protects and
    needs no release: the next interval is a fresh ``SET NX``. Without a store
    there is one replica, which is the winner by definition.
    """
    return try_claim(redis_client, TICK_CLAIM_KEY, ttl_s=max(1, int(poll_s)))


class AutoscalerService:
    """The hosted loop: one claimed tick per interval, forever.

    Separate from :class:`autoscaler.loop.AutoscalerLoop` on purpose -- the
    loop is the decision (pure, testable with a fake control and a fake
    backend), this is the *scheduling* of that decision inside a process that
    also serves the API. The claim lives here because it is a property of the
    deployment (how many replicas exist), not of the decision.
    """

    def __init__(
        self,
        *,
        loop: AutoscalerLoop,
        poll_s: float,
        claim: Callable[[], bool] | None = None,
    ) -> None:
        self.loop = loop
        self.control = loop.control
        self._poll_s = poll_s
        # No claim (no shared store) means one replica, i.e. always this one.
        self._claim = claim or (lambda: True)

    async def tick_once(self) -> bool:
        """Run one interval, unless a peer replica took it. True when it ran."""
        if not self._claim():
            return False
        await self.loop.tick()
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick_once()
            except Exception:
                # A failed tick must not end the loop: the K8s API, the node
                # registry or a live CLI can all be transiently unavailable,
                # and the fleet's capacity problem outlives one interval.
                logger.exception("autoscaler tick failed")
            await asyncio.sleep(self._poll_s)


def build_service(app: Any, *, backend: Any | None = None) -> AutoscalerService:
    """The service ``create_app`` puts on ``app.state.autoscaler``."""
    settings = app.state.settings
    redis_client = getattr(app.state, "redis_client", None)
    poll_s = settings.autoscaler_poll_s
    return AutoscalerService(
        loop=build_loop(app, backend=backend),
        poll_s=poll_s,
        claim=lambda: autoscaler_tick_claim(redis_client, poll_s=poll_s),
    )
