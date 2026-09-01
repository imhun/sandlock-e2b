"""Control plane FastAPI application factory."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from control_plane.api.errors import OfficialError, official_error_handler
from control_plane.api.internal import router as internal_router
from control_plane.api.nodes import router as nodes_router
from control_plane.api.sandboxes import router as sandboxes_router
from control_plane.api.secrets import router as secrets_router
from control_plane.api.snapshots import router as snapshots_router
from control_plane.api.templates import router as templates_router
from control_plane.api.volumes import router as volumes_router
from control_plane.config import Settings
from control_plane.metrics import SlidingWindowCounter
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.secrets import SecretRegistry
from control_plane.registry.snapshots import SnapshotRegistry
from control_plane.registry.templates import TemplateRegistry
from control_plane.ratelimit import SlidingWindowRateLimiter
from control_plane.registry.ttl import TTLSweeper
from control_plane.registry.volumes import VolumeRegistry


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
    if settings.redis_url:
        from control_plane.registry.redis_backend import create_redis_client

        redis_client = create_redis_client(settings.redis_url)
    registry = registry or SandboxRegistry(settings, redis_client=redis_client)

    def _release_node_quota(record) -> None:
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
        def _on_sandbox_removed(record):
            node = app.state.nodes.get(record.node_id or "local")
            if node is not None and node.address != "local://":
                import httpx

                try:
                    httpx.delete(
                        f"{node.address}/agent/sandboxes/{record.sandbox_id}",
                        headers={
                            "X-Internal-Key": settings.internal_api_key
                        },
                        timeout=30,
                    )
                except httpx.HTTPError:
                    pass
                app.state.runtime_registry.unregister(record.sandbox_id)
            else:
                # Local runtime: full teardown (unregister + workspace and
                # per-sandbox volume slice cleanup, E2.5).
                from control_plane.api.sandboxes import _destroy_local

                _destroy_local(app.state, record)

        sweeper = TTLSweeper(on_expired=_on_sandbox_removed)
        app.state.sweeper = sweeper
        sweeper.start(registry)
        yield
        await sweeper.stop()

    app = FastAPI(title="E2B Sandlock Gateway - Control Plane", lifespan=lifespan)
    app.state.settings = settings
    app.state.redis_client = redis_client
    app.state.registry = registry

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
        (workspace_base or settings.workspace_base)
    )
    app.state.nodes = nodes_registry or NodeRegistry(redis_client=redis_client)
    app.state.recent_failures = SlidingWindowCounter()
    app.state.templates = templates_registry or TemplateRegistry(
        (workspace_base or settings.workspace_base) / "_templates"
    )
    app.state.create_limiter = SlidingWindowRateLimiter(
        settings.create_rate_limit_per_min
    )
    # E3.5: template build admission. The slot counter bounds concurrent
    # buildkit builds (the actual CPU/disk consumer); the per-key limiter
    # additionally throttles serial build bombardment. Both are per-process
    # (same shape as the create limiter; single control-plane deployment).
    app.state.template_build_slots = 0
    app.state.template_build_slots_lock = threading.Lock()
    app.state.template_build_limiter = SlidingWindowRateLimiter(
        settings.template_build_rate_limit_per_min
    )
    app.state.tenant_create_limiter = SlidingWindowRateLimiter(
        settings.create_rate_limit_per_min
    )
    app.state.tenant_limiters = {
        tenant_id: SlidingWindowRateLimiter(limit)
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
