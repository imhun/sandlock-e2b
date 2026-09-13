"""``/files`` download and upload endpoints."""

from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from envd_service.filesystem.ops import FilesystemOps, _entry
from envd_service.http.auth import HttpAuthError, http_error_response, require_http_sandbox
from gateway_common.errors import invalid_argument, not_found
from gateway_common.paths import PathTraversalError, resolve_under_root
from gateway_common.upload import (
    UploadTooLargeError,
    check_content_length,
    limit_bytes_from_mb,
    stream_body_to_file,
    stream_upload_file_to_file,
)

router = APIRouter()

#: W6: the deployed worker is not root, so it reaches managed entries through
#: its **group** identity. An entry the sandbox itself restricted to
#: ``0600``/``0700`` (or a ``0700`` parent directory) is therefore out of the
#: worker's reach, and that is a permanent property of the deployment shape,
#: not a transient worker fault — so the read answers ``403`` with this
#: reason instead of a ``500`` carrying the raw errno text (which the SDK
#: reports as ``SandboxException("500: [Errno 13] Permission denied: ...")``
#: — the shape the F1 probe measured on the non-root stack, i.e. a platform
#: fault to the caller's eyes, with nothing to act on). Root workers are
#: unaffected (``CAP_DAC_OVERRIDE``); the remedy is to read the entry from
#: inside the sandbox or to relax that entry's mode.
SANDBOX_PRIVATE_ENTRY_REASON = (
    "the sandbox made this entry private (0600/0700, or a 0700 parent "
    "directory): the worker reads sandbox files with its group identity, so "
    "the entry is out of its reach; read it from inside the sandbox or relax "
    "that entry's mode"
)


def _read_error(path: str, exc: OSError) -> HttpAuthError:
    """Map a failed read/stat onto the answer the client can act on (W6).

    ``ENOENT``/``ENOTDIR`` stay ``404`` (the path is not there), a permission
    failure becomes ``403`` naming the sandbox-private cause, and everything
    else remains a worker-side ``500``.
    """
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return HttpAuthError(404, f"Path {path} not found")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return HttpAuthError(
            403,
            f"Path {path} is not readable: {SANDBOX_PRIVATE_ENTRY_REASON} "
            f"({exc})",
        )
    return HttpAuthError(500, str(exc))


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


def _write_limit(request: Request) -> int | None:
    return limit_bytes_from_mb(request.app.state.settings.max_file_write_mb)


@router.get("/files")
async def download_file(
    request: Request, path: str, username: str | None = None
) -> Response:
    runtime = require_http_sandbox(request)
    ops = FilesystemOps(runtime.workspace_dir)
    try:
        target = _resolve_or_error(ops, path)
        try:
            # ``stat`` first so the mapping can tell "not there" (404) from
            # "there but out of the worker's reach" (403): ``is_file()``
            # re-raises EACCES (pathlib only ignores ENOENT/ENOTDIR/E*LOOP),
            # so an unsearchable 0700 parent used to surface as a bare 500
            # just like the read denial did.
            entry = target.stat()
        except OSError as exc:
            raise _read_error(path, exc) from exc
        if not stat.S_ISREG(entry.st_mode):
            raise HttpAuthError(404, f"Path {path} not found")
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise _read_error(path, exc) from exc
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
            target = _resolve_or_error(ops, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            # E4.2: stream to a sibling temp file and atomically rename so an
            # over-limit upload (413) leaves no partial file and the worker
            # memory does not grow with the body.
            limit = _write_limit(request)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                check_content_length(request, limit)
                await stream_body_to_file(request, tmp, limit)
                os.replace(tmp, target)
            except UploadTooLargeError:
                tmp.unlink(missing_ok=True)
                raise HttpAuthError(413, "File exceeds maximum upload size")
            _persist_metadata(target, metadata)
            return JSONResponse(content=[_upload_response(ops, target, metadata)])

        limit = _write_limit(request)
        try:
            # E4.2 review (Important): reject an oversized multipart request
            # from its Content-Length before request.form() consumes the whole
            # body (Starlette spools parts >1MB to disk), so the limit also
            # covers the receive phase, not just the per-part write phase.
            check_content_length(request, limit)
            form = await request.form()
        except UploadTooLargeError:
            raise HttpAuthError(413, "File exceeds maximum upload size")
        files = [item for item in form.multi_items() if item[0] == "file"]
        if not files:
            raise HttpAuthError(400, "no file parts in multipart upload")
        results: list[dict[str, Any]] = []
        for _, file_obj in files:
            assert isinstance(file_obj, UploadFile)
            file_path = path or (file_obj.filename or "")
            if not file_path:
                raise HttpAuthError(400, "file path is required")
            target = _resolve_or_error(ops, file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                await stream_upload_file_to_file(file_obj, tmp, limit)
                os.replace(tmp, target)
            except UploadTooLargeError:
                tmp.unlink(missing_ok=True)
                raise HttpAuthError(413, "File exceeds maximum upload size")
            _persist_metadata(target, metadata)
            results.append(_upload_response(ops, target, metadata))
        return JSONResponse(content=results)
    except HttpAuthError as e:
        return http_error_response(request, e)
    except Exception as e:
        return http_error_response(request, HttpAuthError(500, str(e)))
