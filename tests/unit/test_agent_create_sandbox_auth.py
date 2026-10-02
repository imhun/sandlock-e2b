"""F6: the create-sandbox agent route must not collapse worker faults into 401.

``agent_create_sandbox`` used to wrap ``_require_internal_key`` and the whole
provisioning call in one ``try``/``except PermissionError``. An EPERM/EACCES
raised *while provisioning* (a root-owned cold shared volume is the observed
case) therefore answered exactly like a bad internal key: 401 with an empty
body, and the control plane's ``Node ... failed to provision: <resp.text>``
showed nothing at all. These tests pin the two apart:

* provisioning ``PermissionError`` -> 500 whose body carries the reason;
* auth failure -> 401 with no reason on the wire.
"""

from __future__ import annotations

import httpx

import envd_service.agent as agent_module
from envd_service.agent_fileops import AgentFileOpsError
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry

# The realistic shape of the observed fault: the worker cannot materialize the
# sandbox directory the shared volume handed it.
PROVISION_REASON = (
    "cold volume /var/lib/e2b-sandboxes/sbx_perm is root-owned, uid 65534 "
    "has no write access (EACCES on mkdir)"
)

# The shape F1 exposed: the destination worker's create handler hands the tree
# over through face B, the control plane's file-op scoping refuses it, and the
# worker must carry *that* refusal back -- not answer a body-less 500 that the
# control plane renders as a bare "failed to provision: ".
FILE_OP_REASON = (
    "the control plane refused chown-workspace for sandbox sbx_fileop "
    "(HTTP 403): Sandbox sbx_fileop belongs to node e2b-worker-0, not "
    "e2b-worker-1"
)


def _make_worker(workspace):
    runtime_registry = RuntimeRegistry(workspace)
    # Pin the settings' workspace to the fixture too: create_app falls back to
    # ``workspace_base or settings.workspace_base``, and the agent routes read
    # settings.workspace_base directly -- leaving them different would write
    # into the repo's tmp/sandboxes instead of the fixture.
    settings = EnvdSettings(executor="local", workspace_base=workspace)
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    return app, settings


async def _post_create(worker, key: str | None, sandbox_id: str):
    app, _settings = worker
    headers = {} if key is None else {"X-Internal-Key": key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            "/agent/sandboxes", json={"sandboxID": sandbox_id}, headers=headers
        )


async def test_create_permission_error_is_500_with_reason(
    workspace, monkeypatch
) -> None:
    worker = _make_worker(workspace)
    _app, settings = worker

    def _boom(**_kwargs):
        raise PermissionError(PROVISION_REASON)

    monkeypatch.setattr(agent_module, "build_volume_mounts", _boom)

    resp = await _post_create(worker, settings.internal_api_key, "sbx_perm")

    assert resp.status_code == 500
    assert resp.status_code != 401
    assert resp.text == PROVISION_REASON


async def test_create_wrong_key_is_401_without_reason(workspace) -> None:
    worker = _make_worker(workspace)
    _app, _settings = worker

    resp = await _post_create(worker, "not-the-internal-key", "sbx_badkey")

    assert resp.status_code == 401
    assert resp.text == ""


async def test_create_missing_key_is_401_without_reason(workspace) -> None:
    worker = _make_worker(workspace)
    _app, _settings = worker

    resp = await _post_create(worker, None, "sbx_nokey")

    assert resp.status_code == 401
    assert resp.text == ""


async def test_create_snapshot_permission_error_is_500_with_reason(
    workspace, monkeypatch
) -> None:
    """Same F6 shape on the snapshot route: a write EACCES is not a 401."""
    worker = _make_worker(workspace)
    app, settings = worker
    sandbox_dir = settings.workspace_base / "sbx_snap_src"
    (sandbox_dir / "workspace").mkdir(parents=True)

    # ``**kwargs``: the writer's signature grew the Task 3 byte cap, and this
    # double stands in for the real call site (the assertion is the 500, not
    # the signature).
    def _boom(*_args, **_kwargs):
        raise PermissionError(PROVISION_REASON)

    monkeypatch.setattr(agent_module, "_write_snapshot_tar", _boom)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post(
            "/agent/snapshots",
            json={"snapshotID": "snap_perm", "sandboxID": "sbx_snap_src"},
            headers={"X-Internal-Key": settings.internal_api_key},
        )

    assert resp.status_code == 500
    assert resp.status_code != 401
    assert resp.text == PROVISION_REASON


async def test_snapshot_copy_runs_off_the_event_loop(workspace, monkeypatch) -> None:
    """Writing a snapshot payload must not stall the worker's loop.

    Measured shape of the bug this pins: on the shared NAS a tree costs about
    16 ms per file to copy, so a few thousand files take longer than the entry
    proxy's timeout. Copying inline stopped every heartbeat and every other
    request on that worker for the duration -- which is what made a snapshot
    look like a worker outage.

    Task 2 changed *what* is written (one ``fs.tar`` instead of an exploded
    ``fs/`` tree); it did not change the rule that reading the sandbox's tree
    and writing the payload happen off the loop.
    """
    import asyncio
    import time

    worker = _make_worker(workspace)
    app, settings = worker
    sandbox_dir = settings.workspace_base / "sbx_snap_loop"
    (sandbox_dir / "workspace").mkdir(parents=True)
    (sandbox_dir / "payload.bin").write_bytes(b"x" * 32)

    # Capture the real one *before* patching: the module attribute is what the
    # handler resolves, so delegating to it after the patch would call the stub
    # again.
    original_write = agent_module._write_snapshot_tar

    def _slow_write(*args, **kwargs):
        time.sleep(0.3)  # stands in for reading a file-heavy tree
        return original_write(*args, **kwargs)

    monkeypatch.setattr(agent_module, "_write_snapshot_tar", _slow_write)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            resp = await client.post(
                "/agent/snapshots",
                json={"snapshotID": "snap_loop", "sandboxID": "sbx_snap_loop"},
                headers={"X-Internal-Key": settings.internal_api_key},
            )
    finally:
        ticker.cancel()

    assert resp.status_code == 201
    # Inline, the loop would have been asleep for the whole 0.3 s copy and this
    # count would be 0 or 1.
    assert ticks > 10, ticks


async def test_a_failed_snapshot_copy_leaves_no_partial_payload(
    workspace, monkeypatch
) -> None:
    """A copy that dies must not leave a payload nothing can reclaim.

    The control plane writes the snapshot's record only on success, so a partial
    ``_snapshots/<id>`` would be an orphan -- and the next attempt for the same
    id would be refused with 409 "already exists".
    """
    import pathlib

    worker = _make_worker(workspace)
    app, settings = worker
    sandbox_dir = settings.workspace_base / "sbx_snap_fail"
    (sandbox_dir / "workspace").mkdir(parents=True)

    def _half_write(_src, dst, **_kwargs):
        # Die after the destination exists: the shape a full disk or a killed
        # worker leaves behind.
        pathlib.Path(dst).write_bytes(b"half")
        raise OSError("no space left on device")

    monkeypatch.setattr(agent_module, "_write_snapshot_tar", _half_write)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post(
            "/agent/snapshots",
            json={"snapshotID": "snap_fail", "sandboxID": "sbx_snap_fail"},
            headers={"X-Internal-Key": settings.internal_api_key},
        )

    assert resp.status_code == 500
    assert not (settings.workspace_base / "_snapshots" / "snap_fail").exists()


async def test_create_file_op_failure_is_500_with_reason(
    workspace, monkeypatch
) -> None:
    """F1: a refused face-B file op names itself instead of an empty 502.

    ``AgentFileOpsError`` is a ``RuntimeError``, so it used to fall through the
    generic arm into ``Response(status_code=500)`` with no body -- which the
    control plane's ``Node ... failed to provision: <resp.text>`` rendered as a
    bare ``"Node ... failed to provision: "``. The same fault now travels named.
    """
    worker = _make_worker(workspace)
    _app, settings = worker

    def _boom(**_kwargs):
        raise AgentFileOpsError(FILE_OP_REASON)

    monkeypatch.setattr(agent_module, "build_volume_mounts", _boom)

    resp = await _post_create(worker, settings.internal_api_key, "sbx_fileop")

    assert resp.status_code == 500
    assert resp.text == FILE_OP_REASON


async def test_create_unexpected_fault_is_500_with_reason(
    workspace, monkeypatch
) -> None:
    """No provisioning fault is silent again -- the generic arm carries it too."""
    worker = _make_worker(workspace)
    _app, settings = worker

    def _boom(**_kwargs):
        raise RuntimeError("a bare provisioning fault")

    monkeypatch.setattr(agent_module, "build_volume_mounts", _boom)

    resp = await _post_create(worker, settings.internal_api_key, "sbx_bare")

    assert resp.status_code == 500
    assert resp.text == "a bare provisioning fault"
