"""Sandbox fork and snapshot endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import _require_owned, require_api_key, tenant_of, tenant_scope
from control_plane.ratelimit import enforce_resource_limit
from control_plane.api.sandboxes import _provision_local, _provision_remote
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxStateConflictError,
    UnknownSandboxError,
)
from control_plane.registry.snapshots import UnknownSnapshotError
from gateway_common.ids import sandbox_id as new_sandbox_id
from gateway_common.upload import UploadTooLargeError, read_json_body

logger = logging.getLogger(__name__)

router = APIRouter()


def _registry(request: Request):
    return request.app.state.registry


def _snapshots(request: Request):
    return request.app.state.snapshots


def _check_name_size(settings, name: str) -> None:
    """E5.3: cap user-supplied snapshot names (UTF-8 bytes)."""
    if (
        settings.max_name_bytes > 0
        and len(name.encode("utf-8")) > settings.max_name_bytes
    ):
        raise OfficialError(400, f"name exceeds {settings.max_name_bytes}-byte limit")


def _capture_snapshot(request: Request, sandbox_id: str, name: str | None):
    """Freeze the sandbox, copy its filesystem, thaw it."""
    registry = _registry(request)
    record = registry.get(sandbox_id)
    _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    if record.state != "running":
        raise OfficialError(409, f"Sandbox {sandbox_id} is not running")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        raise OfficialError(502, f"Node {record.node_id} not found")
    request.app.state.runtime_registry.freeze(sandbox_id)
    try:
        snapshot_id = new_sandbox_id().replace("sbx_", "snap_")
        if node.address == "local://":
            return _snapshots(request).create_from_sandbox(
                workspace_dir=record.workspace_dir,
                template_id=record.template_id,
                env_vars=record.env_vars,
                metadata=record.metadata,
                volume_mounts=[
                    {"name": m["name"], "path": m["path"]}
                    for m in record.volume_mounts
                ],
                base_image=record.base_image,
                allow_internet_access=record.allow_internet_access,
                node_id=node.node_id,
                name=name,
                tenant_id=record.tenant_id,
            )
        # Remote snapshot: ask the worker to copy the sandbox directory into
        # its local snapshot store; the control plane keeps only metadata.
        import httpx

        resp = httpx.post(
            f"{node.address}/agent/snapshots",
            json={"snapshotID": snapshot_id, "sandboxID": sandbox_id},
            headers={"X-Internal-Key": request.app.state.settings.internal_api_key},
            timeout=120,
        )
        if resp.status_code != 201:
            raise OfficialError(502, f"Node {node.node_id} failed to snapshot")
        return _snapshots(request).create_from_sandbox(
            workspace_dir=None,
            template_id=record.template_id,
            env_vars=record.env_vars,
            metadata=record.metadata,
            volume_mounts=[
                {"name": m["name"], "path": m["path"]}
                for m in record.volume_mounts
            ],
            base_image=record.base_image,
            allow_internet_access=record.allow_internet_access,
            node_id=node.node_id,
            name=name,
            snapshot_id=snapshot_id,
            copy_fs=False,
            tenant_id=record.tenant_id,
        )
    finally:
        request.app.state.runtime_registry.thaw(sandbox_id)


@router.post(
    "/sandboxes/{sandbox_id}/snapshots",
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def create_snapshot(
    sandbox_id: str, request: Request
) -> dict[str, Any]:
    # A snapshot copies the sandbox filesystem: the heaviest resource-creating
    # endpoint there is, so it is admitted like sandbox create rather than left
    # unbounded.
    enforce_resource_limit(
        request,
        limiter=request.app.state.snapshot_limiter,
        tenant_limiter=request.app.state.tenant_snapshot_limiter,
        message="Snapshot create rate limit exceeded",
    )
    try:
        body = await read_json_body(
            request, request.app.state.settings.max_json_body_bytes
        )
    except UploadTooLargeError:
        raise OfficialError(413, "Request body exceeds maximum size")
    except json.JSONDecodeError:
        body = {}
    name = body.get("name") if isinstance(body, dict) else None
    if name is not None and not isinstance(name, str):
        raise OfficialError(400, "name must be a string")
    if name is not None:
        _check_name_size(request.app.state.settings, name)
    try:
        record = _capture_snapshot(request, sandbox_id, name)
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    except SandboxStateConflictError:
        raise OfficialError(409, f"Sandbox {sandbox_id} is not running")
    logger.info("snapshot %s captured from sandbox %s", record.snapshot_id, sandbox_id)
    return record.as_snapshot_info()


@router.get("/snapshots", dependencies=[Depends(require_api_key)])
async def list_snapshots(
    request: Request,
    response: Response,
    sandbox_id: str | None = Query(default=None, alias="sandboxID"),
    name: str | None = Query(default=None),
    nextToken: str | None = Query(default=None, alias="nextToken"),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict[str, Any]]:
    offset = int(nextToken) if nextToken and nextToken.isdigit() else 0
    records = _snapshots(request).list(
        sandbox_id_filter=sandbox_id,
        name=name,
        limit=limit,
        offset=offset,
        tenant_id=tenant_scope(request),
    )
    total = len(
        _snapshots(request).list(name=name, tenant_id=tenant_scope(request))
    )
    if offset + len(records) < total:
        response.headers["X-Next-Token"] = str(offset + len(records))
    return [r.as_snapshot_info() for r in records]


@router.delete(
    "/templates/{snapshot_id}",
    status_code=204,
    dependencies=[Depends(require_api_key)],
)
async def delete_snapshot(snapshot_id: str, request: Request) -> Response:
    try:
        record = _snapshots(request).get(snapshot_id)
        _require_owned(request, record, resource_id=snapshot_id, label="Snapshot")
        _snapshots(request).delete(snapshot_id)
    except UnknownSnapshotError:
        raise OfficialError(404, f"Snapshot {snapshot_id} not found")
    return Response(status_code=204)


@router.post(
    "/sandboxes/{sandbox_id}/fork",
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def fork_sandbox(sandbox_id: str, request: Request) -> list[dict[str, Any]]:
    try:
        body = await read_json_body(
            request, request.app.state.settings.max_json_body_bytes
        )
    except UploadTooLargeError:
        raise OfficialError(413, "Request body exceeds maximum size")
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    timeout = body.get("timeout")
    count = body.get("count", 1)
    settings = request.app.state.settings
    timeout = timeout if timeout is not None else settings.default_timeout
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise OfficialError(400, "timeout must be a positive integer")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > 100:
        raise OfficialError(400, "count must be an integer between 1 and 100")

    try:
        snapshot = _capture_snapshot(request, sandbox_id, name=None)
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    except SandboxStateConflictError:
        raise OfficialError(409, f"Sandbox {sandbox_id} is not running")

    results: list[dict[str, Any]] = []
    tenant, is_admin = tenant_of(request)
    for _ in range(count):
        try:
            sandbox = await _create_sandbox_from_snapshot(
                request, snapshot, timeout, tenant_id=tenant, is_admin=is_admin
            )
            results.append({"sandbox": sandbox})
        except OfficialError as e:
            results.append({"error": {"code": e.code, "message": e.message}})
        except Exception as e:  # pragma: no cover - defensive
            results.append({"error": {"code": 500, "message": str(e)}})
    return results


async def _create_sandbox_from_snapshot(
    request: Request, snapshot, timeout: int, *, tenant_id: str | None, is_admin: bool
) -> dict[str, Any]:
    """Create one sandbox from a snapshot's filesystem + metadata."""
    registry = _registry(request)
    settings = request.app.state.settings
    try:
        record = registry.create(
            template_id=snapshot.template_id,
            timeout=timeout,
            metadata=dict(snapshot.metadata),
            env_vars=dict(snapshot.env_vars),
            secure=True,
            allow_internet_access=snapshot.allow_internet_access,
            base_image=snapshot.base_image,
            volume_mounts=[
                {"name": m["name"], "path": m["path"]}
                for m in snapshot.volume_mounts
            ],
            tenant_id=tenant_id,
            is_admin=is_admin,
        )
    except ResourceUnavailableError as e:
        raise OfficialError(503, str(e))

    workspace_dir = request.app.state.workspace_base / record.sandbox_id
    node = request.app.state.nodes.select_and_reserve(
        base_image=snapshot.base_image,
        volume_node_id=snapshot.node_id,
        memory_mb=record.memory_mb,
        cpu_percent=record.cpu_count * 100,
        disk_mb=record.disk_size_mb,
        processes=record.max_processes,
    )
    if node is None:
        registry.delete(record.sandbox_id)
        raise OfficialError(503, "No resources available")
    record.node_id = node.node_id
    # Persist the node assignment: Redis-backed get() reconstructs records
    # from the store, so without save() the gateway cannot route to the fork.
    registry.save(record)
    try:
        if node.address == "local://":
            # Reuse the create path's local provisioner so a per-sandbox-uid
            # fork acquires/applies/commits a host uid through the shared pool
            # exactly like `_provision_local` does (I3 release on failure),
            # and the legacy no-pool shape gets the shared-uid workspace
            # alignment. The duplicate inline provisioning predated uid
            # allocation and silently registered forks without a host_uid.
            _provision_local(request, record, snapshot, record.volume_mounts, settings)
        else:
            await _provision_remote(
                request,
                record,
                node,
                settings,
                snapshot=None,
                volume_mounts=record.volume_mounts,
                snapshot_id=snapshot.snapshot_id,
            )
        record.append_log("sandbox created from snapshot")
    except Exception:
        registry.delete(record.sandbox_id)
        raise OfficialError(500, "Failed to provision forked sandbox")
    return record.as_sandbox()
