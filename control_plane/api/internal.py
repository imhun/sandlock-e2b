"""Internal APIs used by worker agents and the envd gateway."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from fastapi import APIRouter, Header, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import verify_internal_key
from gateway_common.paths import validate_sandbox_id

router = APIRouter()


def _require_internal_key(request: Request) -> None:
    settings = request.app.state.settings
    if not verify_internal_key(
        request.headers.get("X-Internal-Key"), settings
    ):
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
        # E9.2: a paused sandbox gave its reservation back, so it must not be
        # re-booked here (that would strand capacity forever).
        if sandbox.node_id != record.node_id or sandbox.quota_released:
            continue
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
        mcp_ports_in_use=body.get("mcpPortsInUse"),
        mcp_ports_capacity=body.get("mcpPortsCapacity"),
    )
    activity = body.get("sandboxActivity")
    if activity is not None and not isinstance(activity, dict):
        raise OfficialError(400, "sandboxActivity must be a JSON object")
    if isinstance(activity, dict) and activity:
        # E9.1: the worker is the only observer of in-sandbox traffic, so its
        # report is what makes idle detection (and eviction) possible.
        request.app.state.registry.apply_activity_report(node_id, activity)
    return Response(status_code=204)


@router.get("/internal/nodes/{node_id}/sandboxes")
async def node_sandboxes(node_id: str, request: Request) -> dict[str, Any]:
    """Control-plane view of one node's sandbox records (E6.1 recovery).

    The worker uses this as the authoritative list when reconciling its
    local runtime after a partition: any local runtime not in this list is
    an orphan and is torn down locally. The returned ``sandboxIDs`` are the
    reconcile snapshot — the worker must echo them back in
    ``POST .../reconcile``'s ``snapshotIDs`` so the control plane only ever
    removes records that existed when the snapshot was taken (a record
    created after the snapshot is a concurrent create and must survive).
    """
    _require_internal_key(request)
    records = request.app.state.registry.list_by_node(node_id)
    return {"nodeID": node_id, "sandboxIDs": [r.sandbox_id for r in records]}


@router.post("/internal/nodes/{node_id}/reconcile")
async def node_reconcile(node_id: str, request: Request) -> dict[str, Any]:
    """Reconcile control-plane records against the worker's local runtime.

    Body: ``{"sandboxIDs": [...], "snapshotIDs": [...]}`` — ``sandboxIDs``
    are the sandboxes this worker currently runs; ``snapshotIDs`` are the
    records the worker saw in ``GET .../sandboxes`` before computing its
    diff. Records the worker still has are un-orphaned (recovery); records
    it no longer has are removed only when they were part of the snapshot —
    records created after the snapshot (concurrent creates) are kept. The
    result mirrors :meth:`SandboxRegistry.recover_node`.
    """
    _require_internal_key(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("sandboxIDs"), list)
        or not isinstance(body.get("snapshotIDs"), list)
    ):
        raise OfficialError(
            400, "Body must be {\"sandboxIDs\": [...], \"snapshotIDs\": [...]}"
        )
    sandbox_ids = [s for s in body["sandboxIDs"] if isinstance(s, str)]
    snapshot_ids = [s for s in body["snapshotIDs"] if isinstance(s, str)]
    if any(not validate_sandbox_id(s) for s in sandbox_ids) or any(
        not validate_sandbox_id(s) for s in snapshot_ids
    ):
        raise OfficialError(400, "sandboxIDs/snapshotIDs must be valid sandbox ids")
    return request.app.state.registry.recover_node(
        node_id,
        set(sandbox_ids),
        set(snapshot_ids),
        timeout=request.app.state.settings.default_timeout,
    )


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


_EMPTY_TENANT_USAGE = {
    "sandboxes": 0,
    "memoryMB": 0,
    "cpuPercent": 0,
    "diskMB": 0,
    "processes": 0,
}


@router.get("/internal/tenants")
async def internal_tenants(request: Request) -> dict[str, Any]:
    """Per-tenant usage and configured limits (ops reconciliation, E3.1).

    Internal API: authenticated with X-Internal-Key, never tenant-scoped.
    ``unowned`` usage (tenant_id None) is included when present so operators
    can detect resources that still need the migration script.
    """
    _require_internal_key(request)
    settings = request.app.state.settings
    usage = request.app.state.registry.tenant_usage()
    tenant_ids = (
        set(settings.tenant_map) | set(settings.tenant_limits) | set(usage)
    )
    tenant_ids.discard(None)
    tenants = [
        {
            "tenantID": tenant_id,
            "used": usage.get(tenant_id, dict(_EMPTY_TENANT_USAGE)),
            "limits": settings.tenant_limits.get(tenant_id, {}),
        }
        for tenant_id in sorted(tenant_ids)
    ]
    body: dict[str, Any] = {
        "tenants": tenants,
        "compatibleMode": not settings.tenants_enabled,
    }
    if usage.get(None):
        body["unowned"] = usage[None]
    return body
