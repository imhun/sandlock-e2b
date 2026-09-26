"""Control plane FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from control_plane.api.errors import OfficialError, official_error_handler
from control_plane.api.internal import router as internal_router
from control_plane.api.nodes import router as nodes_router
from control_plane.api.sandboxes import router as sandboxes_router
from control_plane.api.secrets import router as secrets_router
from control_plane.api.snapshots import (
    reconcile_pending_snapshots,
    router as snapshots_router,
)
from control_plane.api.templates import router as templates_router
from control_plane.api.volumes import router as volumes_router
from control_plane.config import Settings, local_node_quota_via_agent
from control_plane.metrics import SlidingWindowCounter
from control_plane.queue import CreateQueue
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.secrets import SecretRegistry
from control_plane.registry.snapshots import SnapshotRegistry
from control_plane.registry.templates import TemplateRegistry
from control_plane.ratelimit import SlidingWindowRateLimiter
from control_plane.registry.ttl import TTLSweeper
from control_plane.registry.redis_backend import try_claim

#: The TTL sweeper's cadence, and therefore the width of its single-flight
#: claim (F11 step 4): one replica per interval, no lock to release.
_TTL_SWEEP_INTERVAL_S = 1.0
from control_plane.registry.volumes import VolumeRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only (envd may be absent)
    from envd_service.quota_agent import QuotaAgentClient


class _NoopRuntimeRegistry:
    """Empty runtime registry for the separated control-plane deployment.

    The envd service (and its runtime registry) only exists on worker hosts;
    the control plane never provisions local sandboxes when
    ``E2B_ENABLE_LOCAL_NODE=false``, so every registry call is a safe no-op.
    """

    def register(self, **kwargs):
        return None

    def get(self, sandbox_id):
        return None

    def unregister(self, sandbox_id) -> None:
        pass

    def set_state(self, sandbox_id, state) -> None:
        pass

    def freeze(self, sandbox_id) -> None:
        pass

    def thaw(self, sandbox_id) -> None:
        pass


def _wire_local_node_quota_agent(
    settings: Settings,
) -> "QuotaAgentClient | None":
    """Wire the quota-agent hooks for a combined ("合体") node.

    ``control_plane.combined_main`` runs the control plane and the envd
    gateway in one process, but it never builds the envd worker app
    (``envd_service.app.create_app``) -- which is where the agent hooks
    (``xfs_quota.agent_query`` / ``agent_ops``) are normally wired. The
    control plane's own volume quota reads the same switch
    (:func:`control_plane.config.local_node_quota_via_agent`), so the hooks
    have to exist here too; without them a deployment that *did* configure
    ``E2B_QUOTA_AGENT_URL`` would still be told "quota-agent not configured"
    and silently keep the degraded (no hard limit) shape.

    The three agent coordinates are read with the envd service's own names and
    defaults (pinned by ``tests/unit/test_controlplane_local_node_quota.py``),
    *not* by building ``envd_service.config.Settings``: the combined control
    plane has no business validating the rest of the worker's configuration
    (port mappings, template images, ...), and a malformed unrelated value
    must not take it down at startup.

    Returns the client to close on shutdown, or ``None`` when the agent form
    is off. A separated control plane (``E2B_ENABLE_LOCAL_NODE=false``)
    provisions no quota in-process and wires nothing, exactly like it does not
    need the envd runtime registry.
    """
    if not settings.enable_local_node:
        return None
    try:
        from envd_service.quota_agent import configure_quota_agent_client
    except ImportError:  # pragma: no cover - separated control-plane image
        return None
    if not local_node_quota_via_agent():
        return None
    from gateway_common.env import env_float

    return configure_quota_agent_client(
        url=(os.getenv("E2B_QUOTA_AGENT_URL") or "").strip() or None,
        token=os.getenv("E2B_QUOTA_AGENT_TOKEN") or None,
        timeout_s=env_float("E2B_QUOTA_AGENT_TIMEOUT_S", 5.0),
    )


async def _node_health_loop(
    app: FastAPI,
    registry,
    *,
    window_s: float,
    interval_s: float = 1.0,
    clock=time.monotonic,
) -> None:
    """Mark sandboxes on lost remote nodes orphaned (E6.1).

    The evidence is "this node's last heartbeat is older than the window", and
    that evidence is only valid while *this loop* is running on schedule. After
    a control-plane stall -- a blocking call in a handler, a long pause -- the
    heartbeats that arrived during the stall have not been handled yet, so
    their timestamps are still the old ones and *every* node looks lost at
    once. The verdict is destructive (live sandboxes are torn down as orphans
    and every later request for them answers 409), so a round that finds
    itself ``window_s`` behind skips it and lets the next one decide: by then
    the queued heartbeats have been handled and a node that really is gone is
    still stale. Measured before this guard (2026-09-22, k0s cluster): one
    2000-file snapshot blocked the loop for 76.1 s behind a synchronous
    ``httpx.post``, the two workers' heartbeats gapped 77.2 s / 78.3 s in the
    access log, and the first sweep after the loop resumed answered
    ``node health sweep: orphaned sandboxes on e2b-worker-1``.
    """
    log = logging.getLogger(__name__)
    previous = clock()
    while True:
        now = clock()
        behind = now - previous
        previous = now
        if behind > window_s:
            log.warning(
                "node health sweep: skipping this round -- the control plane "
                "itself was %.1fs behind (>= the %.0fs heartbeat window), so a "
                "stale heartbeat is not yet evidence that a node is gone",
                behind,
                window_s,
            )
            await asyncio.sleep(interval_s)
            continue
        try:
            # F11 step 2: one replica sweeps per round. The view is shared, so
            # every replica computes the same health verdict and the same
            # orphan set -- a second sweep in the same window is duplicated
            # work (and a duplicated warning), not extra coverage. A round
            # whose claim is lost simply does nothing; the next one is a
            # second later.
            if app.state.nodes.try_acquire_sweep(ttl_s=interval_s):
                marked = await asyncio.to_thread(
                    app.state.nodes.reap_unhealthy, registry
                )
            else:
                marked = []
            if marked:
                log.warning(
                    "node health sweep: orphaned sandboxes on %s",
                    ", ".join(marked),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("node health sweep failed")
        await asyncio.sleep(interval_s)


def create_app(
    *,
    settings: Settings | None = None,
    registry: SandboxRegistry | None = None,
    runtime_registry=None,
    workspace_base=None,
    volumes_registry=None,
    secrets_registry=None,
    snapshots_registry=None,
    nodes_registry=None,
    templates_registry=None,
) -> FastAPI:
    settings = settings or Settings()
    redis_client = None
    quota_agent_client = _wire_local_node_quota_agent(settings)
    if settings.redis_url:
        from control_plane.registry.redis_backend import create_redis_client

        redis_client = create_redis_client(settings.redis_url)
    registry = registry or SandboxRegistry(settings, redis_client=redis_client)
    # E9.4: capacity-release queue for creates that survive an eviction round
    # (E9.3) but still have no room. The registry fires the callback on every
    # real quota release (pause / delete / expiry); CreateQueue turns it into
    # event-loop wakeups for waiting POST /sandboxes requests.
    create_queue = CreateQueue(
        timeout_s=settings.create_queue_timeout_s,
        max_waiters=settings.create_queue_max,
    )
    registry.add_on_quota_released(create_queue.notify_capacity)

    def _release_node_quota(record) -> None:
        if record.quota_released:
            # E9.2: a paused sandbox already handed its node reservation back;
            # releasing it again on delete would steal quota from the
            # sandboxes that still hold it.
            return
        app.state.nodes.release_quota(
            record.node_id or "local",
            memory_mb=record.memory_mb,
            cpu_percent=record.cpu_count * 100,
            disk_mb=record.disk_size_mb,
            processes=record.max_processes,
        )

    registry.add_on_removed(_release_node_quota)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def _on_sandbox_removed(record):
            node = app.state.nodes.get(record.node_id or "local")
            if node is not None and node.address != "local://":
                import httpx

                try:
                    # Async, and awaited by the TTL sweeper: a synchronous call
                    # here blocked the event loop for up to its 30 s timeout --
                    # the same class of stall the node-health sweep now refuses
                    # to read as a dead node (N32).
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.delete(
                            f"{node.address}/agent/sandboxes/{record.sandbox_id}",
                            headers={
                                "X-Internal-Key": settings.internal_api_key
                            },
                        )
                except httpx.HTTPError as exc:
                    # TTL expiry has no HTTP response to answer with, so the
                    # only honest signal is this line (review W7 / C1-1): the
                    # node could not be reached, so the tree stays on the
                    # worker and its next reconcile reclaims it (no
                    # control-plane record references it any more).
                    logging.getLogger(__name__).warning(
                        "TTL: %s was not torn down on %s: %s; its tree is left "
                        "to that worker's next reconcile",
                        record.sandbox_id,
                        node.node_id,
                        exc,
                    )
                else:
                    if resp.status_code != 204:
                        # The node answered and refused (a rewritten
                        # ``sandbox.json`` is the shape): nothing was torn
                        # down, and the tree is an orphan now that the record
                        # is gone, so the orphan-tree GC is the retry.
                        logging.getLogger(__name__).warning(
                            "TTL: %s was refused by %s (HTTP %s): %s; its tree "
                            "is left to the orphan-tree GC",
                            record.sandbox_id,
                            node.node_id,
                            resp.status_code,
                            (resp.text or "")[:300],
                        )
                app.state.runtime_registry.unregister(record.sandbox_id)
            else:
                # Local runtime: full teardown (unregister + workspace and
                # per-sandbox volume slice cleanup, E2.5).
                from control_plane.api.sandboxes import _destroy_local

                if not _destroy_local(app.state, record).acknowledged:
                    logging.getLogger(__name__).warning(
                        "TTL: the local teardown of %s was refused; its "
                        "runtime was stopped and its tree is left to the "
                        "orphan-tree GC",
                        record.sandbox_id,
                    )

        sweeper = TTLSweeper(
            on_expired=_on_sandbox_removed,
            interval_seconds=_TTL_SWEEP_INTERVAL_S,
            # F11 step 4: one replica per round. The deadlines live on shared
            # records, so every replica would otherwise expire the same
            # sandboxes (and call every teardown twice).
            claim=lambda: try_claim(
                redis_client, "e2b:ttl:sweep", ttl_s=int(_TTL_SWEEP_INTERVAL_S)
            ),
        )
        app.state.sweeper = sweeper
        sweeper.start(registry)

        health_task = asyncio.create_task(
            _node_health_loop(
                app, registry, window_s=settings.node_heartbeat_timeout_s
            )
        )
        # N29 (async shape): a reserved copy is an in-process task, so a
        # restart leaves its record at `creating` and a poller waiting on a
        # copy nobody runs. One pass settles them -- resumed from the worker's
        # idempotent answer when the payload did finish, failed otherwise --
        # and skips the ones whose shared claim a *live* peer still holds
        # (F11 step 3: those are that replica's copy, not an orphan).
        startup_snapshot_task = asyncio.create_task(
            reconcile_pending_snapshots(app)
        )
        app.state.node_health_task = health_task
        try:
            yield
        finally:
            # The startup pass is short, but it must not outlive the app: an
            # interrupted resume would leave a record half-updated.
            if not startup_snapshot_task.done():
                try:
                    await asyncio.wait_for(startup_snapshot_task, timeout=30)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    startup_snapshot_task.cancel()
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass
        await sweeper.stop()
        if quota_agent_client is not None:
            quota_agent_client.close()

    app = FastAPI(title="E2B Sandlock Gateway - Control Plane", lifespan=lifespan)
    app.state.settings = settings
    app.state.redis_client = redis_client
    app.state.quota_agent_client = quota_agent_client
    app.state.registry = registry
    app.state.create_queue = create_queue

    # Health endpoints for load balancer / monitoring. These are registered
    # before the gateway mount (combined_main), so they win over the gateway
    # catch-all; the sandbox-scoped /health with E2b-Sandbox-Id still routes
    # through the gateway proxy untouched.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root_health() -> dict[str, str]:
        return {"status": "ok", "service": "e2b-sandlock"}

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if runtime_registry is None:
        # The envd service is only imported on the single-host deployment:
        # a separated control-plane image (E2B_ENABLE_LOCAL_NODE=false) runs
        # without it and uses a no-op registry instead.
        try:
            from envd_service.runtime.registry import RuntimeRegistry
        except ImportError:  # pragma: no cover - separated control plane
            RuntimeRegistry = None  # type: ignore[assignment]
        if RuntimeRegistry is not None:
            runtime_registry = RuntimeRegistry(
                workspace_base or settings.workspace_base
            )
        else:
            runtime_registry = _NoopRuntimeRegistry()
    app.state.runtime_registry = runtime_registry
    # E9.1: a single-process (combined/local-node) deployment has no worker
    # heartbeat to carry sandbox activity, so the shared runtime registry
    # reports it straight into the sandbox registry's idle accounting.
    add_activity_callback = getattr(
        runtime_registry, "add_activity_callback", None
    )
    if callable(add_activity_callback):
        add_activity_callback(
            lambda sandbox_id, moment: registry.apply_activity_report(
                None, {sandbox_id: moment}
            )
        )
    app.state.workspace_base = workspace_base or settings.workspace_base
    app.state.workspace_base.mkdir(parents=True, exist_ok=True)
    volume_root = settings.shared_volume_root or (
        (workspace_base or settings.workspace_base) / "_volumes"
    )
    app.state.volumes = volumes_registry or VolumeRegistry(
        volume_root,
        redis_client=redis_client,
        token_ttl_seconds=settings.volume_token_ttl_s,
    )
    app.state.secrets = secrets_registry or SecretRegistry(
        (workspace_base or settings.workspace_base) / "_secrets",
        redis_client=redis_client,
        master_key=settings.secret_master_key,
        legacy_master_keys=settings.secret_master_keys,
    )
    app.state.snapshots = snapshots_registry or SnapshotRegistry(
        (workspace_base or settings.workspace_base),
        # F11 step 3: the per-id copy claim is fleet-wide when the deployment
        # has a shared store (the record's ``creating`` status is the durable
        # half of it; this is the window between "no record" and "record").
        redis_client=redis_client,
    )
    app.state.nodes = nodes_registry or NodeRegistry(
        redis_client=redis_client,
        heartbeat_timeout=settings.node_heartbeat_timeout_s,
    )
    app.state.recent_failures = SlidingWindowCounter()
    app.state.templates = templates_registry or TemplateRegistry(
        (workspace_base or settings.workspace_base) / "_templates"
    )
    # F11 step 4: every limiter below is named and shares its window through
    # the deployment's Redis when there is one. Without a name they would share
    # a key namespace and two limits would spend each other's budget; without
    # Redis they keep the per-process window they always had.
    def _limiter(name: str, limit: int) -> SlidingWindowRateLimiter:
        return SlidingWindowRateLimiter(
            limit, name=name, redis_client=redis_client
        )

    app.state.create_limiter = _limiter("create", settings.create_rate_limit_per_min)
    # E3.5: template build admission. The slot counter bounds concurrent
    # buildkit builds (the actual CPU/disk consumer); the per-key limiter
    # additionally throttles serial build bombardment. Both are per-process
    # (same shape as the create limiter; single control-plane deployment).
    app.state.template_build_slots = 0
    app.state.template_build_slots_lock = threading.Lock()
    app.state.template_build_limiter = _limiter(
        "template-build", settings.template_build_rate_limit_per_min
    )
    app.state.tenant_create_limiter = _limiter(
        "tenant-create", settings.create_rate_limit_per_min
    )
    # Same admission shape for the other resource-creating endpoints. Per
    # endpoint, so a snapshot burst cannot spend the sandbox-create budget.
    app.state.snapshot_limiter = _limiter(
        "snapshot", settings.snapshot_rate_limit_per_min
    )
    app.state.volume_limiter = _limiter("volume", settings.volume_rate_limit_per_min)
    app.state.tenant_snapshot_limiter = _limiter(
        "tenant-snapshot", settings.snapshot_rate_limit_per_min
    )
    app.state.tenant_volume_limiter = _limiter(
        "tenant-volume", settings.volume_rate_limit_per_min
    )
    app.state.tenant_limiters = {
        tenant_id: _limiter(f"tenant:{tenant_id}", limit)
        for tenant_id, limit in settings.tenant_rate_limits.items()
    }
    if settings.enable_local_node and app.state.nodes.get("local") is None:
        app.state.nodes.add_local_node(
            total_memory_mb=settings.max_total_memory_mb,
            total_cpu_percent=settings.max_total_cpu_percent,
            total_disk_mb=settings.max_total_disk_mb,
            total_processes=settings.max_total_processes,
        )
    app.state.select_node = app.state.nodes.select_and_reserve
    app.add_exception_handler(OfficialError, official_error_handler)
    # Snapshots first so DELETE /templates/{snapshotID} wins over the
    # templates catch-all in the sandboxes router.
    app.include_router(snapshots_router)
    app.include_router(templates_router)
    app.include_router(sandboxes_router)
    app.include_router(volumes_router)
    app.include_router(secrets_router)
    app.include_router(nodes_router)
    app.include_router(internal_router)
    return app
