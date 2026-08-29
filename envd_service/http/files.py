"""``/files`` download and upload endpoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from envd_service.filesystem.ops import FilesystemOps, _entry
from envd_service.http.auth import HttpAuthError, http_error_response, require_http_sandbox
from gateway_common.errors import invalid_argument, not_found
from gateway_common.paths import PathTraversalError, resolve_under_root

router = APIRouter()


def _resolve_or_error(ops: FilesystemOps, path: str) -> Path:
    try:
        return resolve_under_root(ops.root, path)
    except PathTraversalError as e:
        raise HttpAuthError(400, str(e)) from e


def _metadata_from_headers(request: Request) -> dict[str, str]:
    metadata: dict[str, str] = {}
    prefix = "x-metadata-"
    for key, value in request.headers.items():
        if key.lower().startswith(prefix):
            metadata[key[len(prefix) :].lower()] = value
    return metadata


def _persist_metadata(path: Path, metadata: dict[str, str]) -> None:
    for key, value in metadata.items():
        try:
            os.setxattr(path, f"user.e2b.{key}", value.encode("utf-8"))
        except (OSError, AttributeError):
            pass


def _read_metadata(path: Path) -> dict[str, str] | None:
    metadata: dict[str, str] = {}
    try:
        names = os.listxattr(path)
    except (OSError, AttributeError):
        return None
    for name in names:
        if name.startswith("user.e2b."):
            try:
                metadata[name[len("user.e2b.") :]] = os.getxattr(
                    path, name
                ).decode("utf-8", "replace")
            except OSError:
                pass
    return metadata or None


def _upload_response(ops: FilesystemOps, path: Path, metadata: dict[str, str]) -> dict[str, Any]:
    return {
        "name": path.name,
        "type": "file",
        "path": path.relative_to(ops.root).as_posix(),
        "metadata": metadata or None,
    }


@router.get("/files")
async def download_file(
    request: Request, path: str, username: str | None = None
) -> Response:
    runtime = require_http_sandbox(request)
    ops = FilesystemOps(runtime.workspace_dir)
    try:
        target = _resolve_or_error(ops, path)
        if not target.is_file():
            raise HttpAuthError(404, f"Path {path} not found")
        data = target.read_bytes()
    except HttpAuthError as e:
        return http_error_response(request, e)
    except OSError as e:
        return http_error_response(request, HttpAuthError(500, str(e)))
    return Response(
        content=data,
        media_type="application/octet-stream",
    )


@router.post("/files")
async def upload_file(
    request: Request,
    path: str | None = None,
) -> Response:
    runtime = require_http_sandbox(request)
    ops = FilesystemOps(runtime.workspace_dir)
    metadata = _metadata_from_headers(request)
    content_type = (request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()

    try:
        if content_type == "application/octet-stream":
            if not path:
                raise HttpAuthError(400, "path is required for octet-stream uploads")
            body = await request.body()
            target = _resolve_or_error(ops, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            _persist_metadata(target, metadata)
            return JSONResponse(content=[_upload_response(ops, target, metadata)])

        form = await request.form()
        files = [item for item in form.multi_items() if item[0] == "file"]
        if not files:
            raise HttpAuthError(400, "no file parts in multipart upload")
        results: list[dict[str, Any]] = []
        for _, file_obj in files:
            assert isinstance(file_obj, UploadFile)
            data = await file_obj.read()
            file_path = path or (file_obj.filename or "")
            if not file_path:
                raise HttpAuthError(400, "file path is required")
            target = _resolve_or_error(ops, file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            _persist_metadata(target, metadata)
            results.append(_upload_response(ops, target, metadata))
        return JSONResponse(content=results)
    except HttpAuthError as e:
        return http_error_response(request, e)
    except Exception as e:
        return http_error_response(request, HttpAuthError(500, str(e)))
