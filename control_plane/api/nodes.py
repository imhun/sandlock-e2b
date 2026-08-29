"""Node management endpoints (control-plane admin view)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import require_api_key

router = APIRouter()


@router.get("/nodes", dependencies=[Depends(require_api_key)])
async def list_nodes(request: Request) -> list[dict[str, Any]]:
    return [n.to_dict() for n in request.app.state.nodes.list()]


@router.delete(
    "/nodes/{node_id}", status_code=204, dependencies=[Depends(require_api_key)]
)
async def remove_node(node_id: str, request: Request) -> Response:
    if request.app.state.nodes.get(node_id) is None:
        raise OfficialError(404, f"Node {node_id} not found")
    request.app.state.nodes.remove(node_id)
    return Response(status_code=204)

