"""Task C: (b) -- the worker starts while the tree is still being made.

The create path used to be serial: the control plane materialized the tree on
the node's agent (p50 79.6 ms measured 2026-10-01) and only then dialled the
worker, which spent another ~78 ms on its own halves. (b) fires both at once
(``asyncio.gather``) and keeps the create's contract **synchronous**: the
worker's ``prepare`` half -- the work that never needed the tree (the
``.creating`` marker, the uid claim, the disk-accounting seed) -- runs beside
the agent's materialization, and one ``finalize`` half follows it once the tree
is there (design v2 §4.6; the completion signal is Task B's decision, measured
in ``tmp/b-two-phase-measurement.md`` §5).

The win is ``min(materialize, prepare)``: the two legs overlap, so only the
longer one is on the critical path. Three properties this file is really about,
because each is a way the change could be wrong while still "working" on the
happy path:

* **the overlap is real** -- a create whose materialization is held open must by
  then already have dialled the worker, or (b) bought nothing;
* **the failure face stays synchronous** -- a materialization that fails, or
  that outruns its deadline, fails *the create*, and the worker's prepared half
  is taken back rather than left on the node as a "prepared, never finalized"
  sandbox (design §4.5);
* **the old path is untouched** -- a create the control plane cannot split (no
  agent client, no verified worker identity, no host uid) is the same single
  ``POST /agent/sandboxes`` it was before, with no ``phase`` in the body.

The worker half of the split is pinned here too: ``prepare`` must not register
anything, and ``cancel`` must give back everything ``prepare`` took.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.c3_agent_client import AgentClientError
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry, UnknownSandboxError
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.volumes import VolumeRegistry
from control_plane.worker_identity_source import StaticWorkerIdentitySource

from envd_service import agent as agent_module
from envd_service import agent_fileops
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry

WORKER = "e2b-worker-0"
#: The agent's own identity -- the host it runs on (``spec.nodeName``). Kept
#: different from the worker's on purpose: a fixture where the two are equal
#: cannot see an instruction addressed to the wrong one.
HOST = "k0s-worker-0"
KEY_A = "key-node-a"
AGENT_TOKEN = "agent-token-0123456789"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
UID_X = 10007
WORKER_UID = 65534
WORKER_GID = 65534
SANDBOX = "sbx_twophase"


# --------------------------------------------------------------------------- #
# the control plane's half
# --------------------------------------------------------------------------- #


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        internal_api_key="fleet-key",
        internal_api_keys=(),
        internal_node_keys={KEY_A: WORKER},
        c3_agent_token=AGENT_TOKEN,
        # This lane is about the remote create path; the shipped default would
        # land every create on ``local://`` and never send an instruction.
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
    """One test's control plane, with the four roots inside its workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.workspace_base = workspace / "workspaces"
        self.state_base = workspace / "state"
        self.image_cache = workspace / "_images"
        for path in (self.workspace_base, self.state_base, self.image_cache):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = _settings(
            workspace_base=self.workspace_base,
            state_base=self.state_base,
            image_cache_dir=self.image_cache,
            shared_volume_root=str(workspace),
        )
        self.volumes = VolumeRegistry(workspace / "_volumes_base")


class _Worker:
    """The node's worker, as the control plane sees it: one POST, one phase.

    A transport rather than a stub for ``_provision_remote``: the phases are the
    *wire* contract (``phase`` in the body), so the thing under test has to be
    the real function that builds the body and the real order it is called in.
    """

    def __init__(
        self, *, fail_prepare: bool = False, cancel_unreachable: bool = False
    ) -> None:
        self.calls: list[dict] = []
        self.fail_prepare = fail_prepare
        self.cancel_unreachable = cancel_unreachable
        #: Set as soon as the worker's prepare has been *received* -- the agent
        #: stub waits on it to prove the two legs really overlap.
        self.prepare_seen = asyncio.Event()

    def phases(self) -> list[str | None]:
        return [call.get("phase") for call in self.calls]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        phase = body.get("phase")
        if phase == "prepare":
            self.prepare_seen.set()
            if self.fail_prepare:
                return httpx.Response(500, content="prepare exploded")
            return httpx.Response(200, json={"phase": "prepare"})
        if phase == "cancel":
            if self.cancel_unreachable:
                raise httpx.ConnectError("no route to the worker")
            return httpx.Response(204)
        return httpx.Response(201, json={})


class _Agent:
    """The node's agent: records the instruction, and can be held open."""

    def __init__(self, *, refuse=None, status_code=502, hold=None, hang=False):
        self.calls: list[dict] = []
        self._refuse = refuse
        self._status_code = status_code
        self._hold = hold
        self._hang = hang

    async def materialize(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._hold is not None:
            await self._hold()
        if self._hang:
            await asyncio.sleep(3600)
        if self._refuse is not None:
            raise AgentClientError(self._refuse, status_code=self._status_code)
        return {"op": "materialize"}


def _app(shape: _Cp, *, client, worker: _Worker):
    app = create_control_app(
        settings=shape.settings,
        registry=SandboxRegistry(shape.settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=shape.volumes,
        workspace_base=shape.workspace_base,
        node_address_resolver=StaticAddressResolver({WORKER: ENDPOINT_A}),
        c3_agent_client=client,
        worker_identity_source=StaticWorkerIdentitySource(
            {WORKER: (WORKER_UID, WORKER_GID)}
        ),
    )
    # The create's hop to the worker rides the app's shared keep-alive client
    # (``app.state.remote_http``, built in the lifespan that a test-built app
    # does not run), so pointing it at the fake worker is what makes the *phase*
    # sequence observable at the wire.
    app.state.remote_http = httpx.AsyncClient(
        transport=httpx.MockTransport(worker.handler)
    )
    return app


def _client(app, *, source_ip: str = "10.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _register_node(app) -> None:
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": KEY_A},
            json={
                "nodeID": WORKER,
                "address": ENDPOINT_A.address,
                # Generous: a node sized to one sandbox turns every create into
                # an admission wait instead of the thing under test.
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


async def _create(app, sandbox_id: str = SANDBOX):
    async with _client(app) as client:
        return await client.post(
            "/sandboxes",
            json={"templateID": "base", "sandboxID": sandbox_id},
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": sandbox_id},
        )


@pytest.mark.asyncio
async def test_the_worker_starts_while_the_tree_is_still_being_made(
    workspace: Path,
) -> None:
    """The whole point of (b): the two legs overlap.

    The agent's materialization is held open until the worker's prepare has
    been *received*. A serial create cannot get there -- it would wait for the
    materialization first -- so the hold's own timeout is the assertion: if the
    control plane dialled the worker only after the tree was made, this create
    would fail instead of overlapping.
    """
    shape = _Cp(workspace)
    worker = _Worker()

    async def _hold() -> None:
        await asyncio.wait_for(worker.prepare_seen.wait(), timeout=2.0)

    agent = _Agent(hold=_hold)
    app = _app(shape, client=agent, worker=worker)
    await _register_node(app)

    resp = await _create(app)

    assert resp.status_code == 201, resp.text
    # One half each, in order: the prepare leg, then the tree-dependent close.
    assert worker.phases() == ["prepare", "finalize"]
    # The agent accepted the instruction, so the close is told the tree is
    # there -- the flag is the whole contract between the two halves.
    assert worker.calls[1]["materialized"] is True
    # ...and the prepare leg is not told: it runs before the answer exists, and
    # it does no tree work either way.
    assert "materialized" not in worker.calls[0]
    assert len(agent.calls) == 1
    assert agent.calls[0]["node_id"] == WORKER


@pytest.mark.asyncio
async def test_a_failed_materialization_fails_the_create_and_undoes_the_worker(
    workspace: Path,
) -> None:
    """The failure face stays synchronous (design v2 §4.6).

    A materialization the agent refuses is a create that did not happen -- never
    "201, and the first command explodes". The worker's prepared half is taken
    back in the same breath, or the node keeps a sandbox that is prepared and
    will never be finalized.
    """
    shape = _Cp(workspace)
    worker = _Worker()
    agent = _Agent(refuse="path-outside-roots: refusing", status_code=400)
    app = _app(shape, client=agent, worker=worker)
    await _register_node(app)

    resp = await _create(app)

    assert resp.status_code == 400
    assert resp.json()["message"] == "path-outside-roots: refusing"
    assert worker.phases() == ["prepare", "cancel"]
    # No record survives: the rollback every provisioning failure takes.
    with pytest.raises(UnknownSandboxError):
        app.state.registry.get(SANDBOX)


@pytest.mark.asyncio
async def test_a_slow_materialization_is_bounded_and_named(workspace: Path) -> None:
    """A stuck agent must not read as "the create hangs" (D9.5).

    The instruction has its own deadline
    (``E2B_C3_AGENT_MATERIALIZE_TIMEOUT_S``, design §4.6) and outrunning it is a
    **named** refusal, not a create that waits forever -- and the worker's
    prepared half is still taken back first.
    """
    shape = _Cp(workspace)
    shape.settings.c3_agent_materialize_timeout_s = 0.2
    worker = _Worker()
    agent = _Agent(hang=True)
    app = _app(shape, client=agent, worker=worker)
    await _register_node(app)

    # The bound is the test's too: before the control plane had one of its own,
    # this create simply never came back (the agent sleeps for an hour), and a
    # red test that hangs is not a red test.
    resp = await asyncio.wait_for(_create(app), timeout=5.0)

    assert resp.status_code == 504
    assert "did not answer within 0.2s" in resp.json()["message"]
    assert worker.phases() == ["prepare", "cancel"]
    with pytest.raises(UnknownSandboxError):
        app.state.registry.get(SANDBOX)


@pytest.mark.asyncio
async def test_a_plain_create_still_takes_the_old_path(workspace: Path) -> None:
    """No split is attempted when there is nothing to overlap with.

    Without an agent client the control plane cannot express the instruction at
    all, so the worker gets the same single call it has always got -- no
    ``phase``, and the create is not made slower by a handshake that cannot
    happen (review C1's rule: never claim a tree nobody made).
    """
    shape = _Cp(workspace)
    worker = _Worker()
    app = _app(shape, client=None, worker=worker)
    app.state.c3_agent_client = None
    await _register_node(app)

    resp = await _create(app)

    assert resp.status_code == 201, resp.text
    assert len(worker.calls) == 1
    assert "phase" not in worker.calls[0]
    assert worker.calls[0]["materialized"] is False


@pytest.mark.asyncio
async def test_an_agent_that_cannot_take_it_falls_back_to_the_single_call(
    workspace: Path,
) -> None:
    """A degrade mid-flight leaves one code path for "the worker builds it".

    An older agent (no ``materialize`` op) or a busy one is not a failed create
    (review I3/I5). The prepared half is abandoned -- the worker is told to take
    it back -- and the create then runs the *old* path, whole, in one call: the
    tree, the slices and the ownership are built by the party that builds them
    when there is no plan to hand over.
    """
    shape = _Cp(workspace)
    worker = _Worker()
    agent = _Agent(
        refuse="the agent for node e2b-worker-0 refused the materialize (HTTP "
        "404): unknown agent op 'materialize'",
        status_code=404,
    )
    app = _app(shape, client=agent, worker=worker)
    await _register_node(app)

    resp = await _create(app)

    assert resp.status_code == 201, resp.text
    assert worker.phases() == ["prepare", "cancel", None]
    assert worker.calls[-1]["materialized"] is False
    assert "phase" not in worker.calls[-1]


@pytest.mark.asyncio
async def test_a_failed_prepare_fails_the_create(workspace: Path) -> None:
    """The prepare leg is part of the create's contract too.

    If the worker cannot do its half, the create fails with the worker's own
    reason -- there is no "the tree was made, so it is fine": the prepared half
    *is* what reserves the sandbox's uid and publishes its accounting.
    """
    shape = _Cp(workspace)
    worker = _Worker(fail_prepare=True)
    agent = _Agent()
    app = _app(shape, client=agent, worker=worker)
    await _register_node(app)

    resp = await _create(app)

    assert resp.status_code == 502
    assert "prepare exploded" in resp.json()["message"]
    with pytest.raises(UnknownSandboxError):
        app.state.registry.get(SANDBOX)


@pytest.mark.asyncio
async def test_a_cancel_that_cannot_be_delivered_does_not_replace_the_reason(
    workspace: Path,
) -> None:
    """The undo is best effort; the failure is still the agent's.

    A node that cannot be reached for the cancel must not turn "the agent
    refused the tree" into "the agent is unreachable" -- the operator needs the
    reason the create failed, and the marker the cancel could not clear is what
    a later teardown (or the orphan sweep) already knows how to reclaim.
    """
    shape = _Cp(workspace)
    worker = _Worker(cancel_unreachable=True)
    agent = _Agent(refuse="path-outside-roots: refusing", status_code=400)
    app = _app(shape, client=agent, worker=worker)
    await _register_node(app)

    resp = await _create(app)

    assert resp.status_code == 400
    assert resp.json()["message"] == "path-outside-roots: refusing"
    assert worker.phases() == ["prepare", "cancel"]


# --------------------------------------------------------------------------- #
# the worker's half: prepare / finalize / cancel
# --------------------------------------------------------------------------- #

WORKER_SANDBOX = "sbx_twophase_w"
WORKER_KEY = "internal-key"
WORKER_HOST_UID = 10000
WORKER_SNAPSHOT = "snap_0123456789abcdef"


class _RelayStub:
    def __init__(self) -> None:
        self.relayed: list[tuple[str, str, bool]] = []

    def chown_workspace(self, sandbox_id: str, *, recursive: bool = True) -> None:
        self.relayed.append(("chown-workspace", sandbox_id, recursive))

    def close(self) -> None:
        return None


def _worker_app(workspace: Path, monkeypatch, *, relay=None):
    """The worker's own app, with the face-B client stubbed out.

    ``create_app`` installs the agent-shaped file-op client when the transport
    says so; isolated first, or that client lands on the module's own list
    object and every later test in the suite dials a control plane that does
    not exist (the lesson this file's sibling lane records).
    """
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [None])
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "agent")
    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", "http://control-plane:3000")
    monkeypatch.setenv("E2B_NODE_ID", WORKER)
    monkeypatch.delenv("E2B_PER_SANDBOX_UID", raising=False)
    workspace_base = workspace / "workspaces"
    state_base = workspace / "state"
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace_base,
        state_base=state_base,
        shared_volume_root=None,
        internal_api_key=WORKER_KEY,
    )
    app = create_envd_app(
        settings=settings,
        runtime_registry=RuntimeRegistry(workspace_base, state_base=state_base),
        workspace_base=workspace_base,
    )
    stub = relay if relay is not None else _RelayStub()
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [stub])
    return app, settings, stub


async def _worker_post(app, body: dict) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        # A create payload always carries the sandbox's envd access token (the
        # control plane sends it as "accessToken"); the worker refuses a create
        # without one (SEC-R3-01). Injected here so every call site inherits it.
        return await client.post(
            "/agent/sandboxes",
            json={"accessToken": "tok", **body},
            headers={"X-Internal-Key": WORKER_KEY},
        )


def _marker(settings: EnvdSettings, sandbox_id: str) -> Path:
    from gateway_common.paths import sandbox_creating_marker

    return sandbox_creating_marker(
        settings.workspace_base, sandbox_id, state_base=settings.state_base
    )


@pytest.mark.asyncio
async def test_the_worker_prepare_announces_the_create_without_a_record(
    workspace: Path, monkeypatch
) -> None:
    """prepare is the window, not the sandbox.

    It claims the uid and publishes the accounting, and it leaves the marker --
    but it writes **no record**: a control plane that dies between the two hops
    must not leave behind a record for a sandbox nobody finished (design §4.5,
    the residue Task 5 closed).
    """
    app, settings, _ = _worker_app(workspace, monkeypatch)
    runtime_registry = app.state.runtime_registry

    resp = await _worker_post(
        app,
        {"sandboxID": WORKER_SANDBOX, "hostUID": WORKER_HOST_UID, "phase": "prepare"},
    )

    assert resp.status_code == 200
    assert _marker(settings, WORKER_SANDBOX).exists()
    assert runtime_registry.get(WORKER_SANDBOX) is None
    assert WORKER_HOST_UID in runtime_registry.uid_pool.allocated_uids()


@pytest.mark.asyncio
async def test_a_cancel_gives_back_everything_prepare_took(
    workspace: Path, monkeypatch
) -> None:
    """The undo of the prepared half, and nothing else.

    The uid reservation goes back to the pool and the marker comes off, so a
    later ``DELETE`` of that id does not wait a full create bound on a create
    that will never finish. The tree is not this half's to remove: when the
    control plane could not send the instruction, nothing made one.
    """
    app, settings, _ = _worker_app(workspace, monkeypatch)
    runtime_registry = app.state.runtime_registry
    await _worker_post(
        app,
        {"sandboxID": WORKER_SANDBOX, "hostUID": WORKER_HOST_UID, "phase": "prepare"},
    )
    assert WORKER_HOST_UID in runtime_registry.uid_pool.allocated_uids()

    resp = await _worker_post(app, {"sandboxID": WORKER_SANDBOX, "phase": "cancel"})

    assert resp.status_code == 204
    assert WORKER_HOST_UID not in runtime_registry.uid_pool.allocated_uids()
    assert not _marker(settings, WORKER_SANDBOX).exists()
    assert runtime_registry.get(WORKER_SANDBOX) is None


@pytest.mark.asyncio
async def test_the_finalize_half_does_the_tree_work_prepare_could_not(
    workspace: Path, monkeypatch
) -> None:
    """finalize is where the tree-dependent work lives, and only there.

    With the materialized flag the worker touches no tree at all (the agent's
    instruction already made it and handed it over); without it, the very same
    half builds the tree and runs the ownership hand-over -- which is the
    degrade path, and the reason the single old call and the split share one
    implementation.
    """
    built: list[tuple] = []
    monkeypatch.setattr(
        agent_module.shutil, "copytree", lambda *args, **kwargs: built.append(args)
    )
    app, settings, relay = _worker_app(workspace, monkeypatch)
    snapshot_fs = (
        workspace / "workspaces" / "_snapshots" / WORKER_SNAPSHOT / "fs" / "workspace"
    )
    snapshot_fs.mkdir(parents=True, exist_ok=True)
    (snapshot_fs / "kept.txt").write_text("kept\n", encoding="utf-8")

    resp = await _worker_post(
        app,
        {
            "sandboxID": WORKER_SANDBOX,
            "hostUID": WORKER_HOST_UID,
            "snapshotID": WORKER_SNAPSHOT,
            "phase": "prepare",
        },
    )
    assert resp.status_code == 200
    finalize = await _worker_post(
        app,
        {
            "sandboxID": WORKER_SANDBOX,
            "hostUID": WORKER_HOST_UID,
            "snapshotID": WORKER_SNAPSHOT,
            "phase": "finalize",
            "materialized": True,
        },
    )

    assert finalize.status_code == 201
    assert built == []
    assert relay.relayed == []
    assert app.state.runtime_registry.get(WORKER_SANDBOX) is not None

    # The other direction: no flag means the worker builds the tree itself,
    # exactly as the single-shot route has always done it.
    other = "sbx_twophase_w2"
    await _worker_post(
        app,
        {
            "sandboxID": other,
            "hostUID": WORKER_HOST_UID + 1,
            "snapshotID": WORKER_SNAPSHOT,
            "phase": "prepare",
        },
    )
    plain = await _worker_post(
        app,
        {
            "sandboxID": other,
            "hostUID": WORKER_HOST_UID + 1,
            "snapshotID": WORKER_SNAPSHOT,
            "phase": "finalize",
        },
    )

    assert plain.status_code == 201
    assert len(built) == 1
    assert relay.relayed == [("chown-workspace", other, True)]


@pytest.mark.asyncio
async def test_an_unknown_phase_is_refused_by_name(
    workspace: Path, monkeypatch
) -> None:
    """A body this route does not understand must not be read as a plain create."""
    app, _, _ = _worker_app(workspace, monkeypatch)

    resp = await _worker_post(app, {"sandboxID": WORKER_SANDBOX, "phase": "whenever"})

    assert resp.status_code == 400
    assert "whenever" in resp.text
