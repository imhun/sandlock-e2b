"""C3 Task 4: the worker's file operations reach the agent, not a worker binary.

The worker sends ``{sandbox_id, op}`` and **nothing else that names a target**
(hard rules 1/3, §14.4). Everything below is the control plane's half of that:
the op vocabulary, the path/uid derivation from its own records and settings,
the two layers of root containment, and the agent hop's typed failures.

The test that matters most is the table: one row per op, asserting the *exact*
verb and parameters that reach the agent and that the derived path lands inside
the four roots -- that is the brief's "每类操作最终都落在 agent 的路径白名单四根
之内", pinned per operation rather than once in prose.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.c3_agent_client import AgentClientError
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.volumes import VolumeRegistry

KEY_A = "key-node-a"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
NODE_A = "node_a"
PID_NAMESPACE = "pid:[4026532458]"
UID_X = 10007
WORKER_UID = 65534
WORKER_GID = 65534
SANDBOX = "sbx_forward"
VOLUME_NAME = "data"
#: The mount payload's ``name`` field is the volume **id** (``vol_…``), which is
#: what the worker sends and what the control plane resolves
#: (``volumes.get(name)``); the display name is not a key. The table below says
#: "the volume this fixture created" and the test substitutes the id, so a map
#: keyed by the display name cannot pass it (review Task 4 slice A, Important 1).
VOLUME_PLACEHOLDER = "<volume-id>"
FLEET_KEY = "fleet-key"


class _StubAgentClient:
    """Records the instruction; answers like the agent would (or refuses)."""

    def __init__(
        self,
        *,
        refuse: str | None = None,
        status_code: int = 502,
        stdout: str = "",
    ) -> None:
        self.calls: list[dict] = []
        self._refuse = refuse
        self._status_code = status_code
        self._stdout = stdout

    def _record(self, verb: str, kwargs: dict) -> dict:
        self.calls.append({"verb": verb, **kwargs})
        if self._refuse is not None:
            raise AgentClientError(self._refuse, status_code=self._status_code)
        answer = {"op": verb, "path": kwargs["path"]}
        if verb == "walk":
            answer["stdout"] = self._stdout
        return answer

    async def chown(self, **kwargs) -> dict:
        return self._record("chown", kwargs)

    async def rm(self, **kwargs) -> dict:
        return self._record("rm", kwargs)

    async def walk(self, **kwargs) -> dict:
        return self._record("walk", kwargs)


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        internal_api_key=FLEET_KEY,
        internal_api_keys=(),
        internal_node_keys={KEY_A: NODE_A},
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


class _C3Shape:
    """One test's control plane, with the four roots inside its workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.workspace_base = workspace / "workspaces"
        self.state_base = workspace / "state"
        self.image_cache = workspace / "_images"
        self.shared_root = workspace
        self.route_b = workspace / "state" / ".route-b"
        for path in (self.workspace_base, self.state_base, self.image_cache, self.route_b):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = _settings(
            workspace_base=self.workspace_base,
            state_base=self.state_base,
            image_cache_dir=self.image_cache,
            shared_volume_root=str(self.shared_root),
            route_b_tmp_root=str(self.route_b),
        )
        self.volumes = VolumeRegistry(workspace / "_volumes_base")
        self.volume = self.volumes.create(name=VOLUME_NAME)

    def roots(self) -> tuple[Path, ...]:
        return (
            self.workspace_base,
            self.state_base,
            self.shared_root,
            self.image_cache,
        )

    def workspace_dir(self) -> Path:
        return self.workspace_base / SANDBOX

    def runtime_dir(self) -> Path:
        return self.state_base / "_runtime" / SANDBOX

    def checkpoint_dir(self) -> Path:
        return self.state_base / "_runtime" / ".checkpoints" / SANDBOX

    def secret_path(self, name: str = "github") -> Path:
        return self.image_cache / "secrets" / SANDBOX / f"{name}.secret"

    def slot_document(self, name: str = "policy.json") -> Path:
        return self.route_b / str(UID_X) / f"rb-{SANDBOX}" / name


def _app(shape: _C3Shape, *, client) -> tuple:
    app = create_control_app(
        settings=shape.settings,
        registry=SandboxRegistry(shape.settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=shape.volumes,
        workspace_base=shape.workspace_base,
        node_address_resolver=StaticAddressResolver({NODE_A: ENDPOINT_A}),
        c3_agent_client=client,
    )
    return app


def _client(app, *, source_ip: str = "10.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _enroll(app, *, worker_identity: bool = True, host_uid: int | None = UID_X):
    """Register node A (with its worker identity) and put one sandbox on it."""
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
    record.node_id = NODE_A
    record.host_uid = host_uid
    registry.save(record)
    body = {
        "nodeID": NODE_A,
        "address": ENDPOINT_A.address,
        "totalMemoryMB": 1024,
        "totalCPUPercent": 100,
        "totalDiskMB": 1024,
        "totalProcesses": 64,
        "pidNamespace": PID_NAMESPACE,
    }
    if worker_identity:
        body["workerUID"] = WORKER_UID
        body["workerGID"] = WORKER_GID
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": KEY_A},
            json=body,
        )
    return resp


async def _file_op(app, body: dict, *, node_id: str = NODE_A):
    async with _client(app) as client:
        return await client.post(
            f"/internal/nodes/{node_id}/file-op",
            headers={"X-Internal-Key": KEY_A},
            json=body,
        )


# ------------------------------------------------ the worker's own identity


@pytest.mark.asyncio
async def test_registration_records_the_workers_uid_and_gid(workspace) -> None:
    shape = _C3Shape(workspace)
    app = _app(shape, client=_StubAgentClient())
    resp = await _enroll(app)
    assert resp.status_code == 200
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (WORKER_UID, WORKER_GID)


@pytest.mark.asyncio
async def test_a_half_identity_is_refused_at_registration(workspace) -> None:
    """Both halves or none: half an identity is not one."""
    shape = _C3Shape(workspace)
    app = _app(shape, client=_StubAgentClient())
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": KEY_A},
            json={
                "nodeID": NODE_A,
                "address": ENDPOINT_A.address,
                "workerUID": WORKER_UID,
            },
        )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": "workerGID must be a positive integer",
    }


@pytest.mark.asyncio
async def test_a_heartbeat_refreshes_the_workers_identity(workspace) -> None:
    shape = _C3Shape(workspace)
    app = _app(shape, client=_StubAgentClient())
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/heartbeat",
            headers={"X-Internal-Key": KEY_A},
            json={"workerUID": 65533, "workerGID": 65533},
        )
    assert resp.status_code == 204
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (65533, 65533)


# ------------------------------------------------------------- the vocabulary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, expected_call, path_family",
    [
        (
            {"op": "chown-workspace", "sandbox_id": SANDBOX, "recursive": True},
            {
                "verb": "chown",
                "uid": UID_X,
                "gid": WORKER_GID,
                "recursive": True,
                "worker_owned": False,
            },
            "workspace",
        ),
        (
            {"op": "remove-workspace", "sandbox_id": SANDBOX},
            {"verb": "rm"},
            "workspace",
        ),
        (
            {"op": "walk-workspace", "sandbox_id": SANDBOX},
            {"verb": "walk"},
            "workspace",
        ),
        (
            {"op": "remove-runtime", "sandbox_id": SANDBOX},
            {"verb": "rm"},
            "runtime",
        ),
        (
            {"op": "chown-checkpoint", "sandbox_id": SANDBOX, "recursive": True},
            {
                "verb": "chown",
                "uid": UID_X,
                "gid": WORKER_GID,
                "recursive": True,
                "worker_owned": False,
            },
            "checkpoint",
        ),
        (
            {"op": "remove-checkpoint", "sandbox_id": SANDBOX},
            {"verb": "rm"},
            "checkpoint",
        ),
        (
            {"op": "walk-checkpoint", "sandbox_id": SANDBOX},
            {"verb": "walk"},
            "checkpoint",
        ),
        (
            {
                "op": "chown-volume-slice",
                "sandbox_id": SANDBOX,
                "volume": VOLUME_PLACEHOLDER,
                "recursive": True,
            },
            {
                "verb": "chown",
                "uid": UID_X,
                "gid": WORKER_GID,
                "recursive": True,
                "worker_owned": False,
            },
            "volume_slice",
        ),
        (
            {
                "op": "chown-volume-root",
                "sandbox_id": SANDBOX,
                "volume": VOLUME_PLACEHOLDER,
            },
            {
                "verb": "chown",
                "uid": UID_X,
                "gid": WORKER_GID,
                "recursive": False,
                "worker_owned": False,
            },
            "volume_root",
        ),
        (
            {
                "op": "remove-volume-slice",
                "sandbox_id": SANDBOX,
                "volume": VOLUME_PLACEHOLDER,
            },
            {"verb": "rm"},
            "volume_slice",
        ),
        (
            {"op": "chown-secret", "sandbox_id": SANDBOX, "name": "github"},
            {
                "verb": "chown",
                "uid": UID_X,
                "gid": WORKER_GID,
                "recursive": False,
                "worker_owned": False,
            },
            "secret",
        ),
        (
            {
                "op": "scope-slot-document",
                "sandbox_id": SANDBOX,
                "name": "policy.json",
            },
            {
                "verb": "chown",
                "uid": None,
                "gid": UID_X,
                "recursive": False,
                "worker_owned": True,
            },
            "slot_document",
        ),
    ],
)
async def test_every_op_reaches_the_agent_with_its_exact_parameters(
    workspace, body, expected_call, path_family
) -> None:
    """One row per operation: the verb, the parameters, and the derived path.

    The path is asserted as the *family* the control plane's records name --
    the same value ``sandbox_runtime_dir`` / the volume registry / the settings
    produce -- and it is asserted to be inside the four roots. The worker's
    request named neither.
    """
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    if body.get("volume") == VOLUME_PLACEHOLDER:
        body = {**body, "volume": shape.volume.volume_id}
    resp = await _file_op(app, body)
    assert resp.status_code == 200
    expected_path = {
        "workspace": shape.workspace_dir(),
        "runtime": shape.runtime_dir(),
        "checkpoint": shape.checkpoint_dir(),
        "volume_slice": shape.volume.path / SANDBOX,
        "volume_root": shape.volume.path,
        "secret": shape.secret_path(),
        "slot_document": shape.slot_document(),
    }[path_family]
    expected_agent = {"op": expected_call["verb"], "path": str(expected_path)}
    expected_response = {
        "nodeID": NODE_A,
        "sandboxID": SANDBOX,
        "op": body["op"],
        "verb": expected_call["verb"],
        "path": str(expected_path),
        "agent": expected_agent,
    }
    if expected_call["verb"] == "walk":
        expected_agent["stdout"] = ""
        expected_response["stdout"] = ""
    assert resp.json() == expected_response
    assert agent.calls == [
        {
            "node_id": NODE_A,
            "sandbox_id": SANDBOX,
            "path": str(expected_path),
            "worker_uid": WORKER_UID,
            "worker_gid": WORKER_GID,
            **expected_call,
        }
    ]
    # The brief's four-root criterion, per operation.
    derived = Path(agent.calls[0]["path"]).resolve()
    assert any(
        derived == root.resolve() or derived.is_relative_to(root.resolve())
        for root in shape.roots()
    )


@pytest.mark.asyncio
async def test_a_walk_relays_the_entry_text(workspace) -> None:
    shape = _C3Shape(workspace)
    stdout = f"d {UID_X} {WORKER_GID} 770 512 {shape.workspace_dir()}\n"
    agent = _StubAgentClient(stdout=stdout)
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(app, {"op": "walk-workspace", "sandbox_id": SANDBOX})
    assert resp.status_code == 200
    assert resp.json()["stdout"] == stdout


@pytest.mark.asyncio
async def test_the_control_plane_never_removes_anything_itself(workspace) -> None:
    """D18.1, from the other side: the CP is a client, not an executor.

    The tree exists on disk; the op is answered by the agent (stub). If any CP
    code path still did the removal locally, the directory would be gone.
    """
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    tree = shape.workspace_dir()
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "workspace").mkdir()
    resp = await _file_op(app, {"op": "remove-workspace", "sandbox_id": SANDBOX})
    assert resp.status_code == 200
    assert tree.is_dir()
    assert (tree / "workspace").is_dir()
    assert agent.calls[0]["verb"] == "rm"


@pytest.mark.asyncio
async def test_a_report_that_names_a_target_is_refused(workspace) -> None:
    """Hard rule 3 on the wire: the worker may not name a path or a uid."""
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(
        app,
        {
            "op": "remove-workspace",
            "sandbox_id": SANDBOX,
            "path": str(shape.workspace_dir()),
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "a remove-workspace report carries no path: the target comes from "
            "the control plane's records"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_report_that_names_an_unknown_parameter_is_refused(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(
        app,
        {"op": "remove-workspace", "sandbox_id": SANDBOX, "recursive": True},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "a remove-workspace report carries no recursive: the target comes "
            "from the control plane's records"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_an_unknown_op_is_refused_by_name(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(app, {"op": "delete-tree", "sandbox_id": SANDBOX})
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "unknown file op 'delete-tree': the surface is "
            "chown-checkpoint, chown-secret, chown-volume-root, "
            "chown-volume-slice, chown-workspace, remove-checkpoint, "
            "remove-runtime, remove-volume-slice, remove-workspace, "
            "scope-slot-document, walk-checkpoint, walk-workspace"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_volume_the_control_plane_does_not_record_is_refused(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(
        app,
        {
            "op": "remove-volume-slice",
            "sandbox_id": SANDBOX,
            "volume": "not-a-volume",
        },
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "code": 404,
        "message": (
            "volume 'not-a-volume' is not a volume id this control plane records"
        ),
    }
    assert agent.calls == []

    # ...and the *display name* is not a key either: the map used to be keyed by
    # it, which made every volume op 404 while the request looked correct.
    display_name = await _file_op(
        app,
        {
            "op": "remove-volume-slice",
            "sandbox_id": SANDBOX,
            "volume": VOLUME_NAME,
        },
    )
    assert display_name.status_code == 404
    assert display_name.json() == {
        "code": 404,
        "message": (
            f"volume {VOLUME_NAME!r} is not a volume id this control plane "
            "records"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_secret_name_that_could_escape_its_directory_is_refused(
    workspace,
) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(
        app,
        {"op": "chown-secret", "sandbox_id": SANDBOX, "name": "../../../etc/passwd"},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "secret name '../../../etc/passwd' is not a valid secret name"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_slot_document_that_is_not_one_is_refused(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(
        app,
        {"op": "scope-slot-document", "sandbox_id": SANDBOX, "name": "token"},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "slot document 'token' is not one of policy.json, program.json"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_control_plane_without_a_route_b_root_refuses_by_name(
    workspace,
) -> None:
    shape = _C3Shape(workspace)
    shape.settings.route_b_tmp_root = ""
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(
        app,
        {
            "op": "scope-slot-document",
            "sandbox_id": SANDBOX,
            "name": "policy.json",
        },
    )
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            "this control plane names no route-B scratch root "
            "(E2B_ROUTE_B_TMP_ROOT), so it cannot derive a slot document's "
            "path: refusing"
        ),
    }
    assert agent.calls == []


# ---------------------------------------------------------------- the guards


@pytest.mark.asyncio
async def test_a_node_without_a_worker_identity_refuses_a_chown(workspace) -> None:
    """The group a tree is handed to comes from the node record or not at all."""
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app, worker_identity=False)
    resp = await _file_op(
        app, {"op": "chown-workspace", "sandbox_id": SANDBOX}
    )
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_A} has not reported the worker's own gid: refusing "
            "to hand a tree to a uid without the group it belongs to"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_sandbox_without_a_host_uid_is_refused(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app, host_uid=None)
    resp = await _file_op(
        app, {"op": "chown-workspace", "sandbox_id": SANDBOX}
    )
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"sandbox {SANDBOX} has no allocated host uid: refusing to "
            "instruct the agent"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_node_without_a_worker_identity_refuses_every_op(workspace) -> None:
    """A rollout window must be a named 503 for *every* op, not a 500.

    Every instruction carries the worker's identity (the ``--worker`` form *is*
    that identity, and the group a tree is handed to is the worker's own gid),
    so guarding only the chown arm let ``remove-*`` / ``walk-*`` reach
    ``int(None)`` -- a bare 500 exactly while a fleet is rolling, which is when
    an operator most needs the named refusal.
    """
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app, worker_identity=False)
    resp = await _file_op(app, {"op": "remove-workspace", "sandbox_id": SANDBOX})
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_A} has reported no worker identity (workerUID/"
            "workerGID): refusing to instruct the agent"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_the_identity_layer_guards_the_file_op_endpoint(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    body = {"op": "remove-workspace", "sandbox_id": SANDBOX}
    async with _client(app) as client:
        unauthenticated = await client.post(
            f"/internal/nodes/{NODE_A}/file-op", json=body
        )
    assert unauthenticated.status_code == 401
    async with _client(app, source_ip="10.0.0.2") as client:
        stolen = await client.post(
            f"/internal/nodes/{NODE_A}/file-op",
            headers={"X-Internal-Key": KEY_A},
            json=body,
        )
    assert stolen.status_code == 403
    assert stolen.json() == {
        "code": 403,
        "message": (
            f"request for node {NODE_A} came from 10.0.0.2, expected 10.0.0.1"
        ),
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_the_object_must_be_the_controls_own_record(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)
    await _enroll(app)
    unknown = await _file_op(
        app, {"op": "remove-workspace", "sandbox_id": "sbx_unknown"}
    )
    assert unknown.status_code == 404
    assert unknown.json() == {
        "code": 404,
        "message": "Sandbox sbx_unknown not found",
    }
    elsewhere = app.state.registry.get(SANDBOX)
    elsewhere.node_id = "node_b"
    app.state.registry.save(elsewhere)
    foreign = await _file_op(app, {"op": "remove-workspace", "sandbox_id": SANDBOX})
    assert foreign.status_code == 403
    assert foreign.json() == {
        "code": 403,
        "message": f"Sandbox {SANDBOX} belongs to node node_b, not node_a",
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_refusing_agent_is_named_and_fail_closed(workspace) -> None:
    shape = _C3Shape(workspace)
    agent = _StubAgentClient(
        refuse=(
            f"e2b-maint rm refused (exit 77): e2b-maint: refused: "
            f"{shape.workspace_dir()} is not under any privileged helper root"
        )
    )
    app = _app(shape, client=agent)
    await _enroll(app)
    resp = await _file_op(app, {"op": "remove-workspace", "sandbox_id": SANDBOX})
    assert resp.status_code == 502
    assert resp.json() == {
        "code": 502,
        "message": (
            f"e2b-maint rm refused (exit 77): e2b-maint: refused: "
            f"{shape.workspace_dir()} is not under any privileged helper root"
        ),
    }
    assert len(agent.calls) == 1


@pytest.mark.asyncio
async def test_a_control_plane_without_an_agent_client_refuses_by_name(
    workspace,
) -> None:
    shape = _C3Shape(workspace)
    app = _app(shape, client=None)
    await _enroll(app)
    resp = await _file_op(app, {"op": "remove-workspace", "sandbox_id": SANDBOX})
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            "this control plane has no C3 agent client configured: refusing "
            "to run remove-workspace"
        ),
    }
