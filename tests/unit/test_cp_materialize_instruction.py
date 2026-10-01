"""Task 3: the control plane sends the create's materialization itself.

The create path used to make the worker build the tree (mkdir / copytree) and
then ask the control plane to *relay* the ownership hand-over -- a
worker→CP→agent→CP→worker circuit per step. Now the control plane, which is
already holding the create and is already about to dial the worker, sends **one
instruction to the node's agent** first and hands the worker a ready tree
(design v2 §4.1).

Two things this file is really about:

* **the address**: the control plane knows the node by the *worker's* identity
  (the record's ``node_id``, e.g. ``e2b-worker-0``) while the agent's own
  identity is the *host* (``k0s-worker-0``). The relayed ops resolve with the
  worker key (``C3AgentClient._target``); the fixture below keeps the two names
  **different** on purpose, because a fixture where they are equal cannot see a
  resolution that goes the wrong way;
* **the content**: every path, uid and gid is the control plane's own
  derivation, so the test asserts the *body* rather than "a request went out".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from control_plane import file_ops
from control_plane.api import sandboxes
from control_plane.c3_agent_client import (
    AgentClientError,
    AgentTarget,
    C3AgentClient,
    StaticAgentAddressResolver,
)
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry, UnknownSandboxError
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.volumes import VolumeRegistry
from control_plane.worker_identity_source import StaticWorkerIdentitySource

KEY_A = "key-node-a"
KEY_B = "key-node-b"
AGENT_TOKEN = "agent-token-0123456789"
#: The worker's identity -- the URL/record key, and the pod name in k8s.
WORKER = "e2b-worker-0"
#: The agent's identity -- the host it runs on (``spec.nodeName`` in k8s).
HOST = "k0s-worker-0"
OTHER_WORKER = "e2b-worker-1"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
ENDPOINT_B = NodeEndpoint("http://10.0.0.2:49983", "10.0.0.2")
AGENT_URL = "http://10.0.0.1:49985"
AGENT_MAINT_URL = "http://10.0.0.1:49986"
UID_X = 10007
WORKER_UID = 65534
WORKER_GID = 65534
SANDBOX = "sbx_cp01"
SNAPSHOT = "snap_0123456789abcdef"
VOLUME_NAME = "data"
VOLUME_QUOTA_MB = 1024


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        internal_api_key="fleet-key",
        internal_api_keys=(),
        internal_node_keys={KEY_A: WORKER, KEY_B: OTHER_WORKER},
        c3_agent_token=AGENT_TOKEN,
        # This lane is about the **remote** create path: with the local node
        # enabled (the shipped default) every create would land on ``local://``
        # and never send an instruction -- which is also why the local case
        # below has to turn it back on to mean anything.
        enable_local_node=False,
        # Pinned so the derived plan's uid is predictable: the create allocates
        # from this pool (``registry.allocate_host_uid``), and a test that
        # asserted "some uid" would not notice the wrong one being handed out.
        uid_pool_start=UID_X,
        uid_pool_size=1000,
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

    def __init__(self, workspace: Path) -> None:
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


class _StubClient:
    """Records one ``materialize`` instruction; refuses on demand."""

    def __init__(self, *, refuse: str | None = None, status_code: int = 502) -> None:
        self.calls: list[dict] = []
        self._refuse = refuse
        self._status_code = status_code

    async def materialize(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._refuse is not None:
            raise AgentClientError(self._refuse, status_code=self._status_code)
        return {"op": "materialize"}


def _app(shape: _Cp, *, client=None, **settings_overrides):
    return create_control_app(
        settings=shape.settings,
        registry=SandboxRegistry(shape.settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=shape.volumes,
        workspace_base=shape.workspace_base,
        node_address_resolver=StaticAddressResolver(
            {WORKER: ENDPOINT_A, OTHER_WORKER: ENDPOINT_B}
        ),
        c3_agent_client=client if client is not None else _StubClient(),
        worker_identity_source=StaticWorkerIdentitySource(
            {WORKER: (WORKER_UID, WORKER_GID)}
        ),
    )


def _client(app, *, source_ip: str = "10.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


@pytest.fixture(autouse=True)
def _no_real_worker_hop(monkeypatch):
    """The create's hop to the *worker* is not what this lane tests.

    ``_provision_remote`` posts to the node's address, which in this fixture is
    a synthetic one -- left alone it would sit in a connect timeout for every
    create. Its own lane (``test_provision_remote_client.py``) covers the wire;
    here it is recorded and answered.
    """
    hops: list[tuple] = []

    async def _fake(request, record, node, settings, snapshot, volume_mounts, **kw):
        hops.append((record.sandbox_id, node.node_id, kw.get("materialized")))

    monkeypatch.setattr(sandboxes, "_provision_remote", _fake)
    return hops


async def _register_node(app, *, node_id: str = WORKER, key: str = KEY_A) -> None:
    """Register the node with its worker identity -- and nothing else.

    No record is planted: the create under test makes its own, which is what
    makes these tests drive the real path (its own id, its own uid).
    """
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": key},
            json={
                "nodeID": node_id,
                "address": (ENDPOINT_A if node_id == WORKER else ENDPOINT_B).address,
                # Generous on purpose: this lane drives real creates, and a
                # node sized to one sandbox turns every create into an
                # admission wait (the shipped queue timeout is tens of
                # seconds) instead of the thing under test.
                "totalMemoryMB": 65536,
                "totalCPUPercent": 6400,
                "totalDiskMB": 1048576,
                "totalProcesses": 4096,
                "pidNamespace": "pid:[4026532458]",
                "containerID": "e4a98a0c5282",
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
            },
        )
    assert resp.status_code == 200


async def _create(app, settings, sandbox_id: str = SANDBOX, **extra):
    """Drive the real ``POST /sandboxes`` -- this is the create path itself."""
    async with _client(app) as client:
        return await client.post(
            "/sandboxes",
            json={"templateID": "base", "sandboxID": sandbox_id, **extra},
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": sandbox_id},
        )


class _Request:
    """The one attribute ``_materialize_remote`` reads off the request."""

    def __init__(self, app) -> None:
        self.app = app


async def _instruct_directly(app, shape, *, snapshot=None, sandbox_id: str = SANDBOX):
    """Call the create path's helper the way ``create_sandbox`` calls it."""
    try:
        app.state.registry.get(sandbox_id)
    except UnknownSandboxError:
        record = app.state.registry.create(
            template_id="base",
            sandbox_id=sandbox_id,
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
        )
        record.node_id = WORKER
        record.host_uid = UID_X
        app.state.registry.save(record)
    return await sandboxes._materialize_remote(
        _Request(app),
        app.state.registry.get(sandbox_id),
        app.state.nodes.get(WORKER),
        shape.settings,
        snapshot,
    )


# --------------------------------------------------------------- the address


@pytest.mark.asyncio
async def test_the_client_resolves_the_worker_key_to_the_hosts_agent() -> None:
    """The instruction is addressed by the agent's own identity (D12).

    The control plane knows the node by the worker's name; ``resolve_agent`` is
    the *host*-keyed lookup (that is what the inventory route uses, where the
    caller is the agent itself). Using it here would ask the pod API for a host
    named after a worker pod and get nothing -- a named 503 on every create.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"op": "materialize"})

    client = C3AgentClient(
        resolver=StaticAgentAddressResolver(
            {
                WORKER: AgentTarget(
                    node_identity=HOST, url=AGENT_URL, maint_url=AGENT_MAINT_URL
                )
            }
        ),
        token=AGENT_TOKEN,
        timeout_s=5.0,
        transport=httpx.MockTransport(handler),
    )

    await client.materialize(
        node_id=WORKER,
        sandbox_id=SANDBOX,
        tree={"path": "/x", "subdir": "workspace", "mode": "0770", "uid": UID_X, "gid": WORKER_GID},
        slices=[],
        worker_uid=WORKER_UID,
        worker_gid=WORKER_GID,
    )

    assert len(seen) == 1
    assert seen[0].url.path == f"/internal/nodes/{HOST}/agent/materialize"
    assert str(seen[0].url.host) == "10.0.0.1"
    assert seen[0].headers["X-Internal-Key"] == AGENT_TOKEN


# ------------------------------------------------------------- the content


@pytest.mark.asyncio
async def test_the_instruction_carries_the_derived_tree(workspace: Path) -> None:
    shape = _Cp(workspace)
    client = _StubClient()
    app = _app(shape, client=client)
    await _register_node(app)

    resp = await _create(app, shape.settings)

    assert resp.status_code == 201
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["node_id"] == WORKER
    assert call["sandbox_id"] == SANDBOX
    assert call["tree"] == {
        "path": str(shape.tree_path()),
        "subdir": "workspace",
        "mode": "0770",
        "uid": UID_X,
        "gid": WORKER_GID,
    }
    assert call["slices"] == []
    assert call["worker_uid"] == WORKER_UID
    assert call["worker_gid"] == WORKER_GID


@pytest.mark.asyncio
async def test_a_snapshot_create_carries_copy_from(workspace: Path) -> None:
    """A snapshot create names the copy source; a plain one does not.

    Called through the helper rather than through ``POST /sandboxes`` because
    the create route resolves ``templateID`` through the snapshot registry,
    which this fixture does not build -- and what is under test here is the
    derivation, which is the same call either way.
    """
    shape = _Cp(workspace)
    client = _StubClient()
    app = _app(shape, client=client)
    await _register_node(app)

    resp = await _create(app, shape.settings)

    assert resp.status_code == 201
    # A plain create's instruction carries no such key: the agent reads
    # "absent" as "nothing to copy", and ``copy_from: null`` would be a second
    # spelling of the same fact.
    assert "copy_from" not in client.calls[0]["tree"]

    # The snapshot shape -- driven through the helper, because the create route
    # resolves ``templateID`` through the snapshot registry this fixture does
    # not build, and the derivation is the same call either way.
    await _instruct_directly(
        app, shape, snapshot=SimpleNamespace(snapshot_id=SNAPSHOT)
    )

    assert client.calls[1]["tree"]["copy_from"] == str(shape.copy_from())


@pytest.mark.asyncio
async def test_a_quota_volume_contributes_a_slice(workspace: Path) -> None:
    shape = _Cp(workspace)
    client = _StubClient()
    app = _app(shape, client=client)
    await _register_node(app)

    resp = await _create(
        app,
        shape.settings,
        volumeMounts=[{"name": shape.volume.volume_id, "path": "/data"}],
    )

    assert resp.status_code == 201
    assert client.calls[0]["slices"] == [
        {
            "volume": shape.volume.volume_id,
            "path": str(shape.slice_path()),
            "uid": UID_X,
            "gid": WORKER_GID,
        }
    ]


# ------------------------------------------------------------- the refusals


@pytest.mark.asyncio
async def test_a_refused_instruction_fails_the_create(workspace: Path) -> None:
    """A materialization the agent refuses is a create that did not happen."""
    shape = _Cp(workspace)
    client = _StubClient(refuse="path-outside-roots: refusing", status_code=400)
    app = _app(shape, client=client)
    await _register_node(app)

    resp = await _create(app, shape.settings)

    assert resp.status_code >= 500
    # The rollback every provisioning failure takes: the record is gone, and
    # with it the host uid the create had allocated.
    with pytest.raises(UnknownSandboxError):
        app.state.registry.get(SANDBOX)


@pytest.mark.asyncio
async def test_a_local_node_needs_no_instruction(workspace: Path, monkeypatch) -> None:
    """``local://`` materializes in this process: there is no agent to dial.

    The branch is what is pinned (the local shape needs a whole in-process
    executor this fixture does not build), so the create's own outcome is not
    asserted -- only that no instruction left the control plane.
    """
    shape = _Cp(workspace)
    client = _StubClient()
    shape.settings.enable_local_node = True
    app = _app(shape, client=client)
    await _register_node(app)
    node = app.state.nodes.get(WORKER)
    node.address = "local://"

    await _create(app, shape.settings)

    assert client.calls == []


@pytest.mark.asyncio
async def test_the_derived_plan_is_the_one_file_ops_derives(workspace: Path) -> None:
    """One derivation, not two: the instruction *is* ``derive_materialize``.

    The op vocabulary, the root checks and the path shapes live in
    ``control_plane/file_ops.py``; a second copy in the create path could only
    drift from the one the grant-era endpoint used.
    """
    shape = _Cp(workspace)
    client = _StubClient()
    app = _app(shape, client=client)
    await _register_node(app)

    resp = await _create(app, shape.settings)

    assert resp.status_code == 201
    # Derive the same plan a second time, from the record the create itself
    # wrote: if the create path had its own copy of the derivation, this is
    # where the two would disagree.
    expected = file_ops.derive_materialize(
        app.state.registry.get(SANDBOX),
        paths=file_ops.control_paths(app.state, shape.settings),
        node_id=WORKER,
        worker_gid=WORKER_GID,
        snapshot_id=None,
    )
    assert client.calls[0]["tree"] == expected["tree"]
    assert client.calls[0]["slices"] == expected["slices"]
