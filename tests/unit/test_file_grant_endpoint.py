"""Task 2: the control plane mints a per-create ``materialize-tree`` grant.

The create path asks for one short-lived, signed plan
(``POST /internal/nodes/{node}/file-grant``) and then carries it straight to
its own node's agent, so the materialization stops paying a
worker→CP→agent→CP→worker round trip per step (design §4.1). The whole point is
that the control plane keeps *deciding* and stops *relaying*.

So these tests are mostly refusals, one per way the endpoint could be made to
hand out an authority it did not derive:

* the plan is addressed to the agent's **own** identity, not the worker's claim
  (D12) -- verified here by re-verifying the token with ``host=<node>``;
* the path and the uid come from the control plane's record, never from the
  body (§14.4 hard rule 2: a ``path``/``uid`` in the request is refused by
  name, not ignored);
* another node's sandbox, and a sandbox nobody records, are 403/404;
* the op vocabulary of this surface is exactly ``materialize-tree`` -- a
  worker asking for ``chown-workspace`` here is refused, even though that op
  exists on the *other* surface;
* the lifetime is the configured TTL and is capped by the protocol's own
  maximum, so a mis-set ``E2B_CREATE_GRANT_TTL_S`` cannot mint a long-lived
  credential (§5).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane import file_ops
from control_plane.c3_agent_client import (
    AgentTarget,
    C3AgentClient,
    StaticAgentAddressResolver,
)
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.volumes import VolumeRegistry
from control_plane.worker_identity_source import StaticWorkerIdentitySource
from gateway_common.create_grant import MAX_TTL_S, OP, verify

KEY_A = "key-node-a"
KEY_B = "key-node-b"
AGENT_TOKEN = "agent-token-0123456789"
NODE_A = "node_a"
NODE_B = "node_b"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
ENDPOINT_B = NodeEndpoint("http://10.0.0.2:49983", "10.0.0.2")
AGENT_A_URL = "http://10.0.0.1:39983"
AGENT_A_MAINT_URL = "http://10.0.0.1:49983"
UID_X = 10007
WORKER_UID = 65534
WORKER_GID = 65534
SANDBOX = "sbx_grant01"
SNAPSHOT = "snap_0123456789abcdef"
VOLUME_NAME = "data"
VOLUME_QUOTA_MB = 1024


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        internal_api_key="fleet-key",
        internal_api_keys=(),
        internal_node_keys={KEY_A: NODE_A, KEY_B: NODE_B},
        c3_agent_token=AGENT_TOKEN,
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


class _Cp:
    """One test's control plane, with the four roots inside its workspace."""

    def __init__(self, workspace: Path, **settings_overrides) -> None:
        self.workspace = workspace
        self.workspace_base = workspace / "workspaces"
        self.state_base = workspace / "state"
        self.image_cache = workspace / "_images"
        self.shared_root = workspace
        for path in (self.workspace_base, self.state_base, self.image_cache):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = _settings(
            workspace_base=self.workspace_base,
            state_base=self.state_base,
            image_cache_dir=self.image_cache,
            shared_volume_root=str(self.shared_root),
            **settings_overrides,
        )
        self.volumes = VolumeRegistry(workspace / "_volumes_base")
        self.volume = self.volumes.create(
            name=VOLUME_NAME, per_sandbox_quota_mb=VOLUME_QUOTA_MB
        )

    def tree_path(self) -> Path:
        return self.workspace_base / SANDBOX

    def copy_from(self, snapshot_id: str = SNAPSHOT) -> Path:
        return self.workspace_base / "_snapshots" / snapshot_id / "fs"

    def slice_path(self) -> Path:
        return Path(self.volume.path) / SANDBOX


def _agent_client() -> C3AgentClient:
    """The real client, wired to a fixed address table (the D4 seam)."""
    return C3AgentClient(
        resolver=StaticAgentAddressResolver(
            {
                NODE_A: AgentTarget(
                    node_identity=NODE_A, url=AGENT_A_URL, maint_url=AGENT_A_MAINT_URL
                )
            }
        ),
        token=AGENT_TOKEN,
        timeout_s=5.0,
    )


def _app(shape: _Cp, *, agent_client=None):
    return create_control_app(
        settings=shape.settings,
        registry=SandboxRegistry(shape.settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=shape.volumes,
        workspace_base=shape.workspace_base,
        node_address_resolver=StaticAddressResolver(
            {NODE_A: ENDPOINT_A, NODE_B: ENDPOINT_B}
        ),
        c3_agent_client=_agent_client() if agent_client is None else agent_client,
        worker_identity_source=StaticWorkerIdentitySource(
            {NODE_A: (WORKER_UID, WORKER_GID)}
        ),
    )


def _client(app, *, source_ip: str = "10.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _enroll(
    app,
    *,
    node_id: str = NODE_A,
    key: str = KEY_A,
    sandbox_id: str = SANDBOX,
    host_uid: int | None = UID_X,
    volume_mounts: list[dict[str, str]] | None = None,
) -> None:
    """Register the node (with its worker identity) and put one sandbox on it."""
    registry = app.state.registry
    record = registry.create(
        template_id="base",
        sandbox_id=sandbox_id,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    record.node_id = node_id
    record.host_uid = host_uid
    record.volume_mounts = list(volume_mounts or [])
    registry.save(record)
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": key},
            json={
                "nodeID": node_id,
                "address": (ENDPOINT_A if node_id == NODE_A else ENDPOINT_B).address,
                "totalMemoryMB": 1024,
                "totalCPUPercent": 100,
                "totalDiskMB": 1024,
                "totalProcesses": 64,
                "pidNamespace": "pid:[4026532458]",
                "containerID": "e4a98a0c5282",
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
            },
        )
    assert resp.status_code == 200


async def _grant(
    app,
    body: dict,
    *,
    node_id: str = NODE_A,
    key: str = KEY_A,
    source_ip: str = "10.0.0.1",
):
    # ``source_ip`` is part of the identity layer, not a detail of the client:
    # node B is only reachable from *its* address, so a test about ownership
    # has to arrive from the right one or it would be refused for the wrong
    # reason.
    async with _client(app, source_ip=source_ip) as client:
        return await client.post(
            f"/internal/nodes/{node_id}/file-grant",
            headers={"X-Internal-Key": key},
            json=body,
        )


# ------------------------------------------------------------ the happy path


def test_the_grant_op_is_the_one_the_vocabulary_names() -> None:
    """One spelling, in two modules: pinned so a rename cannot drift them apart."""
    assert file_ops.FILE_OPS[OP].op == OP


@pytest.mark.asyncio
async def test_the_grant_names_the_agent_host_and_verifies(workspace) -> None:
    """The token is addressed to the agent's identity and carries derived paths."""
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app)

    resp = await _grant(app, {"op": OP, "sandbox_id": SANDBOX})

    assert resp.status_code == 200
    body = resp.json()
    assert body["agentURL"] == AGENT_A_MAINT_URL
    payload = verify(body["grant"], secret=AGENT_TOKEN, host=NODE_A)
    assert payload["op"] == OP
    assert payload["sandbox_id"] == SANDBOX
    assert payload["tree"]["path"] == str(shape.tree_path())
    assert payload["tree"]["subdir"] == "workspace"
    assert payload["tree"]["mode"] == "0770"
    assert payload["tree"]["uid"] == UID_X
    assert payload["tree"]["gid"] == WORKER_GID
    assert "copy_from" not in payload["tree"]
    assert body["expiresAt"] == payload["exp"]
    assert isinstance(payload["jti"], str) and len(payload["jti"]) == 16


@pytest.mark.asyncio
async def test_the_ttl_defaults_to_ten_seconds(workspace) -> None:
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app)

    resp = await _grant(app, {"op": OP, "sandbox_id": SANDBOX})

    assert resp.status_code == 200
    payload = verify(
        resp.json()["grant"], secret=AGENT_TOKEN, host=NODE_A
    )
    assert payload["exp"] - payload["iat"] == 10


@pytest.mark.asyncio
async def test_the_ttl_comes_from_settings_and_is_capped(workspace) -> None:
    """A mis-set ``E2B_CREATE_GRANT_TTL_S`` cannot outlive the protocol's max."""
    shape = _Cp(workspace, create_grant_ttl_s=600)
    app = _app(shape)
    await _enroll(app)

    resp = await _grant(app, {"op": OP, "sandbox_id": SANDBOX})

    assert resp.status_code == 200
    payload = verify(resp.json()["grant"], secret=AGENT_TOKEN, host=NODE_A)
    assert payload["exp"] - payload["iat"] == MAX_TTL_S


@pytest.mark.asyncio
async def test_a_snapshot_create_carries_copy_from(workspace) -> None:
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app)

    resp = await _grant(
        app, {"op": OP, "sandbox_id": SANDBOX, "snapshot_id": SNAPSHOT}
    )

    assert resp.status_code == 200
    payload = verify(resp.json()["grant"], secret=AGENT_TOKEN, host=NODE_A)
    assert payload["tree"]["copy_from"] == str(shape.copy_from())


# --------------------------------------------------- the plan carries slices


@pytest.mark.asyncio
async def test_a_quota_volume_gets_one_slice_in_the_plan(workspace) -> None:
    """One slice per mounted volume, at the volume's own root (E2.5 shape)."""
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(
        app,
        volume_mounts=[{"name": shape.volume.volume_id, "path": "/data"}],
    )

    resp = await _grant(app, {"op": OP, "sandbox_id": SANDBOX})

    assert resp.status_code == 200
    payload = verify(resp.json()["grant"], secret=AGENT_TOKEN, host=NODE_A)
    assert payload["slices"] == [
        {
            "volume": shape.volume.volume_id,
            "path": str(shape.slice_path()),
            "uid": UID_X,
            "gid": WORKER_GID,
        }
    ]


@pytest.mark.asyncio
async def test_a_volume_without_a_quota_contributes_no_slice(workspace) -> None:
    """``per_sandbox_quota_mb <= 0`` mounts the root: there is nothing to make."""
    shape = _Cp(workspace)
    app = _app(shape)
    plain = shape.volumes.create(name="plain", per_sandbox_quota_mb=0)
    await _enroll(
        app, volume_mounts=[{"name": plain.volume_id, "path": "/plain"}]
    )

    resp = await _grant(app, {"op": OP, "sandbox_id": SANDBOX})

    assert resp.status_code == 200
    payload = verify(resp.json()["grant"], secret=AGENT_TOKEN, host=NODE_A)
    assert payload["slices"] == []


# ------------------------------------------------------------ the refusals


@pytest.mark.asyncio
async def test_another_nodes_sandbox_is_refused(workspace) -> None:
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app, node_id=NODE_A, key=KEY_A)

    resp = await _grant(
        app,
        {"op": OP, "sandbox_id": SANDBOX},
        node_id=NODE_B,
        key=KEY_B,
        source_ip="10.0.0.2",
    )

    assert resp.status_code == 403
    assert f"belongs to node {NODE_A}" in resp.json()["message"]


@pytest.mark.asyncio
async def test_an_unknown_sandbox_is_refused(workspace) -> None:
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app)

    resp = await _grant(app, {"op": OP, "sandbox_id": "sbx_nobody01"})

    assert resp.status_code == 404
    assert resp.json()["code"] == 404


@pytest.mark.asyncio
async def test_an_op_outside_the_vocabulary_is_refused(workspace) -> None:
    """``chown-workspace`` is a real op -- on the *other* surface, not this one."""
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app)

    resp = await _grant(app, {"op": "chown-workspace", "sandbox_id": SANDBOX})

    assert resp.status_code == 400
    assert OP in resp.json()["message"]


@pytest.mark.asyncio
async def test_a_body_that_names_a_path_is_refused_by_name(workspace) -> None:
    """The control plane derives the target; the request may not name one."""
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app)

    resp = await _grant(
        app,
        {"op": OP, "sandbox_id": SANDBOX, "path": "/etc"},
    )

    assert resp.status_code == 400
    assert "path" in resp.json()["message"]


@pytest.mark.asyncio
async def test_a_node_without_a_worker_identity_is_refused(workspace) -> None:
    """The gid a tree is handed to is the worker's own; without it, no grant."""
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app)
    app.state.nodes.get(NODE_A).worker_gid = None

    resp = await _grant(app, {"op": OP, "sandbox_id": SANDBOX})

    assert resp.status_code == 503
    assert resp.json()["code"] == 503


@pytest.mark.asyncio
async def test_a_sandbox_without_a_host_uid_is_refused(workspace) -> None:
    """No allocated uid means no ownership to hand over: fail, do not guess."""
    shape = _Cp(workspace)
    app = _app(shape)
    await _enroll(app, host_uid=None)

    resp = await _grant(app, {"op": OP, "sandbox_id": SANDBOX})

    assert resp.status_code == 503
    assert resp.json()["code"] == 503
