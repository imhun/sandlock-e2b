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
from envd_service.rpc import register_rpc
from envd_service.runtime.context import SandboxRuntimeContext
from envd_service.runtime.registry import RuntimeRegistry

logger = logging.getLogger(__name__)


async def _warm_base_image(settings: Settings) -> None:
    """Pre-extract the configured base image rootfs at worker startup."""
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    try:
        await asyncio.to_thread(
            resolve_image_rootfs,
            settings.base_image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
        )
        logger.info("worker base image warmed: %s", settings.base_image)
    except Exception:
        logger.warning(
            "worker base image warm failed: %s",
            settings.base_image,
            exc_info=True,
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
    agent = NodeAgent(
        settings=settings,
        runtime_registry=runtime_registry,
        control_plane_url=control_plane_url,
        node_address=node_address,
    )
    runtime_registry = runtime_registry or RuntimeRegistry(
        workspace_base or settings.workspace_base
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enable_netns:
            from envd_service.netns import ensure_worker_netns_plumbing

            ensure_worker_netns_plumbing()
        agent.start()
        if settings.base_image and _executor_needs_images(settings.executor):
            app.state.warm_task = asyncio.create_task(_warm_base_image(settings))
        yield
        warm_task = getattr(app.state, "warm_task", None)
        if warm_task is not None:
            warm_task.cancel()
        await agent.stop()
        for ctx in app.state.runtimes.values():
            ctx.shutdown()
        app.state.runtimes.clear()

    app = FastAPI(title="E2B Sandlock Gateway - Envd Service", lifespan=lifespan)
    app.state.settings = settings
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
    app.include_router(agent_router)
    register_rpc(app)
    return app
