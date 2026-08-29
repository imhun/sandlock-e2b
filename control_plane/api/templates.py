"""Local template build endpoints (Dockerfile -> image via Docker daemon)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tarfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import require_api_key
from control_plane.registry.templates import (
    BuildRecord,
    TemplateRecord,
    UnknownTemplateBuildError,
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
        if not archive.is_file():
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


async def _run_docker(*args: str) -> tuple[int, str]:
    """Run a docker CLI command, returning (exit code, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _ensure_registry_login(app: Any, registry: str) -> str | None:
    """Log the daemon into the registry when credentials are configured.

    The password is passed via stdin (``--password-stdin``) so it never
    appears in the docker CLI argument list. Returns an error message on
    failure, else ``None``.
    """
    settings = app.state.settings
    username = settings.image_registry_username
    password = settings.image_registry_password
    if not username or not password:
        return None
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "login",
        registry,
        "-u",
        username,
        "--password-stdin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(password.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    out = await proc.stdout.read()
    code = await proc.wait()
    if code != 0:
        return out.decode("utf-8", "replace").strip()[-500:]
    return None


async def _push_template_image(
    app: Any, template: TemplateRecord, build: BuildRecord
) -> None:
    """Tag and push the locally built image to the configured registry."""
    registry = (app.state.settings.image_registry or "").rstrip("/")
    if not registry:
        return
    remote = f"{registry}/{template.template_id}"
    build.append_log(f"pushing image to {remote}")
    login_error = await _ensure_registry_login(app, registry)
    if login_error:
        build.status = "error"
        build.error = f"docker login to {registry} failed: {login_error}"
        return
    code, output = await _run_docker("tag", template.image, remote)
    if code != 0:
        build.status = "error"
        build.error = f"docker tag failed: {output.strip()[-500:]}"
        return
    code, output = await _run_docker("push", remote)
    if code != 0:
        build.status = "error"
        build.error = f"docker push failed: {output.strip()[-500:]}"
        return
    # From here on, sandboxes reference the registry image so worker nodes
    # can pull it instead of depending on the control plane's local daemon.
    template.image = remote
    build.append_log(f"pushed image to {remote}")


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
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "build",
            "-t",
            template.image,
            "-f",
            "-",
            ".",
            cwd=str(ctx_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        build.status = "error"
        build.error = "docker daemon is not available on this host"
        build.append_log(build.error)
        return
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(dockerfile.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        build.append_log(text)
    code = await proc.wait()
    if code == 0:
        await _push_template_image(app, template, build)
        if build.status != "error":
            build.status = "ready"
            build.append_log("Build finished successfully")
    else:
        build.status = "error"
        build.error = f"docker build exited with code {code}"
        build.append_log(build.error)


@router.post("/v3/templates", status_code=202, dependencies=[Depends(require_api_key)])
async def create_template_build(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    name = (body or {}).get("name") if isinstance(body, dict) else None
    if not name or not isinstance(name, str):
        raise OfficialError(400, "name is required")
    record, build = _templates(request).create(name)
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
    if record.is_file_uploaded(file_hash):
        return {"present": True, "url": None}
    token = record.upload_url_token(file_hash)
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
    if not record.verify_upload_token(file_hash, token):
        raise OfficialError(401, "Invalid upload token")
    body = await request.body()
    if not body:
        raise OfficialError(400, "Upload body is empty")
    app = request.app
    workspace_base = app.state.workspace_base
    archives = workspace_base / "_builds" / template_id / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    target = archives / f"{file_hash}.tar.gz"
    try:
        with open(target, "wb") as f:
            f.write(body)
        # Validate the payload is a readable tar/gzip archive before caching.
        with tarfile.open(target) as tar:
            tar.getmembers()
    except (OSError, tarfile.TarError):
        target.unlink(missing_ok=True)
        raise OfficialError(400, "Uploaded file is not a valid tar archive")
    record.mark_file_uploaded(file_hash)
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
        build = record.get_build(build_id)
        body = await request.json()
    except UnknownTemplateBuildError:
        raise OfficialError(404, f"Template build {build_id} not found")
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    try:
        dockerfile = _steps_to_dockerfile(
            (body or {}).get("fromImage"), (body or {}).get("steps") or []
        )
    except ValueError as e:
        build.status = "error"
        build.error = str(e)
        return Response(status_code=202)
    app = request.app
    workspace_base = app.state.workspace_base
    asyncio.create_task(_run_build(app, record, build, dockerfile, workspace_base))
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
        for t in _templates(request).list()
    ]
