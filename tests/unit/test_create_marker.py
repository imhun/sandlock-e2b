"""Task 7: the create-in-flight marker, and the delete that waits for it.

Today's create and teardown already race: a ``DELETE`` that lands mid-create
removes the tree, and the create then keeps going and writes its record and
disk accounting -- residue (the N53 class). This change widens that window
(the record write moves off the response path in the next task), so the race is
closed first, with two readable invariants:

* **the marker is there ⇒ a create is in flight** (a teardown must wait);
* **the record is there ⇒ that create finished** (which is why the marker and
  the record, not two writes in one record, are what say so).

A crashed create leaves the marker and no record: that is the *existing*
orphan path's input, and a stale marker is reclaimed instead of hanging a
delete forever.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from pathlib import Path

import httpx
import pytest

from envd_service import agent as agent_module
from envd_service import agent_fileops
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common import paths

SANDBOX = "sbx_marker"
SNAPSHOT = "snap_0123456789abcdef"
KEY = "internal-key"


class _MarkerClient:
    """The worker's file-operation client, for a create that is held open.

    The hold itself lives on the worker's own tree step (a slow
    ``shutil.copytree``, installed by the test) -- the marker is written before
    that step, so blocking it is exactly "a create is in flight". The removals
    mirror the real client's shape (one op each) and are tolerant of an absent
    path, because "the tree is already gone" is a normal state for the
    stale-marker case rather than the fault ``_AgentStub`` in the file-op lane
    models.
    """

    def __init__(self, *, workspace_base: Path, state_base: Path) -> None:
        self.calls: list[tuple[str, str]] = []
        self._workspace_base = workspace_base
        self._state_base = state_base

    def chown_workspace(self, sandbox_id: str, *, recursive: bool = True) -> None:
        self.calls.append(("chown-workspace", sandbox_id))

    def remove_workspace(self, sandbox_id: str) -> None:
        self.calls.append(("remove-workspace", sandbox_id))
        shutil.rmtree(self._workspace_base / sandbox_id, ignore_errors=True)

    def remove_runtime(self, sandbox_id: str) -> None:
        self.calls.append(("remove-runtime", sandbox_id))
        shutil.rmtree(self._state_base / "_runtime" / sandbox_id, ignore_errors=True)

    def close(self) -> None:
        return None


class _Hold:
    """A create that can be held open, on the worker's own copy step."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        # Bound *before* the patch goes in: ``agent_module.shutil`` is the
        # module itself, so calling ``shutil.copytree`` from here after the
        # patch is a call to this method (a recursion, learned the hard way).
        self._real = shutil.copytree
        self._gated = False

    def copytree(self, *args, **kwargs):
        """The real copy, with a gate in front of the outermost call.

        ``*args`` because ``shutil.copytree`` re-enters itself per directory --
        and it re-enters *this* function, since the patch is on the module.
        Only the first (outermost) call waits.
        """
        if not self._gated:
            self._gated = True
            self.entered.set()
            self.release.wait(timeout=10)
        return self._real(*args, **kwargs)


def _worker(
    workspace: Path,
    monkeypatch,
    *,
    hold: _Hold | None = None,
    wait_s: int = 60,
):
    # ``create_app`` resolves the shape and installs the singleton when the
    # transport is the agent one -- on the *module's* list object unless it is
    # isolated first. A leaked one makes later tests dial a control plane that
    # does not exist (the lesson ``test_c3_fileops_worker.py``'s own autouse
    # fixture records).
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [None])
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "agent")
    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", "http://control-plane:3000")
    monkeypatch.setenv("E2B_NODE_ID", "e2b-worker-0")
    monkeypatch.delenv("E2B_PER_SANDBOX_UID", raising=False)
    workspace_base = workspace / "workspaces"
    state_base = workspace / "state"
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace_base,
        state_base=state_base,
        shared_volume_root=None,
        internal_api_key=KEY,
        create_wait_s=wait_s,
    )
    app = create_envd_app(
        settings=settings,
        runtime_registry=RuntimeRegistry(workspace_base, state_base=state_base),
    )
    client = _MarkerClient(workspace_base=workspace_base, state_base=state_base)
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [client])
    if hold is not None:
        monkeypatch.setattr(agent_module.shutil, "copytree", hold.copytree)
        snapshot = workspace_base / "_snapshots" / SNAPSHOT / "fs"
        (snapshot / "workspace").mkdir(parents=True, exist_ok=True)
    return app, settings, client


def _marker(settings, sandbox_id: str = SANDBOX) -> Path:
    return paths.sandbox_creating_marker(
        settings.workspace_base,
        sandbox_id,
        state_base=settings.state_base,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    )


async def _create(app):
    return await _create_with(app)


async def _create_with(app, **extra):
    async with _client(app) as client:
        return await client.post(
            "/agent/sandboxes",
            json={"sandboxID": SANDBOX, **extra},
            headers={"X-Internal-Key": KEY},
        )


async def _delete(app):
    async with _client(app) as client:
        return await client.delete(
            f"/agent/sandboxes/{SANDBOX}", headers={"X-Internal-Key": KEY}
        )


@pytest.mark.asyncio
async def test_the_marker_exists_while_a_create_is_in_flight(
    workspace, monkeypatch
) -> None:
    hold = _Hold()
    app, settings, _client_ = _worker(workspace, monkeypatch, hold=hold)

    create = asyncio.create_task(
        _create_with(app, snapshotID=SNAPSHOT)
    )
    await asyncio.to_thread(hold.entered.wait, 5)

    assert _marker(settings).is_file() is True
    hold.release.set()
    resp = await create
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_the_marker_is_gone_once_the_create_succeeded(
    workspace, monkeypatch
) -> None:
    app, settings, _client_ = _worker(workspace, monkeypatch)

    resp = await _create(app)

    assert resp.status_code == 201
    # The record write is off the create's response path (the next task), so
    # the marker comes off when the record is *durable*, not when the response
    # is written: the invariant is "record on disk ⇒ marker gone", and this
    # waits for that rather than for the HTTP response.
    deadline = time.monotonic() + 5
    while _marker(settings).exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert _marker(settings).exists() is False
    assert app.state.runtime_registry.get(SANDBOX) is not None


@pytest.mark.asyncio
async def test_a_delete_waits_for_the_in_flight_create_and_removes_nothing_twice(
    workspace, monkeypatch
) -> None:
    hold = _Hold()
    app, settings, client = _worker(workspace, monkeypatch, hold=hold)
    runtime_dir = Path(settings.state_base) / "_runtime" / SANDBOX

    create = asyncio.create_task(_create_with(app, snapshotID=SNAPSHOT))
    await asyncio.to_thread(hold.entered.wait, 5)
    delete = asyncio.create_task(_delete(app))
    # Let the delete reach its wait before the create is allowed to finish: if
    # it did not wait, it would tear the tree down *while* the create is still
    # writing its record -- the residue this marker exists to prevent.
    await asyncio.sleep(0.2)
    assert delete.done() is False

    hold.release.set()
    assert (await create).status_code == 201
    assert (await delete).status_code == 204

    assert [call for call in client.calls if call[0] == "remove-workspace"] == [
        ("remove-workspace", SANDBOX)
    ]
    assert [call for call in client.calls if call[0] == "remove-runtime"] == [
        ("remove-runtime", SANDBOX)
    ]
    assert runtime_dir.exists() is False
    assert (Path(settings.workspace_base) / SANDBOX).exists() is False


@pytest.mark.asyncio
async def test_a_stale_marker_is_reclaimed_instead_of_hanging(
    workspace, monkeypatch
) -> None:
    """A create that died leaves the marker behind: reclaim, do not wait."""
    app, settings, client = _worker(workspace, monkeypatch, wait_s=60)
    marker = _marker(settings)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("?", encoding="utf-8")
    stale = os.stat(marker).st_mtime - 3600
    os.utime(marker, (stale, stale))
    tree = Path(settings.workspace_base) / SANDBOX
    (tree / "workspace").mkdir(parents=True)

    resp = await _delete(app)

    assert resp.status_code == 204
    assert marker.exists() is False
    assert tree.exists() is False
    assert (Path(settings.state_base) / "_runtime" / SANDBOX).exists() is False


@pytest.mark.asyncio
async def test_the_marker_alone_does_not_make_a_record_look_live(
    workspace, monkeypatch
) -> None:
    """Only ``sandbox.json`` says a create succeeded -- never the marker."""
    app, settings, _client_ = _worker(workspace, monkeypatch)
    marker = _marker(settings)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("?", encoding="utf-8")

    assert app.state.runtime_registry.get(SANDBOX) is None
    assert marker.is_file() is True
