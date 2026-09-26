"""Sandbox fork and snapshot endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

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
from gateway_common.paths import validate_sandbox_id
from gateway_common.upload import UploadTooLargeError, read_json_body

logger = logging.getLogger(__name__)

router = APIRouter()

#: One lock per snapshot id, in this process (N29).
#:
#: The copy is synchronous, so the entry proxy can time out on a large tree
#: while the control plane keeps working -- which is exactly when the client's
#: retry arrives, *during* the first copy. Serializing on the id makes that
#: retry wait for the first attempt and then answer with its record, instead of
#: starting a second full copy. A second control-plane replica would need this
#: in the shared store, and it has it since F11 step 3: `try_acquire_copy`
#: takes a TTL'd claim in Redis for a *named* id, and the record's ``creating``
#: status (on the shared volume) is what a loser waits on. This dict stays for
#: the single-process deployment and for the unnamed case, where every request
#: already has an id of its own.
_SNAPSHOT_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(snapshot_id: str) -> asyncio.Lock:
    lock = _SNAPSHOT_LOCKS.get(snapshot_id)
    if lock is None:
        lock = _SNAPSHOT_LOCKS[snapshot_id] = asyncio.Lock()
    return lock


def _existing_snapshot(request: Request, snapshot_id: str):
    """The record for ``snapshot_id`` when it is an *answer*, else ``None``.

    Two states answer a retry: ``completed`` (here it is) and ``creating`` (the
    copy is still running -- the retry must not start a second one). A
    ``failed`` record is deliberately *not* an answer: retrying the same key
    should try again rather than hand the caller back a failure it already
    knows about.
    """
    try:
        record = _snapshots(request).get(snapshot_id)
    except UnknownSnapshotError:
        return None
    return None if record.status == "failed" else record


async def _await_snapshot_record(
    request: Request, snapshot_id: str, *, timeout_s: float = 2.0
):
    """Wait for the record the replica that owns ``snapshot_id`` writes.

    The claim and the record are written by the same replica, so this is a
    short poll rather than a long wait: a copy can run for minutes (N32
    measured 76 s for 2000 files), and the caller behind an entry proxy has its
    own timeout. Finding the record is what makes the loser answer exactly like
    a local retry (``alreadyExists``, 202 while the copy is still running).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        existing = _existing_snapshot(request, snapshot_id)
        if existing is not None:
            return existing
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


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


def _capture_snapshot(
    request: Request,
    sandbox_id: str,
    name: str | None,
    *,
    snapshot_id: str | None = None,
):
    """Freeze the sandbox, copy its filesystem, thaw it.

    ``snapshot_id`` is the caller's idempotency key (N29): when the request
    carries one, the copy lands under exactly that id, so a retry either finds
    the finished payload (the worker answers "completed") or waits for the
    attempt already in flight -- never a second copy of the same tree.
    """
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
        snapshot_id = snapshot_id or new_sandbox_id().replace("sbx_", "snap_")
        if node.address == "local://":
            # A retried id whose payload is already on disk must not copy
            # again -- the local shape has no worker route to answer
            # "completed", so the filesystem is the answer (N29).
            payload_there = _snapshots(request).payload_path(snapshot_id).exists()
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
                snapshot_id=snapshot_id,
                copy_fs=not payload_there,
                tenant_id=record.tenant_id,
                source_sandbox_id=sandbox_id,
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
        if resp.status_code == 409:
            # The id exists but its payload is not a finished snapshot: say so
            # instead of dressing it up as "the node failed" (N29).
            raise OfficialError(
                409,
                f"Snapshot {snapshot_id} already exists on node "
                f"{node.node_id} but is not complete",
            )
        if resp.status_code not in (200, 201):
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
            source_sandbox_id=sandbox_id,
        )
    finally:
        request.app.state.runtime_registry.thaw(sandbox_id)


def _copy_local_payload(state, workspace_dir: str | Path, snapshot_id: str) -> None:
    """Copy a sandbox tree into a snapshot's payload dir (the local shape).

    The same guard `SnapshotRegistry.create_from_sandbox` applies when it
    copies: a payload that would land inside its own source is refused rather
    than copied into itself (the G2 self-nesting incident).
    """
    payload = state.snapshots.payload_path(snapshot_id)
    src = Path(workspace_dir).resolve()
    dst = Path(payload).resolve()
    try:
        dst.relative_to(src)
    except ValueError:
        pass
    else:
        raise ValueError(f"snapshot destination {dst} is inside its source {src}")
    if payload.exists():
        # A retried id whose payload is already on disk must not copy again
        # (N29): the filesystem is the local shape's "already completed" answer.
        return
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=False)


def _capture_reserved(
    state, snapshot_id: str, sandbox_id: str, name: str | None
) -> None:
    """Run the copy for a snapshot whose id was already reserved (N29 async).

    Off the request path and off the event loop: the caller answered 202 long
    before this runs. Moves no metadata -- the record exists -- and ends by
    flipping the status, so a poll sees ``creating`` until the bytes are in
    place. Raises for the caller to translate into ``mark_failed``.
    """
    registry = state.registry
    record = registry.get(sandbox_id)
    node = state.nodes.get(record.node_id or "local")
    if node is None:
        raise RuntimeError(f"Node {record.node_id} not found")
    state.runtime_registry.freeze(sandbox_id)
    try:
        if node.address == "local://":
            _copy_local_payload(state, record.workspace_dir, snapshot_id)
        else:
            import httpx

            resp = httpx.post(
                f"{node.address}/agent/snapshots",
                json={"snapshotID": snapshot_id, "sandboxID": sandbox_id},
                headers={"X-Internal-Key": state.settings.internal_api_key},
                timeout=120,
            )
            if resp.status_code == 409:
                raise RuntimeError(
                    f"snapshot {snapshot_id} already exists on node "
                    f"{node.node_id} but is not complete"
                )
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"node {node.node_id} failed to snapshot")
        state.snapshots.mark_completed(snapshot_id)
    finally:
        state.runtime_registry.thaw(sandbox_id)


@router.post(
    "/sandboxes/{sandbox_id}/snapshots",
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def create_snapshot(
    sandbox_id: str, request: Request
) -> dict[str, Any]:
    """Snapshot a sandbox, idempotently when the caller names the request.

    N29: this copy is synchronous and can outlive the entry proxy's read
    timeout on a large tree. The client's retry then arrives while the first
    attempt is still running, and the two things it must never do are start a
    second copy and come back as "failed" for work that succeeded. So:

    * ``Idempotency-Key`` (or ``snapshotID`` in the body) names the request.
      The same key means the same snapshot -- if its record exists, it is
      returned with ``200`` and ``alreadyExists: true`` and nothing is copied;
      if a copy for it is in flight, this request waits for that one and
      answers with its record.
    * without a key the behaviour is unchanged (a fresh id per request), which
      is what the e2b SDK does: it sends only ``name``, so a retry from it is a
      *new* snapshot. Callers that retry should either send a key or list
      snapshots first; ``docs/k8s-deployment.md`` §22.5.14 says so.
    """
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

    requested_id = request.headers.get("Idempotency-Key") or (
        body.get("snapshotID") if isinstance(body, dict) else None
    )
    if requested_id is not None:
        if not isinstance(requested_id, str) or not validate_sandbox_id(requested_id):
            raise OfficialError(400, "snapshotID/Idempotency-Key is not a valid id")
        existing = _existing_snapshot(request, requested_id)
        if existing is not None:
            return _already_exists(existing)
        # F11 step 3: a *named* id is fleet-wide. Take the shared claim before
        # the per-process lock, so two replicas cannot both look, both miss and
        # both copy the same tree into the same directory. The loser waits for
        # the winner's record (the copy answers with it, exactly like a local
        # retry) and otherwise says who holds it.
        if not _snapshots(request).try_acquire_copy(requested_id):
            existing = await _await_snapshot_record(request, requested_id)
            if existing is not None:
                return _already_exists(existing)
            raise OfficialError(
                409,
                f"snapshot {requested_id} is being copied by another "
                "control-plane replica",
            )
    claimed = requested_id is not None

    try:
        async with _lock_for(requested_id or sandbox_id):
            # Re-checked inside the lock: the attempt we waited behind may just
            # have written its record.
            if requested_id is not None:
                existing = _existing_snapshot(request, requested_id)
                if existing is not None:
                    return _already_exists(existing)
            if _wants_async(request):
                return await _create_snapshot_async(
                    request, sandbox_id, name, requested_id
                )
            try:
                # N32: the capture talks to the worker with a *synchronous* HTTP
                # call (the copy is synchronous there too) and can take as long as
                # the tree is big -- measured 76 s for 2000 files. Running it on
                # this thread used to block the whole event loop for exactly that
                # long: the access log went silent for 76.1 s, so the workers'
                # *arrival* timestamps for heartbeats were one copy old, and the
                # node-health sweep then orphaned live nodes ("node health sweep:
                # orphaned sandboxes on e2b-worker-1", 2026-09-22 on the k0s
                # cluster). Everything the loop serves -- facts that decide whether
                # a node is alive -- has to keep flowing while a copy runs, so the
                # capture goes to a worker thread.
                record = await asyncio.to_thread(
                    _capture_snapshot,
                    request,
                    sandbox_id,
                    name,
                    snapshot_id=requested_id,
                )
            except UnknownSandboxError:
                raise OfficialError(404, f"Sandbox {sandbox_id} not found")
            except SandboxStateConflictError:
                raise OfficialError(409, f"Sandbox {sandbox_id} is not running")
    finally:
        if claimed:
            # Released once the *record* exists (async: it is written before
            # the 202) or the copy is done: from then on the record's own
            # ``creating`` status is what any other replica waits on.
            _snapshots(request).release_copy(requested_id)
    logger.info("snapshot %s captured from sandbox %s", record.snapshot_id, sandbox_id)
    return record.as_snapshot_info()


def _already_exists(record) -> JSONResponse:
    """The answer to a retried snapshot request: the one it already made.

    ``status`` is the record's real state, not a hopeful ``completed``: a
    retry that lands while an async copy is still running has to say
    ``creating`` (and answer 202), or the caller would treat a half-copied
    snapshot as usable. For the sync shape the record is always finished by
    the time the per-id lock is released, so the body is the one N29 pinned.
    """
    still_copying = record.status == "creating"
    return JSONResponse(
        status_code=202 if still_copying else 200,
        content={
            **record.as_snapshot_info(),
            "status": record.status,
            **({"error": record.error} if record.error else {}),
            "alreadyExists": True,
        },
    )


#: Background copies, held so the event loop cannot garbage-collect a running
#: task (the classic "task disappeared mid-flight" trap).
_PENDING_CAPTURES: set[asyncio.Task] = set()


def _wants_async(request: Request) -> bool:
    """Did the caller ask for the long task to be answered out of band?

    Two spellings, because the two audiences write it differently: an HTTP
    client says ``Prefer: respond-async`` (the RFC 7240 preference), a shell
    or CLI says ``?async=1``. The sync shape stays the default so the e2b SDK
    -- which sends only a name and expects the finished snapshot in the
    response -- keeps working unchanged.
    """
    prefer = request.headers.get("Prefer", "")
    if "respond-async" in prefer:
        return True
    return request.query_params.get("async", "").lower() in {"1", "true", "yes"}


async def _create_snapshot_async(
    request: Request,
    sandbox_id: str,
    name: str | None,
    requested_id: str | None,
) -> JSONResponse:
    """Reserve the id, answer 202, and copy in the background (N29 ①).

    The synchronous shape is what makes a big tree exceed the entry's read
    timeout: a 2000-file copy is ~75 s, the entry cuts at 60 s, and the client
    cannot tell "failed" from "still running" (measured twice). This answers
    immediately with the id, and the copy reports through the record's status,
    which the caller polls with ``GET /snapshots/{id}``.

    The checks that decide *whether* a snapshot may be taken stay on the
    request path, so a 202 always means "accepted, copying"; only the bytes
    move later. The reservation is inside the same per-id lock as the sync
    path, so the one-id-one-live-copy rule is unchanged.
    """
    registry = _registry(request)
    record = registry.get(sandbox_id)
    _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    if record.state != "running":
        raise OfficialError(409, f"Sandbox {sandbox_id} is not running")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        raise OfficialError(502, f"Node {record.node_id} not found")
    reserved = _snapshots(request).reserve_from_sandbox(
        template_id=record.template_id,
        env_vars=record.env_vars,
        metadata=record.metadata,
        volume_mounts=[
            {"name": m["name"], "path": m["path"]} for m in record.volume_mounts
        ],
        base_image=record.base_image,
        allow_internet_access=record.allow_internet_access,
        source_sandbox_id=sandbox_id,
        node_id=node.node_id,
        name=name,
        snapshot_id=requested_id,
        tenant_id=record.tenant_id,
    )
    task = asyncio.create_task(
        _run_reserved_capture(
            request.app, reserved.snapshot_id, sandbox_id, name
        )
    )
    _PENDING_CAPTURES.add(task)
    task.add_done_callback(_PENDING_CAPTURES.discard)
    return JSONResponse(status_code=202, content=reserved.as_snapshot_status())


async def _run_reserved_capture(
    app, snapshot_id: str, sandbox_id: str, name: str | None
) -> None:
    """Drive one reserved capture and publish how it ended."""
    try:
        await asyncio.to_thread(
            _capture_reserved, app.state, snapshot_id, sandbox_id, name
        )
    except Exception as exc:  # noqa: BLE001 - the status *is* the error channel
        logging.getLogger(__name__).warning(
            "async snapshot %s failed: %s", snapshot_id, exc
        )
        await asyncio.to_thread(
            app.state.snapshots.mark_failed, snapshot_id, str(exc)
        )


async def reconcile_pending_snapshots(app) -> int:
    """Resolve every copy that was in flight when this process stopped.

    A reserved capture lives in an in-process task, so a restart (or a crash)
    leaves its record at ``creating`` forever and a poller would wait on a copy
    nobody is running. One pass at startup settles each of them: the worker's
    ``POST /agent/snapshots`` is idempotent (a finished payload answers
    ``alreadyExists``), so a copy that *did* finish is simply recorded as
    completed, and anything else is marked failed with the reason.

    Returns how many records were resolved, for the log line.
    """
    log = logging.getLogger(__name__)
    resolved = 0
    for record in list(app.state.snapshots.in_progress()):
        name = record.names[0] if record.names else None
        try:
            if not record.sandbox_id:
                raise RuntimeError(
                    "interrupted by a restart and the source sandbox was not "
                    "recorded; make a new snapshot"
                )
            await asyncio.to_thread(
                _capture_reserved,
                app.state,
                record.snapshot_id,
                record.sandbox_id,
                name,
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the record
            detail = f"interrupted by a restart: {exc}"
            await asyncio.to_thread(
                app.state.snapshots.mark_failed, record.snapshot_id, detail
            )
            log.warning("snapshot %s: %s", record.snapshot_id, detail)
        resolved += 1
    if resolved:
        log.info("reconciled %d in-flight snapshot(s) at startup", resolved)
    return resolved


@router.get(
    "/snapshots/{snapshot_id}", dependencies=[Depends(require_api_key)]
)
async def get_snapshot(snapshot_id: str, request: Request) -> dict[str, Any]:
    """Poll one snapshot: the async shape's completion signal (N29 ①).

    Always carries ``status`` (``creating``/``completed``/``failed``), so a
    caller never has to infer progress from a missing field, and ``error``
    when the copy failed.
    """
    try:
        record = _snapshots(request).get(snapshot_id)
    except UnknownSnapshotError:
        raise OfficialError(404, f"Snapshot {snapshot_id} not found")
    _require_owned(request, record, resource_id=snapshot_id, label="Snapshot")
    return record.as_snapshot_status()


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
        # The payload lives on the shared NAS tree (the control plane and the
        # worker mount the same `_snapshots`), so this delete is an `rmtree`
        # over a whole sandbox tree: measured 17.1 s for 2000 files, and it
        # used to run here on the event loop -- the access log stopped for
        # exactly that long, which is the same "the control plane thinks its
        # own silence is a dead node" shape the capture had (N32). Off the
        # loop, like the capture.
        await asyncio.to_thread(_snapshots(request).delete, snapshot_id)
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
        # Same offload as `create_snapshot`: a fork captures first, and that
        # capture is the long synchronous worker call (N32).
        snapshot = await asyncio.to_thread(
            _capture_snapshot, request, sandbox_id, name=None
        )
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
