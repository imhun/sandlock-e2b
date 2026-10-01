"""Task 5: the worker's create uses the agent's materialization, and degrades by name.

``_agent_create_sandbox`` used to make the tree itself (``mkdir``, or a
``copytree`` of the snapshot) and then ask the control plane to relay the
ownership hand-over. It now asks the control plane for **one plan** and hands
it to the node's own agent, which does mkdir + copy + chown in a single call
(design §4.3/§4.4).

Two shapes have to keep working, and both are pinned here:

* an agent that takes a plan -- the worker does **no** local tree work at all,
  which is the whole point of the change;
* an agent that cannot (a rolling upgrade, or a deployment shape without one)
  -- the original steps still run, one WARNING names the sandbox and the
  reason, and nothing is skipped in silence.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from envd_service import agent as agent_module
from envd_service import agent_fileops
from envd_service.agent_fileops import AgentMaterializeUnsupported
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry

SANDBOX = "sbx_materialize"
SNAPSHOT = "snap_0123456789abcdef"
UNSUPPORTED_REASON = (
    "the agent at http://10.0.0.1:49986 has no /internal/grants/file-op "
    "(HTTP 404)"
)


class _StubAgentFileOps:
    """Stands in for the worker's agent client; records the one call."""

    def __init__(self, *, unsupported: bool = False) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.relayed: list[tuple[str, str, bool]] = []
        self._unsupported = unsupported

    def materialize(self, sandbox_id: str, snapshot_id: str | None = None) -> dict:
        self.calls.append((sandbox_id, snapshot_id))
        if self._unsupported:
            raise AgentMaterializeUnsupported(UNSUPPORTED_REASON)
        return {"materialized": {"tree": {"path": f"/derived/{sandbox_id}"}}}

    def chown_workspace(self, sandbox_id: str, *, recursive: bool = True) -> None:
        self.relayed.append(("chown-workspace", sandbox_id, recursive))

    def close(self) -> None:  # the lifespan asks for it; nothing to close here
        return None


@pytest.fixture()
def install_client(monkeypatch):
    """Install a stub as the worker's active file-operation client."""

    def _install(stub) -> _StubAgentFileOps:
        monkeypatch.setattr(agent_fileops, "_ACTIVE", [stub])
        return stub

    return _install


def _worker(workspace):
    settings = EnvdSettings(executor="local", workspace_base=workspace)
    app = create_envd_app(
        settings=settings,
        runtime_registry=RuntimeRegistry(workspace),
        workspace_base=workspace,
    )
    return app, settings


async def _create(app, settings, payload: dict):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            "/agent/sandboxes",
            json=payload,
            headers={"X-Internal-Key": settings.internal_api_key},
        )


@pytest.mark.asyncio
async def test_a_supported_agent_materializes_once_and_skips_the_workers_own_copy(
    workspace, monkeypatch, install_client
) -> None:
    """The change's whole point: the worker stops touching the tree."""
    copies: list[tuple] = []
    monkeypatch.setattr(
        agent_module.shutil, "copytree", lambda *args, **kwargs: copies.append(args)
    )
    snapshot_fs = workspace / "_snapshots" / SNAPSHOT / "fs"
    snapshot_fs.mkdir(parents=True)
    (snapshot_fs / "payload.txt").write_text("from the snapshot\n", encoding="utf-8")
    stub = install_client(_StubAgentFileOps())
    app, settings = _worker(workspace)

    resp = await _create(app, settings, {"sandboxID": SANDBOX, "snapshotID": SNAPSHOT})

    assert resp.status_code == 201
    assert stub.calls == [(SANDBOX, SNAPSHOT)]
    assert copies == []
    # The agent's plan already did the hand-over: no relayed ``chown-workspace``
    # follows it (that relay is the 71 ms this change exists to remove).
    assert stub.relayed == []


@pytest.mark.asyncio
async def test_an_unsupported_agent_falls_back_to_the_old_path_and_warns(
    workspace, monkeypatch, install_client, caplog
) -> None:
    """A rolling upgrade must not break creates -- and must not hide the fallback."""
    copies: list[tuple] = []
    monkeypatch.setattr(
        agent_module.shutil,
        "copytree",
        lambda *args, **kwargs: copies.append(args),
    )
    snapshot_fs = workspace / "_snapshots" / SNAPSHOT / "fs"
    snapshot_fs.mkdir(parents=True)
    (snapshot_fs / "payload.txt").write_text("from the snapshot\n", encoding="utf-8")
    stub = install_client(_StubAgentFileOps(unsupported=True))
    app, settings = _worker(workspace)

    with caplog.at_level(logging.WARNING, logger=agent_module.logger.name):
        resp = await _create(
            app, settings, {"sandboxID": SANDBOX, "snapshotID": SNAPSHOT}
        )

    assert resp.status_code == 201
    assert stub.calls == [(SANDBOX, SNAPSHOT)]
    assert copies == [(snapshot_fs, workspace / SANDBOX)]
    # The fallback is complete, not partial: the ownership hand-over still
    # happens, through the control plane's relay.
    assert stub.relayed == [("chown-workspace", SANDBOX, True)]
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == agent_module.logger.name
    ] == [
        f"agent materialization is unavailable for sandbox {SANDBOX} "
        f"({UNSUPPORTED_REASON}): falling back to the control plane's relay "
        "(the create still happens, it is just slower)"
    ]


@pytest.mark.asyncio
async def test_without_an_agent_client_the_worker_still_makes_the_tree(
    workspace, install_client
) -> None:
    """The pre-C3 shape (no agent transport at all) is untouched."""
    install_client(None)
    app, settings = _worker(workspace)

    resp = await _create(app, settings, {"sandboxID": SANDBOX})

    assert resp.status_code == 201
    assert (workspace / SANDBOX / "workspace").is_dir()
