"""Local template build endpoints (Dockerfile -> image via Docker daemon)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tarfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import _require_owned, require_api_key, tenant_of, tenant_scope
from control_plane.registry.templates import (
    BuildRecord,
    TemplateRecord,
    UnknownTemplateBuildError,
)
from gateway_common.upload import (
    UploadTooLargeError,
    check_content_length,
    limit_bytes_from_mb,
    stream_body_to_file,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


def _steps_to_dockerfile(from_image: str | None, steps: list[dict]) -> str:
    if not from_image:
        raise ValueError("fromImage is required for template build")
    lines = [f"FROM {from_image}"]
    for step in steps:
        kind = (step.get("type") or "").upper()
        args = step.get("args") or []
        if kind == "RUN":
            lines.append(f"RUN {args[0]}" if args else "RUN true")
        elif kind == "ENV":
            pairs = " ".join(f"{args[i]}={args[i + 1]}" for i in range(0, len(args) - 1, 2))
            lines.append(f"ENV {pairs}" if pairs else "")
        elif kind == "WORKDIR":
            lines.append(f"WORKDIR {args[0]}" if args else "")
        elif kind == "USER":
            lines.append(f"USER {args[0]}" if args else "")
        elif kind == "COPY":
            if len(args) < 2 or not args[0] or not args[1]:
                raise ValueError("COPY steps need a source and a destination")
            src, dest = args[0], args[1]
            opts: list[str] = []
            user = args[2] if len(args) > 2 else ""
            if user:
                opts.append(f"--chown={user}")
            mode = args[3] if len(args) > 3 else ""
            if mode:
                opts.append(f"--chmod={mode}")
            prefix = " ".join(opts) + " " if opts else ""
            lines.append(f"COPY {prefix}{src} {dest}")
        else:
            raise ValueError(f"unsupported template step type: {kind}")
    return "\n".join(lines) + "\n"


def _extract_build_context(build_dir: Path) -> Path:
    """Extract uploaded COPY archives into the docker build context."""
    ctx_dir = build_dir / "ctx"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    archives = build_dir / "archives"
    if not archives.is_dir():
        return ctx_dir
    for archive in sorted(archives.iterdir()):
        # Staging temp files (E3.4 unique-name uploads) are never build
        # context; only published archives are extracted.
        if not archive.is_file() or archive.name.startswith("."):
            continue
        try:
            tar = tarfile.open(archive)
        except tarfile.TarError:
            # The upload endpoint validates archives, so this is a defensive
            # skip for a corrupt/partial file.
            continue
        try:
            members = []
            for member in tar.getmembers():
                # Guard against path traversal from hostile archive content.
                target = (ctx_dir / member.name).resolve()
                if not target.is_relative_to(ctx_dir.resolve()):
                    raise ValueError(f"archive member escapes context: {member.name}")
                # Absolute symlinks cannot be recreated portably in a build
                # context; docker would reject them anyway.
                if member.issym() and os.path.isabs(member.linkname):
                    continue
                members.append(member)
            try:
                tar.extractall(ctx_dir, members=members, filter="data")
            except TypeError:  # pragma: no cover - Python < 3.12
                tar.extractall(ctx_dir, members=members)
        except (tarfile.TarError, ValueError):
            continue
        finally:
            tar.close()
    return ctx_dir


async def _run_buildctl(*args: str) -> tuple[int, str]:
    """Run the buildkit CLI, returning (exit code, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        "buildctl",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


def _write_docker_config(settings: Any) -> None:
    """Write registry credentials for buildctl's push auth.

    buildctl resolves registry credentials from the Docker config file
    (~/.docker/config.json); the buildkit daemon itself has no credential
    config. The password never appears on a command line.
    """
    username = settings.image_registry_username
    password = settings.image_registry_password
    registry = (settings.image_registry or "").rstrip("/")
    if not username or not password or not registry:
        return
    import base64
    import json

    host = registry.split("/")[0]
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    config = {"auths": {host: {"auth": auth}}}
    path = Path.home() / ".docker" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")


async def _run_build(
    app: Any,
    template: TemplateRecord,
    build: BuildRecord,
    dockerfile: str,
    workspace_base: Path,
) -> None:
    build.status = "building"
    build_dir = workspace_base / "_builds" / template.template_id
    build_dir.mkdir(parents=True, exist_ok=True)
    try:
        ctx_dir = _extract_build_context(build_dir)
    except Exception as e:
        build.status = "error"
        build.error = f"failed to prepare build context: {e}"
        build.append_log(build.error)
        return
    # buildctl's dockerfile frontend reads the Dockerfile from a file in a
    # --local dockerfile source (unlike docker build's stdin).
    (ctx_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    settings = app.state.settings
    _write_docker_config(settings)

    registry = (settings.image_registry or "").rstrip("/")
    if registry:
        remote = f"{registry}/{template.template_id}"
        output = f"type=image,name={remote}:latest,push=true"
    else:
        remote = None
        output = f"type=image,name={template.image}"
    build.append_log(f"building template (buildkit: {settings.buildkit_addr})")
    try:
        proc = await asyncio.create_subprocess_exec(
            "buildctl",
            "--addr",
            settings.buildkit_addr,
            "build",
            "--frontend",
            "dockerfile.v0",
            "--local",
            f"context={ctx_dir}",
            "--local",
            f"dockerfile={ctx_dir}",
            "--output",
            output,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        build.status = "error"
        build.error = "buildctl is not available in this image"
        build.append_log(build.error)
        return
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        build.append_log(text)
    code = await proc.wait()
    if code == 0:
        if remote is not None:
            # From here on, sandboxes reference the registry image so worker
            # nodes can pull it via OCI instead of a local daemon.
            template.image = remote
            build.append_log(f"pushed image to {remote}")
        if build.status != "error":
            build.status = "ready"
            build.append_log("Build finished successfully")
    else:
        build.status = "error"
        build.error = f"buildkit build exited with code {code}"
        build.append_log(build.error)


async def _run_build_with_slot(
    app: Any,
    template: TemplateRecord,
    build: BuildRecord,
    dockerfile: str,
    workspace_base: Path,
    release_slot: Any,
) -> None:
    """Run a build and always release its concurrency slot afterwards."""
    try:
        await _run_build(app, template, build, dockerfile, workspace_base)
    except Exception as e:  # defensive: never leave a stuck "building"
        build.status = "error"
        build.error = f"build failed unexpectedly: {e}"
        build.append_log(build.error)
    finally:
        release_slot()


def _acquire_build_slot(request: Request) -> Any:
    """Reserve one build concurrency slot; raises 429 when full."""
    limit = request.app.state.settings.template_build_concurrency
    if limit <= 0:
        return lambda: None
    lock = request.app.state.template_build_slots_lock
    with lock:
        if request.app.state.template_build_slots >= limit:
            raise OfficialError(
                429, "Template build concurrency limit exceeded"
            )
        request.app.state.template_build_slots += 1
        slots = request.app.state.template_build_slots

    def release() -> None:
        with lock:
            request.app.state.template_build_slots = max(
                0, request.app.state.template_build_slots - 1
            )

    return release


@router.post("/v3/templates", status_code=202, dependencies=[Depends(require_api_key)])
async def create_template_build(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    name = (body or {}).get("name") if isinstance(body, dict) else None
    if not name or not isinstance(name, str):
        raise OfficialError(400, "name is required")
    tenant, _is_admin = tenant_of(request)
    record, build = _templates(request).create(name, tenant_id=tenant)
    return {
        "templateID": record.template_id,
        "buildID": build.build_id,
        "public": False,
        "names": [name],
        "tags": [],
        "aliases": [name],
    }


@router.get(
    "/templates/{template_id}/files/{file_hash}",
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def template_file_upload_link(
    template_id: str, file_hash: str, request: Request
) -> dict[str, Any]:
    try:
        record = _templates(request).get(template_id)
    except UnknownTemplateBuildError:
        raise OfficialError(404, f"Template {template_id} not found")
    _require_owned(request, record, resource_id=template_id, label="Template")
    if record.is_file_uploaded(file_hash):
        return {"present": True, "url": None}
    token = record.upload_url_token(file_hash)
    _templates(request).save(record)
    base = str(request.base_url).rstrip("/")
    url = f"{base}/templates/{template_id}/files/{file_hash}/upload?token={token}"
    return {"present": False, "url": url}


@router.put("/templates/{template_id}/files/{file_hash}/upload")
async def template_file_upload(
    template_id: str,
    file_hash: str,
    request: Request,
    token: str = Query(default=""),
) -> Response:
    """Receive a COPY build-context archive.

    The official SDK PUTs the pre-signed URL without an auth header, so the
    endpoint authenticates through a per-file token embedded in the URL.
    """
    try:
        record = _templates(request).get(template_id)
    except UnknownTemplateBuildError:
        raise OfficialError(404, f"Template {template_id} not found")
    # E3.4: once uploaded, the token is cleared and the file cannot be
    # overwritten. A replayed PUT (old URL or a stale retry) is rejected
    # before token verification so the error is unambiguous.
    if record.is_file_uploaded(file_hash):
        raise OfficialError(409, "File already uploaded")
    if not record.verify_upload_token(file_hash, token):
        raise OfficialError(401, "Invalid upload token")
    app = request.app
    workspace_base = app.state.workspace_base
    archives = workspace_base / "_builds" / template_id / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    target = archives / f"{file_hash}.tar.gz"
    # Unique temp file: concurrent PUTs of the same file_hash each stage
    # their own archive and atomically rename it into place, so one upload
    # can never truncate or delete another's in-flight file (E3.4 review
    # I2). The loser of claim_file_upload must not unlink the winner's
    # archive, so only the temp file is ever cleaned up on failure.
    tmp = archives / f".{file_hash}.{uuid.uuid4().hex}.tmp"
    try:
        # E4.2: stream the body to disk (never buffer it in memory), bounded
        # by E2B_MAX_FILE_WRITE_MB; over-limit uploads get 413.
        limit = limit_bytes_from_mb(app.state.settings.max_file_write_mb)
        check_content_length(request, limit)
        size = await stream_body_to_file(request, tmp, limit)
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise OfficialError(400, "Upload body is empty")
        # Validate the payload is a readable tar/gzip archive before it is
        # published under the final name.
        with tarfile.open(tmp) as tar:
            tar.getmembers()
    except UploadTooLargeError:
        tmp.unlink(missing_ok=True)
        raise OfficialError(413, "Uploaded file exceeds maximum size")
    except (OSError, tarfile.TarError):
        tmp.unlink(missing_ok=True)
        raise OfficialError(400, "Uploaded file is not a valid tar archive")
    try:
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise OfficialError(500, "Failed to store uploaded file")
    if not _templates(request).claim_file_upload(template_id, file_hash):
        # A concurrent upload won the race. Our archive was already
        # atomically renamed into place (same file_hash => same content),
        # so unlink here would delete the winner's archive.
        raise OfficialError(409, "File already uploaded")
    return Response(status_code=204)

@router.post(
    "/v2/templates/{template_id}/builds/{build_id}",
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
async def trigger_template_build(
    template_id: str, build_id: str, request: Request
) -> Response:
    try:
        record = _templates(request).get(template_id)
    except UnknownTemplateBuildError:
        raise OfficialError(404, f"Template {template_id} not found")
    # Ownership is checked before build lookup so a cross-tenant template
    # returns the same 404 as a missing one (no existence leak).
    _require_owned(request, record, resource_id=template_id, label="Template")
    try:
        build = record.get_build(build_id)
    except UnknownTemplateBuildError:
        raise OfficialError(404, f"Template build {build_id} not found")
    # E3.5: per-key build rate limit (0 = disabled), then the global
    # concurrency cap. A malicious key cannot flood buildkit with parallel
    # builds; over-capacity triggers are rejected with 429.
    if not request.app.state.template_build_limiter.allow(
        request.headers.get("X-API-Key") or ""
    ):
        raise OfficialError(429, "Template build rate limit exceeded")
    release_slot = _acquire_build_slot(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        release_slot()
        raise OfficialError(400, "Invalid JSON body")
    try:
        dockerfile = _steps_to_dockerfile(
            (body or {}).get("fromImage"), (body or {}).get("steps") or []
        )
    except ValueError as e:
        release_slot()
        build.status = "error"
        build.error = str(e)
        return Response(status_code=202)
    app = request.app
    workspace_base = app.state.workspace_base
    asyncio.create_task(
        _run_build_with_slot(
            app, record, build, dockerfile, workspace_base, release_slot
        )
    )
    return Response(status_code=202)


@router.get(
    "/templates/{template_id}/builds/{build_id}/status",
    dependencies=[Depends(require_api_key)],
)
async def template_build_status(
    template_id: str,
    build_id: str,
    request: Request,
    logsOffset: int = Query(default=0, alias="logsOffset"),
) -> dict[str, Any]:
    try:
        record = _templates(request).get(template_id)
    except UnknownTemplateBuildError:
        raise OfficialError(404, f"Template {template_id} not found")
    # Same 404 unification as trigger_template_build: a cross-tenant
    # template is indistinguishable from a missing one.
    _require_owned(request, record, resource_id=template_id, label="Template")
    try:
        build = record.get_build(build_id)
    except UnknownTemplateBuildError:
        raise OfficialError(404, f"Template build {build_id} not found")
    info = build.as_info(record.template_id)
    info["logs"] = info["logs"][logsOffset:]
    info["logEntries"] = info["logEntries"][logsOffset:]
    return info


@router.get("/templates", dependencies=[Depends(require_api_key)])
async def list_templates(request: Request) -> list[dict[str, Any]]:
    return [
        {"templateID": t.template_id, "name": t.name, "image": t.image}
        for t in _templates(request).list(tenant_id=tenant_scope(request))
    ]
