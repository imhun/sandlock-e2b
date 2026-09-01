"""NFS server-side quota-agent HTTP service (E2.6).

Small FastAPI service deployed on the NFS server (or next to it with the
shared XFS filesystem bind-mounted). Workers that only see an NFS client
mount delegate every ``xfs_quota`` operation here; the agent resolves the
server-side filesystem and runs the quota commands locally.

Request surface (all responses JSON objects):

- ``GET /detect?mount=...`` -> server-side detection facts
  (``fs_type`` / ``projid32bit`` / ``prjquota`` / ``xfs_quota``) or
  ``{"error": reason}``.
- ``POST /project_create`` ``{"projid", "path", "limit_mb", "mount"}``
  -> ``{"projid": int}``; on failure ``500 {"error": ...}``.
- ``POST /project_delete`` ``{"projid", "path", "mount"}``
  -> ``{"deleted": int}``.
- ``GET /report?mount=...`` -> ``{"projects": {projid: {"used_blocks",
  "soft_blocks", "hard_blocks"}}}``.
- ``POST /reconcile`` ``{"workspace_base", "mount"}`` -> ``{"cleaned":
  [projid], "skipped": [{"projid", "reason"}]}``.

Auth: every request must carry ``X-Internal-Key`` equal to
``E2B_QUOTA_AGENT_TOKEN`` (constant-time compare); the service refuses to
answer when the token is unconfigured, so an accidentally exposed port
cannot be abused.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import envd_service.xfs_quota as xfs_quota
from deploy.quota_agent.config import Settings

_PROJID_MAX = xfs_quota._PROJID_MAX


class ProjectCreateBody(BaseModel):
    projid: int = Field(ge=1, le=_PROJID_MAX)
    path: str = Field(min_length=1)
    limit_mb: int = Field(gt=0)
    mount: str = Field(min_length=1)


class ProjectDeleteBody(BaseModel):
    projid: int = Field(ge=1, le=_PROJID_MAX)
    path: str = Field(min_length=1)
    mount: str = Field(min_length=1)


class ReconcileBody(BaseModel):
    workspace_base: str = Field(min_length=1)
    mount: str = Field(min_length=1)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_key(request: Request) -> None:
    settings = _settings(request)
    if not settings.token:
        raise HTTPException(
            status_code=500, detail={"error": "quota-agent token not configured"}
        )
    provided = request.headers.get("X-Internal-Key")
    if provided is None or not secrets.compare_digest(provided, settings.token):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def _rewrite_path(path: str, settings: Settings) -> str:
    """Map a client-side path to the server-side path (identity by default)."""
    for client_prefix, server_prefix in settings.path_map:
        if path == client_prefix or path.startswith(
            client_prefix.rstrip("/") + "/"
        ):
            return server_prefix + path[len(client_prefix) :]
    return path


def _quota_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=500, detail={"error": str(exc)})


def create_app(*, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="E2B Sandlock Quota Agent", version="0.1.0")
    app.state.settings = settings

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    @app.get("/detect")
    def detect(
        request: Request, mount: str = Query(min_length=1)
    ) -> dict[str, Any]:
        _require_key(request)
        return xfs_quota._local_facts(_rewrite_path(mount, settings))

    @app.post("/project_create")
    def project_create(body: ProjectCreateBody, request: Request) -> dict[str, Any]:
        _require_key(request)
        path = _rewrite_path(body.path, settings)
        mount = _rewrite_path(body.mount, settings)
        try:
            projid = xfs_quota.provision_project(
                sandbox_id=f"quota-agent:{body.projid}",
                project_dir=path,
                mount_point=mount,
                disk_mb=body.limit_mb,
                project_id=body.projid,
            )
        except xfs_quota.ProjectQuotaError as exc:
            raise _quota_error(exc) from exc
        return {"projid": projid}

    @app.post("/project_delete")
    def project_delete(body: ProjectDeleteBody, request: Request) -> dict[str, Any]:
        _require_key(request)
        path = _rewrite_path(body.path, settings)
        mount = _rewrite_path(body.mount, settings)
        try:
            xfs_quota.release_project(
                project_dir=path,
                mount_point=mount,
                projid=body.projid,
            )
        except xfs_quota.ProjectQuotaError as exc:
            raise _quota_error(exc) from exc
        return {"deleted": body.projid}

    @app.get("/report")
    def report(
        request: Request, mount: str = Query(min_length=1)
    ) -> dict[str, Any]:
        _require_key(request)
        try:
            table = xfs_quota.project_quota_table(_rewrite_path(mount, settings))
        except xfs_quota.ProjectQuotaError as exc:
            raise _quota_error(exc) from exc
        return {
            "projects": {
                str(projid): {
                    "used_blocks": usage.used_blocks,
                    "soft_blocks": usage.soft_blocks,
                    "hard_blocks": usage.hard_blocks,
                }
                for projid, usage in table.items()
            }
        }

    @app.post("/reconcile")
    def reconcile(body: ReconcileBody, request: Request) -> dict[str, Any]:
        _require_key(request)
        try:
            return xfs_quota._local_reconcile(
                _rewrite_path(body.workspace_base, settings),
                _rewrite_path(body.mount, settings),
            )
        except xfs_quota.ProjectQuotaError as exc:
            raise _quota_error(exc) from exc

    return app
