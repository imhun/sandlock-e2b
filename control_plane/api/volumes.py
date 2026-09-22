"""Volume CRUD and volume content endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from control_plane.api.errors import OfficialError
from control_plane.auth import _require_owned, require_api_key, tenant_of, tenant_scope
from control_plane.ratelimit import enforce_resource_limit
from control_plane.registry.volumes import UnknownVolumeError
from gateway_common.paths import PathTraversalError, resolve_under_root
from gateway_common.upload import (
    UploadTooLargeError,
    check_content_length,
    limit_bytes_from_mb,
    stream_body_to_file,
)

router = APIRouter()


def _volumes(request: Request):
    return request.app.state.volumes


def _require_volume(request: Request, volume_id: str):
    auth = request.headers.get("Authorization") or ""
    token = auth.removeprefix("Bearer ").strip() or request.headers.get("X-Access-Token")
    if not token:
        raise OfficialError(401, "Volume token is required")
    try:
        return _volumes(request).verify_token(volume_id, token)
    except UnknownVolumeError:
        raise OfficialError(401, "Invalid volume token")


def _resolve(volume, path: str) -> Path:
    try:
        return resolve_under_root(volume.path, path)
    except PathTraversalError as e:
        raise OfficialError(400, str(e)) from e


def _entry(path: Path, rel_root: Path) -> dict[str, Any]:
    st = path.stat()
    kind = "directory" if st.st_mode & 0o4000 or path.is_dir() else "file"
    if path.is_symlink():
        kind = "symlink"
    rel = path.relative_to(rel_root.resolve()).as_posix()
    entry: dict[str, Any] = {
        "name": path.name,
        "type": kind,
        "path": rel,
        "size": st.st_size,
        "mode": st.st_mode & 0o7777,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "atime": _iso(st.st_atime),
        "mtime": _iso(st.st_mtime),
        "ctime": _iso(st.st_ctime),
    }
    if path.is_symlink():
        entry["target"] = __import__("os").readlink(path)
    return entry


def _iso(mtime: float) -> str:
    import datetime

    dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@router.post("/volumes", status_code=201, dependencies=[Depends(require_api_key)])
async def create_volume(request: Request) -> dict[str, Any]:
    # Volume create allocates a quota slice and a directory tree under the
    # workspace base: admitted like the other resource-creating endpoints.
    enforce_resource_limit(
        request,
        limiter=request.app.state.volume_limiter,
        tenant_limiter=request.app.state.tenant_volume_limiter,
        message="Volume create rate limit exceeded",
    )
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    name = body.get("name") if isinstance(body, dict) else None
    per_sandbox_quota_mb = (
        body.get("perSandboxQuotaMb", 0) if isinstance(body, dict) else 0
    )
    tenant, _is_admin = tenant_of(request)
    try:
        record = _volumes(request).create(
            name, per_sandbox_quota_mb=per_sandbox_quota_mb, tenant_id=tenant
        )
    except ValueError as e:
        raise OfficialError(400, str(e))
    return record.as_volume_and_token()


@router.get("/volumes", dependencies=[Depends(require_api_key)])
async def list_volumes(
    request: Request,
    response: Response,
    nextToken: str | None = Query(default=None, alias="nextToken"),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict[str, Any]]:
    offset = int(nextToken) if nextToken and nextToken.isdigit() else 0
    records = _volumes(request).list(
        limit=limit, offset=offset, tenant_id=tenant_scope(request)
    )
    total = len(_volumes(request).list(tenant_id=tenant_scope(request)))
    if len(records) == limit and offset + len(records) < total:
        response.headers["X-Next-Token"] = str(offset + len(records))
    return [r.as_volume() for r in records]


@router.get("/volumes/{volume_id}", dependencies=[Depends(require_api_key)])
async def get_volume(volume_id: str, request: Request) -> dict[str, Any]:
    try:
        record = _volumes(request).get(volume_id)
        _require_owned(request, record, resource_id=volume_id, label="Volume")
        return record.as_volume_and_token()
    except UnknownVolumeError:
        raise OfficialError(404, f"Volume {volume_id} not found")


@router.delete(
    "/volumes/{volume_id}", status_code=204, dependencies=[Depends(require_api_key)]
)
async def delete_volume(volume_id: str, request: Request) -> Response:
    try:
        record = _volumes(request).get(volume_id)
        _require_owned(request, record, resource_id=volume_id, label="Volume")
        # The volume's payload is a tree on the shared NAS; the registry's
        # delete is an `rmtree` over it. Not on the event loop (N32: a NAS
        # `rmtree` in an async handler is a stall, and a stall is what the
        # node-health sweep reads as a dead node).
        await asyncio.to_thread(_volumes(request).delete, volume_id)
    except UnknownVolumeError:
        raise OfficialError(404, f"Volume {volume_id} not found")
    return Response(status_code=204)


@router.get("/volumecontent/{volume_id}/file")
async def volume_read_file(
    volume_id: str, request: Request, path: str = Query(...)
) -> Response:
    volume = _require_volume(request, volume_id)
    target = _resolve(volume, path)
    if not target.is_file():
        raise OfficialError(404, f"Path {path} not found")
    return Response(content=target.read_bytes(), media_type="application/octet-stream")


@router.get("/volumecontent/{volume_id}/path")
async def volume_stat_path(
    volume_id: str, request: Request, path: str = Query(...)
) -> dict[str, Any]:
    volume = _require_volume(request, volume_id)
    target = _resolve(volume, path)
    if not target.exists() and not target.is_symlink():
        raise OfficialError(404, f"Path {path} not found")
    return _entry(target, volume.path)


@router.get("/volumecontent/{volume_id}/dir")
async def volume_list_dir(
    volume_id: str,
    request: Request,
    path: str = Query(...),
    depth: int = Query(default=1),
) -> list[dict[str, Any]]:
    volume = _require_volume(request, volume_id)
    target = _resolve(volume, path)
    if not target.is_dir():
        raise OfficialError(404, f"Path {path} not found")
    entries: list[dict[str, Any]] = []

    def walk(base: Path, level: int) -> None:
        for child in base.iterdir():
            entries.append(_entry(child, volume.path))
            if child.is_dir() and (depth == 0 or level + 1 < depth):
                walk(child, level + 1)

    walk(target, 0)
    entries.sort(key=lambda e: e["path"])
    return entries


@router.post("/volumecontent/{volume_id}/dir", status_code=201)
async def volume_make_dir(
    volume_id: str,
    request: Request,
    path: str = Query(...),
    force: bool | None = Query(default=None),
) -> dict[str, Any]:
    volume = _require_volume(request, volume_id)
    target = _resolve(volume, path)
    if target.exists():
        if force is not False:
            return _entry(target, volume.path)
        raise OfficialError(409, f"Path {path} already exists")
    target.mkdir(parents=force is not False)
    return _entry(target, volume.path)


@router.put("/volumecontent/{volume_id}/file", status_code=201)
async def volume_write_file(
    volume_id: str,
    request: Request,
    path: str = Query(...),
    force: bool | None = Query(default=None),
) -> dict[str, Any]:
    volume = _require_volume(request, volume_id)
    target = _resolve(volume, path)
    if target.exists() and force is False:
        raise OfficialError(409, f"Path {path} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    # E4.2: stream to a sibling temp file and atomically rename so an
    # over-limit upload (413) never leaves a partial file or clobbers the
    # previous content, and the worker memory never holds the whole body.
    limit = limit_bytes_from_mb(request.app.state.settings.max_file_write_mb)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        check_content_length(request, limit)
        await stream_body_to_file(request, tmp, limit)
        os.replace(tmp, target)
    except UploadTooLargeError:
        tmp.unlink(missing_ok=True)
        raise OfficialError(413, "File exceeds maximum upload size")
    except OSError:
        # E4.2 review (Minor): a failed os.replace (e.g. the target is a
        # directory) must not leave the temp file behind or surface as an
        # unhandled 500; mirror templates.py.
        tmp.unlink(missing_ok=True)
        raise OfficialError(500, "Failed to store uploaded file")
    return _entry(target, volume.path)


@router.delete("/volumecontent/{volume_id}/path")
async def volume_delete_path(
    volume_id: str, request: Request, path: str = Query(...)
) -> Response:
    volume = _require_volume(request, volume_id)
    target = _resolve(volume, path)
    if not target.exists() and not target.is_symlink():
        raise OfficialError(404, f"Path {path} not found")
    if target.is_dir() and not target.is_symlink():
        await asyncio.to_thread(shutil.rmtree, target)
    else:
        await asyncio.to_thread(target.unlink)
    return Response(status_code=204)


@router.patch("/volumecontent/{volume_id}/path")
async def volume_rename_path(
    volume_id: str, request: Request, path: str = Query(...)
) -> dict[str, Any]:
    volume = _require_volume(request, volume_id)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    destination = (body or {}).get("path") if isinstance(body, dict) else None
    if not isinstance(destination, str):
        raise OfficialError(400, "body.path is required")
    src = _resolve(volume, path)
    dst = _resolve(volume, destination)
    if not src.exists() and not src.is_symlink():
        raise OfficialError(404, f"Path {path} not found")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return _entry(dst, volume.path)
