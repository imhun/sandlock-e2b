"""Health, envs, metrics and init endpoints."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response

from envd_service.http.auth import HttpAuthError, require_http_sandbox

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


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
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
    return {
        "cpu": {"usedPercent": 0.0, "total": runtime.cpu_percent},
        "memory": {
            "usedBytes": 0,
            "totalBytes": runtime.memory_mb * 1024 * 1024,
        },
        "disk": {
            "usedBytes": _dir_size(workspace),
            "totalBytes": min(disk_usage.total, runtime.disk_mb * 1024 * 1024),
            "freeBytes": max(0, disk_usage.free),
        },
    }


@router.post("/init")
async def init(request: Request) -> Response:
    require_http_sandbox(request)
    return Response(status_code=204)

