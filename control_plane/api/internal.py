"""Internal APIs used by worker agents and the envd gateway."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, Request, Response

from control_plane.api.errors import OfficialError

router = APIRouter()


def _require_internal_key(request: Request) -> None:
    key = request.headers.get("X-Internal-Key")
    settings = request.app.state.settings
    internal_key = getattr(settings, "internal_api_key", None) or "internal-key"
    if key != internal_key:
        raise OfficialError(401, "Unauthorized")


@router.post("/internal/nodes/register")
async def register_node(request: Request) -> dict[str, Any]:
    _require_internal_key(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    record = request.app.state.nodes.register(
        node_id=body.get("nodeID"),
        address=body.get("address"),
        total_memory_mb=int(body.get("totalMemoryMB", 0)),
        total_cpu_percent=int(body.get("totalCPUPercent", 0)),
        total_disk_mb=int(body.get("totalDiskMB", 0)),
        total_processes=int(body.get("totalProcesses", 0)),
        images=body.get("images") or [],
        labels=body.get("labels") or {},
    )
    return {"nodeID": record.node_id}


@router.post("/internal/nodes/{node_id}/heartbeat")
async def node_heartbeat(node_id: str, request: Request) -> Response:
    _require_internal_key(request)
    record = request.app.state.nodes.heartbeat(node_id)
    if record is None:
        raise OfficialError(404, f"Node {node_id} not found")
    return Response(status_code=204)


@router.get("/internal/routes/{sandbox_id}")
async def get_route(sandbox_id: str, request: Request) -> dict[str, Any]:
    _require_internal_key(request)
    registry = request.app.state.registry
    try:
        record = registry.get(sandbox_id)
    except Exception:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        raise OfficialError(404, f"Node {record.node_id} not found")
    if node.status != "healthy":
        raise OfficialError(502, f"Node {record.node_id} unavailable")
    return {"nodeID": node.node_id, "address": node.address}


@router.get("/internal/nodes")
async def list_nodes_internal(request: Request) -> list[dict[str, Any]]:
    _require_internal_key(request)
    return [n.to_dict() for n in request.app.state.nodes.list()]
