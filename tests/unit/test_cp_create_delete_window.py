"""Task 5: a teardown must not race a create of the same sandbox.

The control plane now materializes a create's tree on the node's agent
**before** it hands the worker anything (design v2 §4.1), which opens a window
the old shape did not have: the tree exists, the worker has not been dialled
yet. A ``DELETE`` that lands in that window -- possible whenever the client
chose the sandbox id -- must not produce either of the two residues the
platform has cleaned up before (open-issues N53):

* the record kept while the tree is gone (the delete tore the tree down and the
  in-flight create then re-saved its record), or
* the tree kept while the record is gone (the delete dropped the record and the
  create finished onto a tree nobody owns).

The mechanism is the same discipline the worker's create marker uses, one level
up: the create registers a claim, a teardown of that id waits for it -- bounded
-- and a claim that outlives the bound is **abandoned**, which the create
itself honours before it would keep a record.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from control_plane.api import sandboxes
from control_plane.app import create_app as create_control_app
from control_plane.c3_agent_client import AgentTarget, C3AgentClient, StaticAgentAddressResolver
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry, UnknownSandboxError
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.volumes import VolumeRegistry
from control_plane.worker_identity_source import StaticWorkerIdentitySource

KEY = "key-node-a"
API_KEY = "local-key"
AGENT_TOKEN = "agent-token-0123456789"
WORKER = "e2b-worker-0"
HOST = "k0s-worker-0"
ENDPOINT = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
AGENT_URL = "http://10.0.0.1:49985"
AGENT_MAINT_URL = "http://10.0.0.1:49986"
UID_X = 10007
WORKER_UID = 65534
WORKER_GID = 65534
SANDBOX = "sbx_window"


class _HeldMaterialize:
    """The agent client, with a materialization a test can hold open."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def materialize(self, **kwargs) -> dict:
        self.calls.append(kwargs["sandbox_id"])
        self.entered.set()
        await self.release.wait()
        return {"op": "materialize"}


class _SharedRecordStore:
    """The Redis shape in a dict: two replicas share it, and reads go through it.

    ``SandboxRegistry``'s injection seam (``record_store=``) exists so a lane
    with no Redis can still be the *shared-store* shape: ``get`` reads the store
    rather than the process cache (so a delete by "another replica" is visible
    immediately) and ``save`` writes through to it (so a create that went on
    would visibly resurrect the record -- which is what makes this the faithful
    reproduction of review C2, not an in-process approximation).
    """

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def get(self, sandbox_id: str):
        return self.records.get(sandbox_id)

    def put(self, sandbox_id: str, payload: dict, ttl=None) -> None:
        self.records[sandbox_id] = dict(payload)

    def delete(self, sandbox_id: str) -> None:
        self.records.pop(sandbox_id, None)

    def record_key(self, sandbox_id: str) -> str:
        return f"e2b:sandbox:{sandbox_id}"

    def keys(self) -> list[str]:
        """The ids in the store (``SandboxRegistry.list`` walks these)."""
        return list(self.records)


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=(API_KEY,),
        internal_api_key="fleet-key",
        internal_api_keys=(),
        internal_node_keys={KEY: WORKER},
        c3_agent_token=AGENT_TOKEN,
        enable_local_node=False,
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
    def __init__(self, workspace: Path, **overrides) -> None:
        self.workspace = workspace
        self.workspace_base = workspace / "workspaces"
        self.state_base = workspace / "state"
        for path in (self.workspace_base, self.state_base):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = _settings(
            workspace_base=self.workspace_base,
            state_base=self.state_base,
            shared_volume_root=str(workspace),
            **overrides,
        )
        self.volumes = VolumeRegistry(workspace / "_volumes_base")


def _app(shape: _Cp, *, agent, registry=None) -> object:
    return create_control_app(
        settings=shape.settings,
        registry=registry if registry is not None else SandboxRegistry(shape.settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=shape.volumes,
        workspace_base=shape.workspace_base,
        node_address_resolver=StaticAddressResolver({WORKER: ENDPOINT}),
        c3_agent_client=agent,
        worker_identity_source=StaticWorkerIdentitySource(
            {WORKER: (WORKER_UID, WORKER_GID)}
        ),
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.1", 44444)),
        base_url="http://control",
    )


async def _register(app) -> None:
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": KEY},
            json={
                "nodeID": WORKER,
                "address": ENDPOINT.address,
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


async def _create(app):
    async with _client(app) as client:
        return await client.post(
            "/sandboxes",
            json={"templateID": "base", "sandboxID": SANDBOX},
            headers={"X-API-Key": API_KEY, "X-Sandbox-Id": SANDBOX},
        )


async def _delete(app):
    async with _client(app) as client:
        return await client.delete(
            f"/sandboxes/{SANDBOX}", headers={"X-API-Key": API_KEY}
        )


def _register_record(app, sandbox_id: str = SANDBOX):
    """Is there a record? ``registry.get`` raises when there is not."""
    try:
        return app.state.registry.get(sandbox_id)
    except UnknownSandboxError:
        return None


@pytest.fixture()
def worker_teardown(monkeypatch):
    """The control plane's hop to the *worker*: recorded instead of dialled."""
    removed: list[tuple[str, bool]] = []

    async def _fake(request, record, node, *, force=False):
        removed.append((record.sandbox_id, force))
        return SimpleNamespace(acknowledged=True, deferred=False)

    monkeypatch.setattr(sandboxes, "_destroy_remote", _fake)
    return removed


@pytest.fixture(autouse=True)
def worker_provision(monkeypatch):
    """The hop to the *worker* for the create itself: not this lane's subject.

    Left alone it would dial the fixture's synthetic address and sit in a
    connect timeout (its own lane covers the wire).
    """
    hops: list[tuple[str, bool]] = []

    async def _fake(request, record, node, settings, snapshot, volume_mounts, **kw):
        hops.append((record.sandbox_id, kw.get("materialized")))

    monkeypatch.setattr(sandboxes, "_provision_remote", _fake)
    return hops


# ---------------------------------------------------------------- the window


@pytest.mark.asyncio
async def test_a_delete_during_materialization_waits_for_the_create_then_removes_both(
    workspace: Path, worker_teardown
) -> None:
    """The ordinary case: the wait is short, and both halves are gone after."""
    shape = _Cp(workspace)
    agent = _HeldMaterialize()
    app = _app(shape, agent=agent)
    await _register(app)

    create = asyncio.create_task(_create(app))
    await asyncio.wait_for(agent.entered.wait(), 5)
    delete = asyncio.create_task(_delete(app))
    # The teardown must be *waiting*, not already done: it would otherwise tear
    # down a tree the create is still writing to.
    await asyncio.sleep(0.2)
    assert delete.done() is False

    agent.release.set()
    assert (await create).status_code == 201
    assert (await delete).status_code == 204

    assert worker_teardown == [(SANDBOX, False)]
    assert _register_record(app) is None


@pytest.mark.asyncio
async def test_a_delete_that_gives_up_leaves_no_record_and_no_tree(
    workspace: Path, worker_teardown
) -> None:
    """A create slower than the bound is reclaimed as unfinished, not kept.

    The create is still running when the delete gives up, so the delete cannot
    ``registry.save`` anything on its behalf -- what must hold is that the
    create itself does not get to keep a record afterwards (that is the
    "record on disk, tree gone" residue). It is marked abandoned, and the tree
    it materialized is left to the orphan sweep, which is exactly the path an
    unfinished create already takes.
    """
    shape = _Cp(workspace, create_window_wait_s=1)
    agent = _HeldMaterialize()
    app = _app(shape, agent=agent)
    await _register(app)

    create = asyncio.create_task(_create(app))
    await asyncio.wait_for(agent.entered.wait(), 5)
    resp = await _delete(app)

    assert resp.status_code in (204, 404)
    agent.release.set()
    create_resp = await create
    # Either the create was refused outright, or it finished without a record:
    # what may not happen is a record for a tree the delete already removed.
    assert create_resp.status_code >= 200
    assert _register_record(app) is None
    # ...and it must not leave a *running* sandbox behind either (review I6):
    # the delete gave up before the create provisioned, so the worker has a
    # runtime registered for a sandbox the control plane has forgotten. The
    # orphan sweep only reclaims **trees** (``remove-orphan-workspace``), so the
    # create has to take its own runtime down -- with ``force``, because by then
    # the record it would be verified against is gone.
    assert worker_teardown == [(SANDBOX, False), (SANDBOX, True)]


@pytest.mark.asyncio
async def test_a_completed_create_is_never_removed_by_a_late_delete(
    workspace: Path, worker_teardown
) -> None:
    """The claim is released on success: a later delete is a plain delete."""
    shape = _Cp(workspace)
    agent = _HeldMaterialize()
    app = _app(shape, agent=agent)
    await _register(app)

    create = asyncio.create_task(_create(app))
    await asyncio.wait_for(agent.entered.wait(), 5)
    agent.release.set()
    assert (await create).status_code == 201
    assert _register_record(app) is not None

    resp = await _delete(app)

    assert resp.status_code == 204
    assert worker_teardown == [(SANDBOX, False)]
    assert _register_record(app) is None


@pytest.mark.asyncio
async def test_a_delete_on_the_other_replica_does_not_resurrect_the_record(
    workspace: Path, worker_teardown
) -> None:
    """The window has to be closed across replicas too (review C2).

    The control plane ships ``replicas: 2`` and load-balances, so the delete
    that races a create is usually answered by the *other* pod. The in-process
    claim cannot see it -- what both replicas do share is the registry, and the
    create therefore confirms, before it keeps a record, that the record in
    that store is still the one it made.

    The store here is the *shared* shape (``record_store=``): reads go through
    it and writes go through it, so with the shared check stubbed out this test
    goes red with the record **back** -- which is the reproduction review C2
    describes (a 204 for the client, and a sandbox that is still listed).

    Measured, because an earlier pass recorded the opposite and stopped
    looking (the harness that keeps this honest is
    ``test_the_cross_replica_shape_resurrects_the_record_without_the_check``):
    in this shape **nothing else answers 409 first**. With the check stubbed,
    no ``OfficialError(409)`` is raised anywhere in the flow -- the attempt
    reaches ``registry.save``, answers 201, and the record is back in the
    shared store. With it real, the one and only 409 is the window branch in
    ``control_plane/api/sandboxes.py``, which is why the assertion below pins
    that branch's message rather than just the status code.
    """
    shape = _Cp(workspace)
    agent = _HeldMaterialize()
    store = _SharedRecordStore()
    registry = SandboxRegistry(shape.settings, record_store=store)
    replica_a = _app(shape, agent=agent, registry=registry)
    replica_b = _app(shape, agent=agent, registry=registry)
    await _register(replica_a)
    await _register(replica_b)

    create = asyncio.create_task(_create(replica_a))
    await asyncio.wait_for(agent.entered.wait(), 5)
    # The other replica knows nothing about the claim: it answers at once.
    assert (await _delete(replica_b)).status_code == 204

    agent.release.set()
    create_resp = await create
    assert create_resp.status_code == 409, create_resp.text
    # The *branch* that decided, not merely "some 409": a teardown that landed
    # between the materialization and the record has to be the reason.
    assert create_resp.json()["message"] == (
        f"Sandbox {SANDBOX} was deleted while it was being created"
    )
    # The shared store is the authority in this shape, so that is where the
    # absence has to be read from.
    assert store.records == {}
    try:
        assert registry.get(SANDBOX) is not None
    except UnknownSandboxError:
        pass
    else:  # pragma: no cover - the failure this test exists to catch
        raise AssertionError("the record was resurrected after the delete")
    # ...and the runtime this create registered is taken down by the create
    # itself (review I6), not left for a sweep that only reclaims trees.
    assert worker_teardown == [(SANDBOX, False), (SANDBOX, True)]


@pytest.mark.asyncio
async def test_the_cross_replica_shape_resurrects_the_record_without_the_check(
    workspace: Path, worker_teardown, monkeypatch
) -> None:
    """The negative control for the test above: disable the check, get the residue.

    "The cross-replica test passes" is only evidence if the same shape *fails*
    when the check is disabled, so this runs that shape with
    ``_record_is_still_ours`` pinned to ``True`` and asserts the residue review
    C2 describes: the delete was answered 204, the create went on to
    ``registry.save`` anyway, answered 201, and the record -- the very same one,
    by ``started_at`` -- is back in the shared store. The client has a sandbox
    it was told was gone.

    Two things this pins beyond the sibling test:

    * the stub has to bind to the *call site* (the module-level name). An
      earlier pass patched a symbol that was not the call site, applied
      cleanly, changed nothing, and produced the opposite conclusion -- see
      the docstring of the sibling test;
    * the residue is reachable here at all, i.e. the shared store really does
      write through (``_SharedRecordStore.put``), so the sibling test is not
      green because the store quietly refuses the create's write.

    If a future change adds a second defence and this control goes red, that is
    the signal to re-measure -- not to weaken the sibling test.
    """
    monkeypatch.setattr(sandboxes, "_record_is_still_ours", lambda *a, **k: True)

    shape = _Cp(workspace)
    agent = _HeldMaterialize()
    store = _SharedRecordStore()
    registry = SandboxRegistry(shape.settings, record_store=store)
    replica_a = _app(shape, agent=agent, registry=registry)
    replica_b = _app(shape, agent=agent, registry=registry)
    await _register(replica_a)
    await _register(replica_b)

    create = asyncio.create_task(_create(replica_a))
    await asyncio.wait_for(agent.entered.wait(), 5)
    created = store.records[SANDBOX]["started_at"]
    assert (await _delete(replica_b)).status_code == 204
    assert store.records == {}

    agent.release.set()
    create_resp = await create
    assert create_resp.status_code == 201, create_resp.text
    assert list(store.records) == [SANDBOX]
    # The *same* record, not a fresh one: ``started_at`` is the nonce the check
    # compares, and the create's own record is the one that came back.
    assert store.records[SANDBOX]["started_at"] == created
    assert registry.get(SANDBOX) is not None
    # Nothing tore the runtime down: the branch that would have (I6) never ran.
    assert worker_teardown == [(SANDBOX, False)]


def test_the_shared_check_sees_a_record_someone_else_removed(workspace: Path) -> None:
    """The pin for ``_record_is_still_ours`` itself.

    Two facts have to hold for the cross-replica half of §4.5 to mean anything:
    a record that a teardown removed is no longer ours (so the create does not
    write it back), and the record we did create is ours (so an ordinary create
    keeps it). The nonce is ``started_at``, which the store round-trips.
    """
    shape = _Cp(workspace)
    registry = SandboxRegistry(shape.settings)
    app = _app(shape, agent=_HeldMaterialize(), registry=registry)
    _ = app
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

    assert sandboxes._record_is_still_ours(registry, record) is True

    registry.delete(SANDBOX)
    assert sandboxes._record_is_still_ours(registry, record) is False

    # Another record with the same id (a re-create) is not ours either: that is
    # the difference ``started_at`` buys, and a bare "does it exist" check would
    # miss it.
    replacement = registry.create(
        template_id="base",
        sandbox_id=SANDBOX,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    assert replacement.started_at != record.started_at
    assert sandboxes._record_is_still_ours(registry, record) is False
    assert sandboxes._record_is_still_ours(registry, replacement) is True
