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
from control_plane.worker_identity_source import (
    KernelWorkerIdentitySource,
    NoWorkerIdentitySource,
    StaticWorkerIdentitySource,
)
from gateway_common.paths import route_b_instance_name

KEY_A = "key-node-a"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
NODE_A = "node_a"
PID_NAMESPACE = "pid:[4026532458]"
#: The container identity a compose worker reports (D25): its hostname,
#: which the runtime sets to the first 12 characters of the container id.
CONTAINER_ID = "e4a98a0c5282"
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
        # The directory leaf is the worker's *instance name*, from the shared
        # rule (D20) -- not ``rb-<id>``, which was this test's own copy of a
        # rule the worker never used.
        return (
            self.route_b / str(UID_X) / route_b_instance_name(SANDBOX) / name
        )


def _app(shape: _C3Shape, *, client, worker_identity=None) -> tuple:
    app = create_control_app(
        settings=shape.settings,
        registry=SandboxRegistry(shape.settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=shape.volumes,
        workspace_base=shape.workspace_base,
        node_address_resolver=StaticAddressResolver({NODE_A: ENDPOINT_A}),
        c3_agent_client=client,
        # C3 Task 4, fourth review ②: the worker's own uid/gid is only ever
        # taken from a trusted source. The default here is "the deployment pins
        # exactly what the worker reports", i.e. the happy path; the tests that
        # are *about* the verification pass their own source.
        worker_identity_source=(
            StaticWorkerIdentitySource({NODE_A: (WORKER_UID, WORKER_GID)})
            if worker_identity is None
            else worker_identity
        ),
    )
    return app


def _client(app, *, source_ip: str = "10.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _enroll(
    app,
    *,
    worker_identity: bool = True,
    host_uid: int | None = UID_X,
    pid_namespace: bool = True,
    container_id: bool = True,
):
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
    }
    if pid_namespace:
        body["pidNamespace"] = PID_NAMESPACE
    if container_id:
        body["containerID"] = CONTAINER_ID
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
        "message": (
            "workerGID must be a positive integer: a worker may not run as "
            "root (uid 0) or report a non-identity, because the group a "
            "sandbox tree is handed to and the `--worker` form both mean "
            "*this* worker's own non-zero identity"
        ),
    }


@pytest.mark.asyncio
async def test_a_root_worker_identity_is_refused_with_the_real_reason(
    workspace,
) -> None:
    """m-1: a body that names uid 0 is refused, and the message says why.

    The shipped worker no longer *sends* this (see
    ``envd_service.worker_identity``: a root worker reports no identity at all,
    so its node stays joinable), which makes this arm the hostile-input guard --
    and the reason it names is the deployment fact an operator has to fix.
    """
    shape = _C3Shape(workspace)
    app = _app(shape, client=_StubAgentClient())
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": KEY_A},
            json={
                "nodeID": NODE_A,
                "address": ENDPOINT_A.address,
                "workerUID": 0,
                "workerGID": 0,
            },
        )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "workerUID must be a positive integer: a worker may not run as "
            "root (uid 0) or report a non-identity, because the group a "
            "sandbox tree is handed to and the `--worker` form both mean "
            "*this* worker's own non-zero identity"
        ),
    }


@pytest.mark.asyncio
async def test_a_heartbeat_refreshes_a_verified_workers_identity(workspace) -> None:
    shape = _C3Shape(workspace)
    # The deployment pins 65533 for this node, and the worker reports the same:
    # a *verified* value is what gets refreshed.
    app = _app(
        shape,
        client=_StubAgentClient(),
        worker_identity=StaticWorkerIdentitySource({NODE_A: (65533, 65533)}),
    )
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/heartbeat",
            headers={"X-Internal-Key": KEY_A},
            json={"workerUID": 65533, "workerGID": 65533},
        )
    assert resp.status_code == 200
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (65533, 65533)


# ------------------------------- ② the worker's claim is never the identity


@pytest.mark.asyncio
async def test_a_worker_claiming_another_identity_gets_nothing_stored(
    workspace,
) -> None:
    """Fourth review ②: a claim that disagrees with the trusted source is dropped.

    The identity face B uses is the one a tree (or a slot's credential-bearing
    ``policy.json``) is handed to, and in C1 it came from ``SO_PEERCRED``. The
    first cut of the agent took it from the worker's own register/heartbeat
    body, so a compromised worker could name **another tenant's** uid and have
    that tenant handed the file. Here the deployment pins
    ``65534:65534`` (the shipped image's USER) and the worker claims
    ``10007:10007``: the node records no identity at all, and the operation
    refuses by name without ever dialling the agent.
    """
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent)  # pins (WORKER_UID, WORKER_GID)
    await _enroll(app)

    # ...and now the worker "changes" its identity in a heartbeat.
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/heartbeat",
            headers={"X-Internal-Key": KEY_A},
            json={"workerUID": UID_X, "workerGID": UID_X},
        )
    assert resp.status_code == 200
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (WORKER_UID, WORKER_GID), (
        "the forged claim must never replace a verified identity"
    )

    forged = _C3Shape(workspace)
    agent2 = _StubAgentClient()
    app2 = _app(forged, client=agent2)
    # Registration itself with the wrong identity: nothing is stored.
    async with _client(app2) as client:
        registered = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": KEY_A},
            json={
                "nodeID": NODE_A,
                "address": ENDPOINT_A.address,
                "pidNamespace": PID_NAMESPACE,
                "workerUID": UID_X,
                "workerGID": UID_X,
            },
        )
    assert registered.status_code == 200
    node = app2.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (None, None)

    registry = app2.state.registry
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
    record.host_uid = UID_X
    registry.save(record)
    resp = await _file_op(
        app2,
        {"op": "chown-workspace", "sandbox_id": SANDBOX},
    )
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_A} has not reported the worker's own gid: refusing "
            "to hand a tree to a uid without the group it belongs to"
        ),
    }
    # Nothing was handed to the claimed uid: the agent was never dialled.
    assert agent2.calls == []


@pytest.mark.asyncio
async def test_a_shape_without_a_trusted_source_records_no_identity(
    workspace,
) -> None:
    """The compose lane today: no source ⇒ no identity ⇒ named refusal.

    A shape that cannot *verify* the claim is the one place the ruling allows to
    be useless rather than unsafe: registration still succeeds (the node has
    other work to do), the identity stays unset, and every file operation that
    needs it refuses by name.
    """
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent, worker_identity=NoWorkerIdentitySource())
    resp = await _enroll(app)
    assert resp.status_code == 200
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (None, None)

    response = await _file_op(app, {"op": "remove-workspace", "sandbox_id": SANDBOX})
    assert response.status_code == 503
    assert response.json() == {
        "code": 503,
        "message": (
            f"node {NODE_A} has reported no worker identity (workerUID/"
            "workerGID): refusing to instruct the agent"
        ),
    }
    assert agent.calls == []


# ------------------------- the two "cannot verify" messages (fifth review, 1)


async def _heartbeat(app) -> int:
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/heartbeat",
            headers={"X-Internal-Key": KEY_A},
            json={"workerUID": WORKER_UID, "workerGID": WORKER_GID},
        )
    return resp.status_code


def _identity_warnings(caplog) -> list[str]:
    return [
        record.message
        for record in caplog.records
        if record.name == "control_plane.api.internal"
        and "worker identity" in record.message
    ]


@pytest.mark.asyncio
async def test_an_unverifiable_identity_is_reported_once_per_node(
    workspace, monkeypatch, caplog
) -> None:
    """Fifth review 1: one line per node, not one per 5-second heartbeat.

    The shipped k8s shape configures the source but pins nothing on the worker
    pod, so it cannot verify -- and the heartbeat runs every few seconds for
    every worker. The once-per-node discipline is the one
    ``_unresolvable_nodes_reported`` already uses.
    """
    from control_plane.api import internal as internal_module

    monkeypatch.setattr(internal_module, "_unverified_identity_reported", set())
    shape = _C3Shape(workspace)
    app = _app(
        shape,
        client=_StubAgentClient(),
        # A k8s-shaped source whose pod pins nothing: "configured, but no pin".
        worker_identity=_k8s_source(
            lambda request: httpx.Response(200, json=_pod_spec(None, None))
        ),
    )
    caplog.set_level("WARNING", logger="control_plane.api.internal")
    caplog.clear()
    # One registration and two heartbeats: the line is owed once, not three
    # times (and in production the heartbeat is every ~5 s, forever).
    await _enroll(app)
    assert await _heartbeat(app) == 200
    assert await _heartbeat(app) == 200

    assert _identity_warnings(caplog) == [
        f"internal API: node {NODE_A} reported worker identity "
        f"({WORKER_UID}, {WORKER_GID}), but its pod spec pins no "
        "runAsUser/runAsGroup (the worker relies on the image's USER, which the "
        "API cannot read): recording no identity -- pin both in the worker "
        "manifest, or the file operations that need one will refuse by name",
    ]
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (None, None)


@pytest.mark.asyncio
async def test_a_shape_with_no_source_at_all_says_that_instead(
    workspace, monkeypatch, caplog
) -> None:
    """"No source in this shape" and "the pod pins nothing" are different problems."""
    from control_plane.api import internal as internal_module

    monkeypatch.setattr(internal_module, "_unverified_identity_reported", set())
    shape = _C3Shape(workspace)
    app = _app(
        shape, client=_StubAgentClient(), worker_identity=NoWorkerIdentitySource()
    )
    caplog.set_level("WARNING", logger="control_plane.api.internal")
    caplog.clear()
    await _enroll(app)
    assert await _heartbeat(app) == 200
    assert await _heartbeat(app) == 200

    assert _identity_warnings(caplog) == [
        f"internal API: node {NODE_A} reported worker identity "
        f"({WORKER_UID}, {WORKER_GID}), but this shape has no trusted source "
        "for it (only the k8s lane can verify a report): recording no identity "
        "-- the file operations that need one will refuse by name",
    ]


@pytest.mark.asyncio
async def test_a_verified_identity_logs_nothing(
    workspace, monkeypatch, caplog
) -> None:
    """The happy path stays quiet -- and a later regression is reported again."""
    from control_plane.api import internal as internal_module

    monkeypatch.setattr(internal_module, "_unverified_identity_reported", set())
    shape = _C3Shape(workspace)
    app = _app(shape, client=_StubAgentClient())
    caplog.set_level("WARNING", logger="control_plane.api.internal")
    caplog.clear()
    await _enroll(app)
    assert await _heartbeat(app) == 200
    assert _identity_warnings(caplog) == []
    assert (NODE_A, "no-pin") not in internal_module._unverified_identity_reported


# ------------------------------- the k8s trusted source itself (②, option 1)


def _pod_spec(security: dict | None, container_security: dict | None) -> dict:
    spec: dict = {"containers": [{"name": "worker"}]}
    if security is not None:
        spec["securityContext"] = security
    if container_security is not None:
        spec["containers"][0]["securityContext"] = container_security
    return {"kind": "Pod", "spec": spec}


def _k8s_source(handler):
    from control_plane.worker_identity_source import K8sWorkerIdentitySource

    return K8sWorkerIdentitySource(
        namespace="sandlock",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://kubernetes.default.svc",
        ),
    )


def test_the_k8s_source_reads_the_pods_own_security_context() -> None:
    """The trusted answer is the *deployment's* pin, not the worker's report."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=_pod_spec({"runAsUser": 65534, "runAsGroup": 65534}, None))

    assert _k8s_source(handler).identity_for(NODE_A) == (65534, 65534)
    assert seen == [f"/api/v1/namespaces/sandlock/pods/{NODE_A}"]


def test_the_k8s_source_reads_a_container_level_pin_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_pod_spec(None, {"runAsUser": 65534, "runAsGroup": 65533}),
        )

    assert _k8s_source(handler).identity_for(NODE_A) == (65534, 65533)


def test_a_pod_that_pins_no_identity_has_no_trusted_answer() -> None:
    """No pin ⇒ no identity (fail closed), never "trust the report"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pod_spec(None, None))

    assert _k8s_source(handler).identity_for(NODE_A) is None
    # Half a pin is not a pin either.
    def half(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pod_spec({"runAsUser": 65534}, None))

    assert _k8s_source(half).identity_for(NODE_A) is None


def test_an_api_that_cannot_answer_is_not_a_trusted_answer() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"kind": "Status"})

    assert _k8s_source(missing).identity_for(NODE_A) is None

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to the API server")

    assert _k8s_source(broken).identity_for(NODE_A) is None

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    assert _k8s_source(garbage).identity_for(NODE_A) is None


def test_a_hostile_node_id_never_reaches_the_api() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        seen.append(request.url.path)
        return httpx.Response(200, json=_pod_spec({"runAsUser": 1, "runAsGroup": 1}, None))

    assert _k8s_source(handler).identity_for("../../etc/passwd") is None
    assert seen == []


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
                # ``<uid>:<uid>``, matching the pre-C3 hand-over
                # (``checkpoint_store._hand_to_sandbox``); only the workspace
                # tree keeps the worker's gid as the group.
                "gid": UID_X,
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
                "gid": UID_X,
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
    # No trusted source and no claim: the record keeps no identity (see
    # ``test_a_shape_without_a_trusted_source_records_no_identity``).
    app = _app(shape, client=agent, worker_identity=NoWorkerIdentitySource())
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
    app = _app(shape, client=agent, worker_identity=NoWorkerIdentitySource())
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


# ------------------------------- the compose lane's kernel-anchored source


@pytest.mark.asyncio
async def test_the_compose_shape_stores_the_claim_and_carries_the_anchor(
    workspace,
) -> None:
    """D21 option 2: compose keeps the claim **and** hands the agent the anchor.

    The compose shape has no pod spec to read, so the control plane cannot
    answer here -- but it is not a shape that "cannot answer" either: the agent
    reads the worker's own process identity out of the kernel, and the anchor
    that lets it do so is the container id this control plane already records
    (ruling D25 -- the cgroup path carries it and is world-readable, which is
    what lets face B do the read without a capability or a uid change).
    So the node keeps the reported uid/gid as the value the agent will confirm,
    and every instruction that acts as the worker carries the anchor.
    """
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent, worker_identity=KernelWorkerIdentitySource())
    await _enroll(app)
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (WORKER_UID, WORKER_GID)

    resp = await _file_op(app, {"op": "chown-workspace", "sandbox_id": SANDBOX})

    assert resp.status_code == 200
    assert agent.calls == [
        {
            "node_id": NODE_A,
            "sandbox_id": SANDBOX,
            "path": str(shape.workspace_dir()),
            "worker_uid": WORKER_UID,
            "worker_gid": WORKER_GID,
            "worker_container_id": CONTAINER_ID,
            "verb": "chown",
            "uid": UID_X,
            "gid": WORKER_GID,
            "recursive": True,
            "worker_owned": False,
        }
    ]


@pytest.mark.asyncio
async def test_the_compose_shape_refuses_an_instruction_it_cannot_anchor(
    workspace,
) -> None:
    """No anchor ⇒ no kernel answer ⇒ the instruction is refused by name.

    A worker that registers without a container id (an older worker, or one
    whose hostname is not one -- a stack that overrode ``hostname:``) leaves
    nothing for the agent to confirm the claim against. The control plane names
    that instead of sending an instruction the agent would have to refuse
    anyway.
    """
    shape = _C3Shape(workspace)
    agent = _StubAgentClient()
    app = _app(shape, client=agent, worker_identity=KernelWorkerIdentitySource())
    await _enroll(app, container_id=False)
    node = app.state.nodes.get(NODE_A)
    assert (node.worker_uid, node.worker_gid) == (WORKER_UID, WORKER_GID)

    resp = await _file_op(app, {"op": "chown-workspace", "sandbox_id": SANDBOX})

    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_A} has reported no container id for the agent to "
            "confirm its worker identity against (a C3 worker must keep the "
            "runtime's hostname): refusing to instruct the agent"
        ),
    }
    assert agent.calls == []


def test_the_k8s_shape_is_not_kernel_verified() -> None:
    """The two shapes stay distinguishable: only compose defers to the kernel."""
    assert KernelWorkerIdentitySource().configured is True
    assert KernelWorkerIdentitySource().kernel_verified is True
    assert NoWorkerIdentitySource().kernel_verified is False
    assert StaticWorkerIdentitySource({}).kernel_verified is False


def test_a_hostname_deployment_builds_the_kernel_verified_source() -> None:
    """The switch is the node-address mode, exactly as the other lanes' is."""
    from control_plane.worker_identity_source import build_worker_identity_source

    source = build_worker_identity_source(_settings(node_address_mode="hostname"))

    assert isinstance(source, KernelWorkerIdentitySource)
    assert source.configured is True
    # The compose shape answers *through the agent*, never from here.
    assert source.identity_for(NODE_A) is None
