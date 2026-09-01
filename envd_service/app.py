"""Envd service FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from envd_service.config import Settings
from envd_service.agent import (
    NodeAgent,
    _executor_needs_images,
    router as agent_router,
)
from envd_service.http.auth import HttpAuthError, http_error_response
from envd_service.http.files import router as files_router
from envd_service.http.health import router as health_router
from envd_service.http.mcp import router as mcp_router
from envd_service.quota_maintenance import QuotaMonitor
from envd_service.rpc import register_rpc
from envd_service.runtime.context import SandboxRuntimeContext
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.uid_pool import UidPool
from envd_service.xfs_quota import ProjectQuotaError, reconcile_orphan_projects

logger = logging.getLogger(__name__)


async def _warm_base_image(settings: Settings) -> None:
    """Pre-extract the configured base image + template images at startup."""
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    images = [settings.base_image]
    images.extend(v for v in settings.template_images.values() if v)
    for image in dict.fromkeys(images):  # dedupe, keep order
        if not image:
            continue
        try:
            await asyncio.to_thread(
                resolve_image_rootfs,
                image,
                settings.image_cache_dir,
                registry_username=settings.image_registry_username,
                registry_password=settings.image_registry_password,
            )
            logger.info("worker image warmed: %s", image)
        except Exception:
            logger.warning("worker image warm failed: %s", image, exc_info=True)


async def _startup_reconcile(settings: Settings) -> None:
    """Reconcile quota table vs sandbox.json records once at worker startup."""
    try:
        result = await asyncio.to_thread(
            reconcile_orphan_projects,
            workspace_base=settings.workspace_base,
            mount_point=settings.workspace_base,
            via_agent=settings.quota_via_agent,
        )
    except ProjectQuotaError as exc:
        logger.warning("startup quota reconciliation skipped: %s", exc)
        return
    logger.info(
        "startup quota reconciliation: cleaned=%s skipped=%s",
        result.get("cleaned"),
        result.get("skipped"),
    )


async def _startup_uid_reconcile(pool: UidPool) -> None:
    """Reclaim orphan host uids once at worker startup (E3.2)."""
    result = await asyncio.to_thread(pool.reconcile)
    logger.info(
        "startup uid reconciliation: referenced=%s reclaimed=%s cleaned=%s "
        "skipped=%s",
        result.get("referenced"),
        result.get("reclaimed"),
        result.get("cleaned"),
        result.get("skipped"),
    )


def create_app(
    *,
    settings: Settings | None = None,
    runtime_registry: RuntimeRegistry | None = None,
    workspace_base=None,
    control_plane_url: str | None = None,
    node_address: str | None = None,
) -> FastAPI:
    settings = settings or Settings()
    control_plane_url = control_plane_url or os.getenv("E2B_CONTROL_PLANE_URL")
    node_address = node_address or os.getenv("E2B_NODE_ADDRESS")
    quota_agent_client = None
    if settings.quota_via_agent:
        from envd_service.quota_agent import configure_quota_agent_client

        quota_agent_client = configure_quota_agent_client(
            url=settings.quota_agent_url,
            token=settings.quota_agent_token,
            timeout_s=settings.quota_agent_timeout_s,
        )
    runtime_registry = runtime_registry or RuntimeRegistry(
        workspace_base or settings.workspace_base
    )
    # E5.1: per-sandbox host uids need a privileged supervisor (root /
    # CAP_SETUID + chown). A non-root worker (uid 65534) cannot map
    # arbitrary host uids (S1.2 fail-closed), so the switch is auto-disabled
    # and the worker keeps the fixed-identity + Landlock model instead of
    # crash-looping on EPERM.
    if settings.per_sandbox_uid and os.geteuid() == 0:
        runtime_registry.uid_pool = UidPool(
            start=settings.uid_pool_start,
            size=settings.uid_pool_size,
            workspace_base=workspace_base or settings.workspace_base,
        )
        runtime_registry.add_unregister_callback(
            runtime_registry.uid_pool.release
        )
    elif settings.per_sandbox_uid:
        logger.warning(
            "E2B_PER_SANDBOX_UID is enabled but the worker is not running "
            "as root; per-sandbox host uids are disabled (non-root workers "
            "use the fixed identity + Landlock model, E5.1)"
        )
    quota_monitor = QuotaMonitor(
        workspace_base=settings.workspace_base,
        mount_point=settings.workspace_base,
        via_agent=settings.quota_via_agent,
        interval_s=settings.quota_monitor_interval_s,
        quota_warn_ratio=settings.quota_warn_ratio,
        disk_warn_ratio=settings.disk_warn_ratio,
        disk_error_ratio=settings.disk_error_ratio,
    )
    agent = NodeAgent(
        settings=settings,
        runtime_registry=runtime_registry,
        control_plane_url=control_plane_url,
        node_address=node_address,
        metrics_provider=quota_monitor.metrics,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enable_netns:
            from envd_service.netns import ensure_worker_netns_plumbing

            ensure_worker_netns_plumbing()
        app.state.quota_monitor = quota_monitor
        quota_monitor.start()
        reconcile_task: asyncio.Task | None = None
        if settings.quota_reconcile_on_startup:
            reconcile_task = asyncio.create_task(_startup_reconcile(settings))
            app.state.reconcile_task = reconcile_task
        uid_reconcile_task: asyncio.Task | None = None
        if (
            settings.per_sandbox_uid
            and settings.uid_reconcile_on_startup
            and os.geteuid() == 0
            and runtime_registry.uid_pool is not None
        ):
            uid_reconcile_task = asyncio.create_task(
                _startup_uid_reconcile(runtime_registry.uid_pool)
            )
        agent.start()
        if settings.base_image and _executor_needs_images(settings.executor):
            app.state.warm_task = asyncio.create_task(_warm_base_image(settings))
        yield
        warm_task = getattr(app.state, "warm_task", None)
        if warm_task is not None:
            warm_task.cancel()
        if reconcile_task is not None:
            reconcile_task.cancel()
            try:
                await reconcile_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("startup reconcile task failed", exc_info=True)
        if uid_reconcile_task is not None:
            uid_reconcile_task.cancel()
            try:
                await uid_reconcile_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "startup uid reconcile task failed", exc_info=True
                )
        await agent.stop()
        await quota_monitor.stop()
        quota_agent_client = getattr(app.state, "quota_agent_client", None)
        if quota_agent_client is not None:
            quota_agent_client.close()
        for ctx in app.state.runtimes.values():
            ctx.shutdown()
        app.state.runtimes.clear()

    app = FastAPI(title="E2B Sandlock Gateway - Envd Service", lifespan=lifespan)
    app.state.settings = settings
    app.state.quota_agent_client = quota_agent_client
    app.state.runtime_registry = runtime_registry
    app.state.runtimes: dict[str, SandboxRuntimeContext] = {}
    app.state.context_factory = lambda record: SandboxRuntimeContext(record, settings)
    runtime_registry.add_unregister_callback(
        lambda sandbox_id: (
            app.state.runtimes.pop(sandbox_id, None).shutdown()
            if sandbox_id in app.state.runtimes
            else None
        )
    )
    runtime_registry.add_state_callback(
        lambda sandbox_id, state: (
            (
                app.state.runtimes[sandbox_id].pause()
                if state == "paused"
                else app.state.runtimes[sandbox_id].resume()
            )
            if sandbox_id in app.state.runtimes
            else None
        )
    )

    app.add_exception_handler(HttpAuthError, http_error_response)
    app.include_router(health_router)
    app.include_router(files_router)
    app.include_router(mcp_router)
    app.include_router(agent_router)
    register_rpc(app)
    return app
