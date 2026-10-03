"""Sandbox fork and snapshot endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from control_plane.api.errors import OfficialError
from control_plane.auth import _require_owned, require_api_key, tenant_of, tenant_scope
from control_plane.ratelimit import enforce_resource_limit
from control_plane.api.sandboxes import (
    _non_shared_volume_node_id,
    _provision_local,
    _provision_remote,
)
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxStateConflictError,
    UnknownSandboxError,
)
from control_plane.registry.snapshots import (
    COPY_LEASE_REFRESH_S,
    COPY_LEASE_TTL_S,
    UnknownSnapshotError,
)
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


#: The snapshot ids *this* process is copying right now (N46). The fleet-wide
#: lease is what tells *other* replicas a copy is in flight; this is the same
#: fact for the one replica that cannot read it back as a stranger -- itself.
#: Without a store the lease is trivially ours, so a periodic reconcile pass
#: would read this process's own copy as an orphan and re-drive it; and with a
#: store the set still answers the question a round earlier than a round trip
#: does.
_IN_FLIGHT_COPIES: set[str] = set()

#: The cadence of the reconcile pass (N46). The *startup* pass settles what a
#: restart left behind, but once a live copy holds a lease the startup pass is
#: no longer enough on its own: a record whose owner died *after* startup would
#: wait for the next restart to be settled, which is worse than the hole the
#: lease closes (where any ``creating`` record was fair game). One replica per
#: round, like the health sweep and the TTL sweep.
SNAPSHOT_RECONCILE_INTERVAL_S = 10.0


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
    #: The token under which this request holds the id (N46). The claim itself
    #: is the F11 step 3 shape; the token is what lets the *copy* keep holding
    #: it -- an async copy refreshes this very lease for as long as it runs, so
    #: a peer's reconcile pass can tell "somebody is copying" from "orphan".
    claim_token = uuid.uuid4().hex if requested_id is not None else None
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
        if not _snapshots(request).try_acquire_copy(
            requested_id, token=claim_token
        ):
            existing = await _await_snapshot_record(request, requested_id)
            if existing is not None:
                return _already_exists(existing)
            raise OfficialError(
                409,
                f"snapshot {requested_id} is being copied by another "
                "control-plane replica",
            )
    claimed = requested_id is not None
    handed_off = False

    try:
        async with _lock_for(requested_id or sandbox_id):
            # Re-checked inside the lock: the attempt we waited behind may just
            # have written its record.
            if requested_id is not None:
                existing = _existing_snapshot(request, requested_id)
                if existing is not None:
                    return _already_exists(existing)
            if _wants_async(request):
                response = await _create_snapshot_async(
                    request, sandbox_id, name, requested_id, lease_token=claim_token
                )
                # N46: the copy task owns the lease from here, so this request
                # must not release it on the way out (it used to, for the
                # named async shape -- which left the in-flight record with no
                # marker at all). It is released when the record carries the
                # answer, or by the lease's TTL if this replica dies.
                handed_off = True
                return response
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
        if claimed and not handed_off:
            # Released once the *record* exists (async: it is written before
            # the 202) or the copy is done: from then on the record's own
            # ``creating`` status is what any other replica waits on.
            _snapshots(request).release_copy(requested_id, token=claim_token)
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
    *,
    lease_token: str | None = None,
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

    N46: the copy takes the id under a *lease* before the record is written,
    and hands that same lease to the background task. For a named id the token
    is the one the request already claimed under (so this is the same claim,
    now held by the copy); for an unnamed id there is no earlier claim to
    adopt, so this is where it is taken. The order matters: between "record
    written" and "a peer can tell somebody is copying it" there must be no
    window, or a reconcile pass reading the record in that window settles a
    copy that is running.
    """
    registry = _registry(request)
    record = registry.get(sandbox_id)
    _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    if record.state != "running":
        raise OfficialError(409, f"Sandbox {sandbox_id} is not running")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        raise OfficialError(502, f"Node {record.node_id} not found")
    snapshot_id = requested_id or new_sandbox_id().replace("sbx_", "snap_")
    token = lease_token or uuid.uuid4().hex
    if not _snapshots(request).try_acquire_copy(
        snapshot_id, ttl_s=int(COPY_LEASE_TTL_S), token=token
    ):
        # Only reachable for a named id (an unnamed one was minted a line ago,
        # so nothing can be holding it): the caller's claim was taken over.
        raise OfficialError(
            409,
            f"snapshot {snapshot_id} is being copied by another "
            "control-plane replica",
        )
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
        snapshot_id=snapshot_id,
        tenant_id=record.tenant_id,
    )
    # Registered before the task starts: a reconcile round can run between the
    # record being written and the task's first byte, and this is the copy's
    # in-process marker for the whole of that time.
    _IN_FLIGHT_COPIES.add(reserved.snapshot_id)
    task = asyncio.create_task(
        _run_reserved_capture(
            request.app, reserved.snapshot_id, sandbox_id, name, token
        )
    )
    _PENDING_CAPTURES.add(task)
    task.add_done_callback(_PENDING_CAPTURES.discard)
    return JSONResponse(status_code=202, content=reserved.as_snapshot_status())


async def _refresh_copy_lease(
    snapshots,
    snapshot_id: str,
    token: str,
    *,
    ttl_s: float = COPY_LEASE_TTL_S,
    refresh_s: float = COPY_LEASE_REFRESH_S,
    sleep=asyncio.sleep,
) -> None:
    """Keep this replica's copy lease alive for as long as its copy runs (N46).

    The lease's whole point is that it *expires*: an owner that dies mid-copy
    has to stop looking live within seconds, so the TTL is short and the owner
    has to keep saying "still here" (a copy can run for minutes -- the worker
    call times out at 120 s, and N32 measured 76 s for a 2000-file tree).
    Without this refresh the lease would be the *weaker* half of the fix: a
    copy that outlived its TTL is exactly the record a peer's reconcile pass
    settles.

    Losing the lease is not fatal to the copy. The bytes are the user's work,
    so this keeps copying and says so in the log once: what a lost lease
    changes is *who a peer may settle*, not what this replica is doing.

    ``sleep`` is injected so the property is testable on a fake clock
    (``tests/unit/test_snapshot_copy_lease.py``): a real clock would need
    minutes of sleeping to show that ten TTLs of copying do not lapse the
    lease.
    """
    log = logging.getLogger(__name__)
    lost = False
    while True:
        await sleep(refresh_s)
        if snapshots.refresh_copy(snapshot_id, token, ttl_s=int(ttl_s)):
            lost = False
            continue
        if not lost:
            lost = True
            log.warning(
                "snapshot %s: this replica's copy lease is no longer held "
                "(taken over, or the shared store is unreachable); a reconcile "
                "pass may now settle the record while this replica is still "
                "copying it",
                snapshot_id,
            )


async def _run_reserved_capture(
    app,
    snapshot_id: str,
    sandbox_id: str,
    name: str | None,
    lease_token: str,
) -> None:
    """Drive one reserved capture and publish how it ended.

    The copy holds the fleet-wide lease (N46) for as long as it runs, so a
    peer's reconcile pass reads "somebody is copying this" and leaves the
    record alone -- and stops reading it that way on its own within
    ``COPY_LEASE_TTL_S`` if this process dies mid-copy.
    """
    snapshots = app.state.snapshots
    refresher = asyncio.create_task(
        _refresh_copy_lease(snapshots, snapshot_id, lease_token)
    )
    try:
        await asyncio.to_thread(
            _capture_reserved, app.state, snapshot_id, sandbox_id, name
        )
    except Exception as exc:  # noqa: BLE001 - the status *is* the error channel
        logging.getLogger(__name__).warning(
            "async snapshot %s failed: %s", snapshot_id, exc
        )
        try:
            await asyncio.to_thread(
                snapshots.mark_failed, snapshot_id, str(exc)
            )
        except UnknownSnapshotError:
            # A peer deleted the record (its ``rmtree`` is the truth) while
            # this capture was running. There is nothing left to write the
            # failure to, and letting it raise would only kill this task --
            # whose sole callback discards the id -- so the exception would
            # vanish. Name the id and the reason instead.
            logger.warning(
                "snapshot %s: the record was deleted by another replica "
                "before the failure could be recorded",
                snapshot_id,
            )
    finally:
        _IN_FLIGHT_COPIES.discard(snapshot_id)
        refresher.cancel()
        try:
            await refresher
        except asyncio.CancelledError:
            pass
        # The record now carries the answer (completed/failed), which is what
        # a peer reads; the lease has done its job.
        snapshots.release_copy(snapshot_id, token=lease_token)


async def reconcile_pending_snapshots(app) -> int:
    """Resolve every copy whose owner is gone.

    A reserved capture lives in an in-process task, so a restart (or a crash)
    leaves its record at ``creating`` forever and a poller would wait on a copy
    nobody is running. One pass settles each of them: the worker's
    ``POST /agent/snapshots`` is idempotent (a finished payload answers
    ``alreadyExists``), so a copy that *did* finish is simply recorded as
    completed, and anything else is marked failed with the reason.

    Run at startup, and then on ``SNAPSHOT_RECONCILE_INTERVAL_S`` by
    :func:`snapshot_reconcile_loop` -- the pass has to keep running, because
    "the owner is not copying any more" is now decided by the copy's lease, and
    an owner that dies after startup would otherwise wait for the next restart
    to be settled.

    Returns how many records were resolved, for the log line.
    """
    log = logging.getLogger(__name__)
    resolved = 0
    for record in list(app.state.snapshots.in_progress()):
        name = record.names[0] if record.names else None
        if record.snapshot_id in _IN_FLIGHT_COPIES:
            # N46: this very process is copying it. The fleet-wide lease below
            # answers the same question when a store is configured, but without
            # one the lease is trivially ours, and a periodic pass would then
            # read this process's own in-flight copy as an orphan and re-drive
            # it -- the exact harm the lease exists to stop, self-inflicted.
            log.info(
                "snapshot %s: this replica is copying it; leaving it alone",
                record.snapshot_id,
            )
            continue
        # F11 step 3 + N46: not every ``creating`` record is this process's
        # business. The lease is the half that says "somebody is copying this
        # id *now*" -- held and refreshed by whoever is running the copy, named
        # or unnamed -- and taking it is the same test the request path makes.
        # Losing it means leaving the record to the replica that owns it;
        # winning it means nobody owns the record any more, so this pass does.
        claim_token = uuid.uuid4().hex
        if not app.state.snapshots.try_acquire_copy(
            record.snapshot_id,
            ttl_s=int(COPY_LEASE_TTL_S),
            token=claim_token,
        ):
            log.info(
                "snapshot %s: another replica is copying it; leaving the record "
                "to that replica",
                record.snapshot_id,
            )
            continue
        # The re-drive can itself take as long as a copy (the worker call times
        # out at 120 s), so it holds a refreshed lease too: a second replica
        # must not start re-driving the same record while this one is midway.
        refresher = asyncio.create_task(
            _refresh_copy_lease(app.state.snapshots, record.snapshot_id, claim_token)
        )
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
        finally:
            # Same shape as the request path: once the record itself carries the
            # answer (completed/failed), the claim has done its job. Holding it
            # would keep a later pass -- on this replica or the other one -- from
            # settling a record whose owner died mid-copy.
            refresher.cancel()
            try:
                await refresher
            except asyncio.CancelledError:
                pass
            app.state.snapshots.release_copy(
                record.snapshot_id, token=claim_token
            )
        resolved += 1
    if resolved:
        log.info("reconciled %d ownerless snapshot copy(ies)", resolved)
    return resolved


async def snapshot_reconcile_loop(
    app,
    *,
    interval_s: float = SNAPSHOT_RECONCILE_INTERVAL_S,
    clock=time.monotonic,
    sleep=asyncio.sleep,
) -> None:
    """Settle ownerless copies on a cadence, one replica per round (N46).

    **Wiring**: this belongs next to the startup pass in ``create_app``'s
    lifespan --
    ``reconcile_task = asyncio.create_task(snapshot_reconcile_loop(app))`` --
    with the same cancel-on-shutdown discipline as ``node_health_task``.
    (This module owns the loop rather than ``app.py`` so the control plane's
    task wiring stays one line; ``control_plane/app.py`` is not part of this
    change.)

    The round claim is the TTL'd-key single-flight every sweep here uses
    (``NodeRegistry.try_acquire_sweep``, ``try_claim``): two replicas would
    otherwise settle the same records twice.

    A round that runs after the loop itself was stalled is skipped. The reason
    is the one the health sweep documents, one level down: this replica's
    leases are refreshed *by this loop*, so a stall longer than a lease is
    exactly when a record with no lease is not yet evidence that its owner is
    gone -- and settling one is destructive (it re-drives the copy and
    publishes ``failed`` when the worker refuses a half-written payload).
    """
    log = logging.getLogger(__name__)
    previous = clock()
    while True:
        await sleep(interval_s)
        now = clock()
        behind = now - previous
        previous = now
        if behind > COPY_LEASE_TTL_S:
            log.warning(
                "snapshot reconcile: skipping this round -- this loop was "
                "%.1fs behind (>= the %.0fs copy lease), so a record with no "
                "lease is not yet evidence that its owner is gone",
                behind,
                COPY_LEASE_TTL_S,
            )
            continue
        try:
            if app.state.snapshots.try_acquire_reconcile(ttl_s=int(interval_s)):
                resolved = await reconcile_pending_snapshots(app)
                if resolved:
                    log.info("snapshot reconcile: settled %d record(s)", resolved)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("snapshot reconcile pass failed")


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
    # One listing, taken once, off the event loop: the registry walks the
    # shared volume (a stat per record, a ``json.loads`` per uncached one), so
    # asking it twice per request doubled that I/O for a count -- and both
    # calls used to run on the loop, stalling every other request behind
    # them. The page is sliced here instead; ``total`` is the same number the
    # second call used to compute.
    all_records = await asyncio.to_thread(
        _snapshots(request).list,
        sandbox_id_filter=sandbox_id,
        name=name,
        tenant_id=tenant_scope(request),
    )
    total = len(all_records)
    records = all_records[offset : offset + limit]
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
        # The payload lives on the platform namespace root -- `<export>/_snapshots`,
        # the same directory the control plane's own `snapshot.json` sits in
        # (one id, one directory since N58; the two used to be different roots,
        # and the agent wrote the payload under the *tree* root), so this delete
        # is an `rmtree`
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
    volume_mounts = [
        {"name": m["name"], "path": m["path"]} for m in snapshot.volume_mounts
    ]
    try:
        record = registry.create(
            template_id=snapshot.template_id,
            timeout=timeout,
            metadata=dict(snapshot.metadata),
            env_vars=dict(snapshot.env_vars),
            secure=True,
            allow_internet_access=snapshot.allow_internet_access,
            base_image=snapshot.base_image,
            volume_mounts=volume_mounts,
            tenant_id=tenant_id,
            is_admin=is_admin,
        )
    except ResourceUnavailableError as e:
        raise OfficialError(503, str(e))

    workspace_dir = request.app.state.workspace_base / record.sandbox_id
    # The snapshot itself produces **no** pin (N65): its payload is a tar on
    # the shared volume, so the node that happened to capture it buys no
    # locality -- and pinning to it made this path answer 503 while another
    # node sat empty (N60's shape on a second endpoint). A pin comes from one
    # place only: a volume whose bytes are not shared, judged by the same
    # helper the create path uses.
    node = request.app.state.nodes.select_and_reserve(
        base_image=snapshot.base_image,
        volume_node_id=_non_shared_volume_node_id(request, volume_mounts),
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
