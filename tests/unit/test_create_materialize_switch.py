"""Task 4: the worker is handed a ready tree, or builds one itself.

The control plane now materializes the tree on the node's agent **before** it
dials the worker (design v2 §4.1), so the create payload says so: one boolean,
``materialized``. ``True`` means "the tree exists and is already handed over --
skip both the ``mkdir``/``copytree`` and the ownership step"; anything else
means today's behaviour, byte for byte.

That "anything else" is the whole rolling-upgrade story, and it is why there is
no exception type, no retry and no warning here (v1 had all three):

* a new control plane + an old worker: the old worker does not know the key and
  builds the tree itself;
* an old control plane + a new worker: no key, so the worker builds it itself.

Both directions are the old path, and the old path is the one that already
works. Nothing here can be "unsupported".
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from envd_service import agent as agent_module
from envd_service import agent_fileops
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry

SANDBOX = "sbx_materialize"
SNAPSHOT = "snap_0123456789abcdef"
KEY = "internal-key"


class _RelayStub:
    """The worker's file-operation client: records the relayed steps."""

    def __init__(self) -> None:
        self.relayed: list[tuple[str, str, bool]] = []

    def chown_workspace(self, sandbox_id: str, *, recursive: bool = True) -> None:
        self.relayed.append(("chown-workspace", sandbox_id, recursive))

    def close(self) -> None:  # the lifespan asks for it; nothing to close here
        return None


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    """``create_app`` installs the agent-shaped client when the transport says so.

    Isolated first, or that client lands on the module's own list object and
    every later test in the suite dials a control plane that does not exist
    (the lesson this file's sibling lane records).
    """
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [None])


def _worker(workspace: Path, monkeypatch, *, relay=None):
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
    )
    app = create_envd_app(
        settings=settings,
        runtime_registry=RuntimeRegistry(workspace_base, state_base=state_base),
        workspace_base=workspace_base,
    )
    stub = relay if relay is not None else _RelayStub()
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [stub])
    return app, settings, stub


async def _create(app, settings, **extra):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            "/agent/sandboxes",
            json={"sandboxID": SANDBOX, **extra},
            headers={"X-Internal-Key": KEY},
        )


def _snapshot(workspace: Path) -> Path:
    fs = workspace / "workspaces" / "_snapshots" / SNAPSHOT / "fs"
    (fs / "workspace").mkdir(parents=True, exist_ok=True)
    (fs / "workspace" / "kept.txt").write_text("kept\n", encoding="utf-8")
    return fs


def _spy_volume_pass(monkeypatch) -> list[bool]:
    """Record what the volume pass is told about the slices."""
    seen: list[bool] = []

    def _spy(**kwargs):
        seen.append(kwargs["slices_materialized"])
        return [], []

    monkeypatch.setattr(agent_module, "build_volume_mounts", _spy)
    return seen


@pytest.mark.asyncio
async def test_a_materialized_create_skips_the_tree_and_the_handover(
    workspace: Path, monkeypatch
) -> None:
    """The change's whole point: the worker stops touching the tree."""
    relay = _RelayStub()
    copies: list[tuple] = []
    monkeypatch.setattr(
        agent_module.shutil, "copytree", lambda *args, **kwargs: copies.append(args)
    )
    slices = _spy_volume_pass(monkeypatch)
    _snapshot(workspace)
    app, settings, _ = _worker(workspace, monkeypatch, relay=relay)

    resp = await _create(
        app, settings, snapshotID=SNAPSHOT, materialized=True
    )

    assert resp.status_code == 201
    assert copies == []
    # The agent's chown is the hand-over: a relayed one on top of it would be
    # the 71 ms round trip this change exists to remove.
    assert relay.relayed == []
    # ...and the volume pass is told the slices are already made.
    assert slices == [True]


@pytest.mark.asyncio
async def test_a_plain_create_still_builds_the_tree_and_hands_it_over(
    workspace: Path, monkeypatch
) -> None:
    """No flag (an older control plane, or a shape that materializes itself)."""
    relay = _RelayStub()
    copies: list[tuple] = []
    monkeypatch.setattr(
        agent_module.shutil, "copytree", lambda *args, **kwargs: copies.append(args)
    )
    slices = _spy_volume_pass(monkeypatch)
    fs = _snapshot(workspace)
    app, settings, _ = _worker(workspace, monkeypatch, relay=relay)

    resp = await _create(app, settings, snapshotID=SNAPSHOT)

    assert resp.status_code == 201
    # Into the tree **root**: ``fs/`` carries the root's contents (the shape
    # the agent's own materialization reproduces).
    assert copies == [(fs, settings.workspace_base / SANDBOX)]
    assert relay.relayed == [("chown-workspace", SANDBOX, True)]
    assert slices == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, False, "yes", 1])
async def test_an_unknown_flag_value_is_treated_as_absent(
    workspace: Path, monkeypatch, flag
) -> None:
    """Only a literal ``True`` means "already materialized"."""
    relay = _RelayStub()
    copies: list[tuple] = []
    monkeypatch.setattr(
        agent_module.shutil, "copytree", lambda *args, **kwargs: copies.append(args)
    )
    slices = _spy_volume_pass(monkeypatch)
    _snapshot(workspace)
    app, settings, _ = _worker(workspace, monkeypatch, relay=relay)

    resp = await _create(app, settings, snapshotID=SNAPSHOT, materialized=flag)

    assert resp.status_code == 201
    assert len(copies) == 1
    assert relay.relayed == [("chown-workspace", SANDBOX, True)]
    assert slices == [False]
