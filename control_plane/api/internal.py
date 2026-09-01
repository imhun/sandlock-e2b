"""Internal APIs used by worker agents and the envd gateway."""

from __future__ import annotations

import json
from collections import Counter
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
    _rebuild_node_reservations(request, record)
    return {"nodeID": record.node_id}


def _rebuild_node_reservations(request: Request, record) -> None:
    """Restore a re-registering node's reserved quota from sandbox records.

    Sandbox records persist in Redis across control-plane restarts but the
    node registry's reserved fields are in-memory; on re-registration the
    reservations start at zero, so fleet utilization would be misreported
    (and nodes over-committed). Aggregating the node's records here keeps the
    two views consistent.
    """
    dims = {"memory": 0, "cpu": 0, "disk": 0, "processes": 0}
    for sandbox in request.app.state.registry.list():
        if sandbox.node_id == record.node_id:
            dims["memory"] += sandbox.memory_mb
            dims["cpu"] += sandbox.cpu_count * 100
            dims["disk"] += sandbox.disk_size_mb
            dims["processes"] += sandbox.max_processes
    request.app.state.nodes.set_reserved(
        record.node_id,
        memory_mb=dims["memory"],
        cpu_percent=dims["cpu"],
        disk_mb=dims["disk"],
        processes=dims["processes"],
    )


@router.post("/internal/nodes/{node_id}/heartbeat")
async def node_heartbeat(node_id: str, request: Request) -> Response:
    _require_internal_key(request)
    body: dict[str, Any] = {}
    raw = await request.body()
    if raw:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise OfficialError(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise OfficialError(400, "Heartbeat body must be a JSON object")
    record = request.app.state.nodes.heartbeat(node_id)
    if record is None:
        raise OfficialError(404, f"Node {node_id} not found")
    record.update_usage(
        used_disk_mb=body.get("diskUsedMB"),
        disk_total_mb=body.get("diskTotalMB"),
        quota_over_limit=body.get("quotaOverLimit"),
        quota_near_limit=body.get("quotaNearLimit"),
        quota_over_limit_count=body.get("quotaOverLimitCount"),
        quota_near_limit_count=body.get("quotaNearLimitCount"),
        disk_warn_count=body.get("diskWarnCount"),
        disk_error_count=body.get("diskErrorCount"),
    )
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


@router.post("/internal/nodes/{node_id}/drain")
async def drain_node(node_id: str, request: Request) -> dict[str, Any]:
    _require_internal_key(request)
    nodes = request.app.state.nodes
    record = nodes.set_draining(node_id, True)
    if record is None:
        raise OfficialError(404, f"Node {node_id} not found")
    active = sum(
        1
        for r in request.app.state.registry.list()
        if r.node_id == node_id
    )
    return {"nodeID": node_id, "activeSandboxes": active, "draining": True}


@router.post("/internal/nodes/{node_id}/undrain")
async def undrain_node(node_id: str, request: Request) -> Response:
    _require_internal_key(request)
    record = request.app.state.nodes.set_draining(node_id, False)
    if record is None:
        raise OfficialError(404, f"Node {node_id} not found")
    return Response(status_code=204)


@router.get("/internal/fleet/metrics")
async def fleet_metrics(request: Request) -> dict[str, Any]:
    """Aggregate fleet state for the autoscaler.

    Returns per-node utilization/active sandboxes, fleet aggregates, the
    remaining standard-sandbox capacity, and the recent 503 error count.
    """
    _require_internal_key(request)
    settings = request.app.state.settings
    nodes = request.app.state.nodes.list()
    records = request.app.state.registry.list()
    active_by_node: Counter[str] = Counter(r.node_id for r in records)

    dims = {
        "memory": ("reserved_memory_mb", "total_memory_mb", settings.default_memory_mb),
        "cpu": ("reserved_cpu_percent", "total_cpu_percent", settings.default_cpu_percent),
        "disk": ("reserved_disk_mb", "total_disk_mb", settings.default_disk_mb),
        "processes": (
            "reserved_processes",
            "total_processes",
            settings.default_max_processes,
        ),
    }

    node_metrics: list[dict[str, Any]] = []
    fleet_totals = {key: {"reserved": 0, "total": 0} for key in dims}
    remaining_capacity: int | None = 0
    unlimited_node = False
    for node in nodes:
        per_node: dict[str, Any] = {}
        node_remaining: int | None = None
        for key, (reserved_attr, total_attr, demand) in dims.items():
            reserved = getattr(node, reserved_attr)
            total = getattr(node, total_attr)
            fleet_totals[key]["reserved"] += reserved
            fleet_totals[key]["total"] += total
            utilization = (reserved / total) if total > 0 else 0.0
            per_node[key] = {
                "reserved": reserved,
                "total": total,
                "utilization": round(utilization, 4),
            }
            if total > 0 and demand > 0:
                candidate = max(0, (total - reserved) // demand)
                node_remaining = (
                    candidate
                    if node_remaining is None
                    else min(node_remaining, candidate)
                )
        if node_remaining is None:
            unlimited_node = True
        elif remaining_capacity is not None:
            remaining_capacity += node_remaining
        node_metrics.append(
            {
                "nodeID": node.node_id,
                "status": node.status,
                "draining": node.draining,
                "images": node.images,
                "activeSandboxes": active_by_node.get(node.node_id, 0),
                "utilization": per_node,
            }
        )

    fleet: dict[str, Any] = {}
    for key, totals in fleet_totals.items():
        fleet[key] = {
            "reserved": totals["reserved"],
            "total": totals["total"],
            "utilization": round(
                (totals["reserved"] / totals["total"])
                if totals["total"] > 0
                else 0.0,
                4,
            ),
        }
    return {
        "nodes": node_metrics,
        "fleet": fleet,
        "standardSandboxDims": {
            "memory": settings.default_memory_mb,
            "cpu": settings.default_cpu_percent,
            "disk": settings.default_disk_mb,
            "processes": settings.default_max_processes,
        },
        "remainingSandboxCapacity": (
            None if unlimited_node else remaining_capacity
        ),
        "activeSandboxes": len(records),
        "recent503Count": request.app.state.recent_failures.count(),
    }
