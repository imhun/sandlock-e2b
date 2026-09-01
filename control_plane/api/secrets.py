"""Secret CRUD endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import _require_owned, require_api_key, tenant_of, tenant_scope
from control_plane.registry.secrets import UnknownSecretError

router = APIRouter()


def _secrets(request: Request):
    return request.app.state.secrets


@router.post("/secrets", status_code=201, dependencies=[Depends(require_api_key)])
async def create_secret(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    tenant, _is_admin = tenant_of(request)
    try:
        record = _secrets(request).create(
            body.get("name"),
            body.get("value"),
            body.get("metadata"),
            tenant_id=tenant,
        )
    except ValueError as e:
        raise OfficialError(400, str(e))
    return record.as_model()


@router.get("/secrets", dependencies=[Depends(require_api_key)])
async def list_secrets(
    request: Request,
    response: Response,
    nextToken: str | None = Query(default=None, alias="nextToken"),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict[str, Any]]:
    offset = int(nextToken) if nextToken and nextToken.isdigit() else 0
    records = _secrets(request).list(
        limit=limit, offset=offset, tenant_id=tenant_scope(request)
    )
    total = len(_secrets(request).list(tenant_id=tenant_scope(request)))
    if offset + len(records) < total:
        response.headers["X-Next-Token"] = str(offset + len(records))
    return [r.as_model() for r in records]


@router.get("/secrets/{secret_id}", dependencies=[Depends(require_api_key)])
async def get_secret(secret_id: str, request: Request) -> dict[str, Any]:
    try:
        record = _secrets(request).get(secret_id)
        _require_owned(request, record, resource_id=secret_id, label="Secret")
        return record.as_model()
    except UnknownSecretError:
        raise OfficialError(404, f"Secret {secret_id} not found")


@router.post("/secrets/{secret_id}", dependencies=[Depends(require_api_key)])
async def update_secret(secret_id: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    try:
        record = _secrets(request).get(secret_id)
        _require_owned(request, record, resource_id=secret_id, label="Secret")
        record = _secrets(request).update(
            secret_id, body.get("value"), body.get("metadata")
        )
    except UnknownSecretError:
        raise OfficialError(404, f"Secret {secret_id} not found")
    return record.as_model()


@router.delete(
    "/secrets/{secret_id}", status_code=204, dependencies=[Depends(require_api_key)]
)
async def delete_secret(secret_id: str, request: Request) -> Response:
    try:
        record = _secrets(request).get(secret_id)
        _require_owned(request, record, resource_id=secret_id, label="Secret")
        _secrets(request).delete(secret_id)
    except UnknownSecretError:
        raise OfficialError(404, f"Secret {secret_id} not found")
    return Response(status_code=204)
