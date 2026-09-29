"""Health, envs, metrics and init endpoints."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response

from envd_service.http.auth import HttpAuthError, require_http_sandbox
from envd_service.runtime import brief_stat

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> Response:
    try:
        require_http_sandbox(request, health=True)
    except HttpAuthError as e:
        return Response(status_code=e.status_code)
    return Response(status_code=204)


@router.get("/envs")
async def envs(request: Request) -> dict[str, str]:
    runtime = require_http_sandbox(request)
    return dict(runtime.env_vars)


def _dir_size(path: Path, *, sandbox_id: str | None = None) -> int:
    # Track F / fix round 1 (c1): the workspace is `0770` owned by the
    # sandbox uid with the worker's gid, so the worker's group access walks it
    # in-process; priv_helpers falls back to e2b-maint for the trees that
    # access cannot reach. Either way this no longer silently reports 0.
    from envd_service import agent_fileops, priv_helpers

    client = agent_fileops.active()
    if client is not None:
        # C3 Task 4: measured by the agent (``walk-workspace``); the sandbox id
        # is the only thing this side names.
        if sandbox_id is None:
            raise priv_helpers.PrivHelperError(
                "the C3 agent shape needs the sandbox id to measure "
                f"{path}: the metrics call did not name one"
            )
        try:
            return client.workspace_bytes(sandbox_id)
        except Exception as exc:  # noqa: BLE001 - a metric may not raise
            # N2's second half: this endpoint answers a *number*, and the
            # pre-C3 shapes already answer "what the worker's own DAC can see"
            # when nobody can be asked. Same here, with the reason on record --
            # a metrics call is not the place to fail a sandbox's monitoring.
            logger.warning(
                "cannot measure %s through the agent: %s: %s; falling back to "
                "the worker's own walk",
                sandbox_id,
                type(exc).__name__,
                exc,
            )

    brokered = priv_helpers.dir_size(path)
    if brokered is not None:
        return brokered
    # Same quantity as `priv_helpers.dir_size` (N31 fix 2): the files
    # (`entry_size`) plus each directory's allocated size (`directory_cost` --
    # `st_blocks x 512`, which is what `du` reports; `st_size` is 8-32x that on
    # this NAS). This branch only runs when neither the worker's own DAC nor
    # the broker can read the tree, so it is the last resort -- but it must not
    # answer a different definition than the path that normally does.
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            try:
                total += brief_stat.directory_cost(root)
            except OSError:
                pass
            for name in files:
                try:
                    total += brief_stat.entry_size(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    runtime = require_http_sandbox(request)
    workspace = Path(runtime.workspace_dir)
    disk_usage = shutil.disk_usage(workspace)
    # Off the event loop (C3 Task 4 review, N2): in the agent shape this walk is
    # a control-plane round trip, and its deadline is the *file-op* one (minutes)
    # because a big tree legitimately takes that long. Awaiting it inline would
    # park every heartbeat and every sandbox API behind a black-holed CP.
    # Off the event loop (C3 Task 4 review, N2): in the agent shape this walk is
    # a control-plane round trip, and its deadline is the *file-op* one (minutes)
    # because a big tree legitimately takes that long. Awaiting it inline would
    # park every heartbeat and every sandbox API behind a black-holed CP.
    used_bytes = await asyncio.to_thread(
        _dir_size, workspace, sandbox_id=runtime.sandbox_id
    )
    return {
        "cpu": {"usedPercent": 0.0, "total": runtime.cpu_percent},
        "memory": {
            "usedBytes": 0,
            "totalBytes": runtime.memory_mb * 1024 * 1024,
        },
        "disk": {
            "usedBytes": used_bytes,
            "totalBytes": min(disk_usage.total, runtime.disk_mb * 1024 * 1024),
            "freeBytes": max(0, disk_usage.free),
        },
    }


@router.post("/init")
async def init(request: Request) -> Response:
    require_http_sandbox(request)
    return Response(status_code=204)
