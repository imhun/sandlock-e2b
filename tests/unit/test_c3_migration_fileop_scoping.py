"""F1: cross-node migration must survive the C3 file-op scoping.

Migration's whole point is "provision the tree on *another* node", so the
control plane orders the destination worker to run the provision handler, and
that handler hands the tree over through face B --
``apply_sandbox_ownership`` -> ``chown-workspace`` -> the control plane's
``POST /internal/nodes/{node}/file-op``. The file-op scoping requires the
sandbox record to belong to the **calling** node. With the record still on the
source while the destination provisioned, the control plane refused the very
operation it had just ordered (403 "belongs to node <source>, not <target>")
and the whole migration 502'd.

Two properties, pinned here against a control plane whose scoping is live:

* the migration re-points the record at the destination *before* it provisions
  there, so the destination's file op is accepted -- the CP's own deliberate
  step, never anything the request says;
* the ordinary create/delete scoping is *unchanged*: a worker still cannot run
  a file op for a sandbox the record puts on a peer.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import control_plane.api.sandboxes as sandboxes
from control_plane.api.errors import OfficialError
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.volumes import VolumeRegistry
from control_plane.worker_identity_source import StaticWorkerIdentitySource

KEY_A = "key-node-a"
KEY_B = "key-node-b"
IP_A = "10.0.0.1"
IP_B = "10.0.0.2"
NODE_A = "node_a"
NODE_B = "node_b"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", IP_A)
ENDPOINT_B = NodeEndpoint("http://10.0.0.2:49983", IP_B)
UID_X = 10007
WORKER_UID = 65534
WORKER_GID = 65534
SANDBOX = "sbx_migrate"


class _StubAgentClient:
    """The agent hop: records one instruction and answers like the agent."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _record(self, verb: str, kwargs: dict) -> dict:
        self.calls.append({"verb": verb, **kwargs})
        return {"op": verb, "path": kwargs["path"]}

    async def chown(self, **kwargs) -> dict:
        return self._record("chown", kwargs)

    async def rm(self, **kwargs) -> dict:
        return self._record("rm", kwargs)

    async def walk(self, **kwargs) -> dict:
        return self._record("walk", kwargs)


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        internal_api_key="fleet-key",
        internal_api_keys=(),
        internal_node_keys={KEY_A: NODE_A, KEY_B: NODE_B},
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


def _control_app(workspace: Path, *, client) -> tuple:
    workspace_base = workspace / "workspaces"
    state_base = workspace / "state"
    workspace_base.mkdir(parents=True, exist_ok=True)
    state_base.mkdir(parents=True, exist_ok=True)
    settings = _settings(
        workspace_base=workspace_base,
        state_base=state_base,
        # The live k8s lane runs a shared workspace, so migration is the
        # route-switch shape (no archive transfer) -- the shape the defect was
        # observed in.
        shared_workspace_root=str(workspace),
    )
    app = create_control_app(
        settings=settings,
        registry=SandboxRegistry(settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=VolumeRegistry(workspace / "_volumes_base"),
        workspace_base=workspace_base,
        node_address_resolver=StaticAddressResolver(
            {NODE_A: ENDPOINT_A, NODE_B: ENDPOINT_B}
        ),
        c3_agent_client=client,
        worker_identity_source=StaticWorkerIdentitySource(
            {
                NODE_A: (WORKER_UID, WORKER_GID),
                NODE_B: (WORKER_UID, WORKER_GID),
            }
        ),
    )
    return app, workspace_base


def _client(app, *, source_ip: str):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _register(app, *, node_id: str, key: str, source_ip: str) -> None:
    body = {
        "nodeID": node_id,
        "totalMemoryMB": 8192,
        "totalCPUPercent": 800,
        "totalDiskMB": 16384,
        "totalProcesses": 512,
        "workerUID": WORKER_UID,
        "workerGID": WORKER_GID,
    }
    async with _client(app, source_ip=source_ip) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": key},
            json=body,
        )
    assert resp.status_code == 200, resp.text


def _enroll(app, *, node_id: str = NODE_A) -> None:
    """One sandbox record, owned by ``node_id``, with an allocated host uid."""
    registry = app.state.registry
    record = registry.create(
        template_id="base",
        sandbox_id=SANDBOX,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    record.node_id = node_id
    record.host_uid = UID_X
    registry.save(record)


async def _file_op(app, *, node_id: str, key: str, source_ip: str, body: dict):
    async with _client(app, source_ip=source_ip) as client:
        return await client.post(
            f"/internal/nodes/{node_id}/file-op",
            headers={"X-Internal-Key": key},
            json=body,
        )


@pytest.mark.asyncio
async def test_cross_node_migration_is_not_refused_by_the_file_op_scoping(
    workspace,
    monkeypatch,
) -> None:
    agent = _StubAgentClient()
    app, _workspace_base = _control_app(workspace, client=agent)
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app, node_id=NODE_A)

    # The destination's provision handler, reproduced: it runs the sandbox's
    # ownership step through face B, exactly as the worker's create handler
    # does (`agent_fileops.chown_workspace`). The scoping is live here, not
    # stubbed -- that is the point.
    file_op_status: list[int] = []

    async def _provision(request, record, node, settings, snapshot,
                         volume_mounts, snapshot_id=None):
        resp = await _file_op(
            app,
            node_id=NODE_B,
            key=KEY_B,
            source_ip=IP_B,
            body={"op": "chown-workspace", "sandbox_id": SANDBOX, "recursive": True},
        )
        file_op_status.append(resp.status_code)
        if resp.status_code != 200:
            raise OfficialError(
                502, f"Node {node.node_id} failed to provision: {resp.text}"
            )

    async def _stop(request, record, node):
        return True

    async def _export(request, record, node):
        raise AssertionError("shared workspace: no export expected")

    async def _import(request, record, node, tar_path):
        raise AssertionError("shared workspace: no import expected")

    async def _destroy(request, record, node, keep_files=False,
                       keep_volume_slices=False):
        return None

    monkeypatch.setattr(sandboxes, "_stop_source_runtime", _stop)
    monkeypatch.setattr(sandboxes, "_export_sandbox_archive", _export)
    monkeypatch.setattr(sandboxes, "_import_sandbox_archive", _import)
    monkeypatch.setattr(sandboxes, "_provision_remote", _provision)
    monkeypatch.setattr(sandboxes, "_destroy_on_node", _destroy)

    async with _client(app, source_ip=IP_A) as client:
        migrated = await client.post(
            f"/sandboxes/{SANDBOX}/migrate",
            headers={"X-API-Key": "local-key"},
            json={"nodeID": NODE_B},
        )

    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["nodeID"] == NODE_B
    # The destination's hand-over was accepted -- the record names it by then.
    assert file_op_status == [200]
    assert [c["verb"] for c in agent.calls] == ["chown"]
    assert app.state.registry.get(SANDBOX).node_id == NODE_B


@pytest.mark.asyncio
async def test_ordinary_file_op_still_refuses_a_cross_node_request(
    workspace,
) -> None:
    """A worker may not run a file op for a sandbox the record puts on a peer."""
    agent = _StubAgentClient()
    app, _workspace_base = _control_app(workspace, client=agent)
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app, node_id=NODE_A)

    foreign = await _file_op(
        app,
        node_id=NODE_B,
        key=KEY_B,
        source_ip=IP_B,
        body={"op": "chown-workspace", "sandbox_id": SANDBOX, "recursive": True},
    )
    assert foreign.status_code == 403
    assert foreign.json() == {
        "code": 403,
        "message": f"Sandbox {SANDBOX} belongs to node {NODE_A}, not {NODE_B}",
    }
    assert agent.calls == []
