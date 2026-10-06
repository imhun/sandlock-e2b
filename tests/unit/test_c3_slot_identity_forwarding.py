"""C3 Task 3 (rulings D9.1/D9.2): the control plane forwards a slot report.

The worker reports ``{sandbox_id, pid}`` -- **no uid** -- to
``POST /internal/nodes/{node_id}/slot-identity``. The control plane then does
three things in order, and each has its own refusal here:

① the identity layer (``_require_node_identity``): a credential bound to a node,
   a claim equal to it, and the source-IP second factor;
② the *object*: the reported sandbox must be in the control plane's own
   records **and** on that node -- and the uid must come from those records, not
   from the request (hard rule 1/3). A body that carries a uid is refused by
   name: the worker must not be able to name an identity at all;
③ the instruction: the agent is dialled with the record's uid, the reported
   container pid and the worker's identity, and every failure on that hop is
   named and typed (a stuck agent is a 504, never a hung create).

The worker's own pid namespace identity is what makes the agent's reverse
lookup unambiguous (D9.3); it rides register/heartbeat and lives on the node
record, refreshed on every heartbeat so a restarted worker container is not
pinned to the namespace inode it had before.
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.api.errors import OfficialError
from control_plane.app import create_app as create_control_app
from control_plane.c3_agent_client import AgentClientError, AgentTarget
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from envd_service import worker_identity
from envd_service.priv_helpers import PrivHelperError

KEY_A = "key-node-a"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
NODE_A = "node_a"
PID_NAMESPACE = "pid:[4026532458]"
UID_X = 10007
FLEET_KEY = "fleet-key"


class _StubAgentClient:
    """Records the instruction; answers like the agent would (or refuses)."""

    def __init__(self, *, answer: dict | None = None, refuse: str | None = None,
                 status_code: int = 502) -> None:
        self.calls: list[dict] = []
        self._answer = answer or {
            "op": "grant-slot",
            "hostPid": 990425,
            "asUid": "C3-ASUID-OK pid=990425 uid=10007",
        }
        self._refuse = refuse
        self._status_code = status_code

    async def grant_slot(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._refuse is not None:
            raise AgentClientError(self._refuse, status_code=self._status_code)
        return self._answer


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


def _app(workspace, *, client, registry=None, nodes=None, settings=None):
    settings = settings or _settings()
    return create_control_app(
        settings=settings,
        registry=registry or SandboxRegistry(settings),
        nodes_registry=nodes or NodeRegistry(heartbeat_timeout=600.0),
        workspace_base=workspace,
        node_address_resolver=StaticAddressResolver({NODE_A: ENDPOINT_A}),
        c3_agent_client=client,
    )


def _client(app, *, source_ip: str = "10.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _enroll(app, *, pid_namespace: str | None = PID_NAMESPACE, registry=None):
    """Register node A (with its worker identity) and put one sandbox on it."""
    registry = registry or app.state.registry
    record = registry.create(
        template_id="base",
        sandbox_id="sbx_forward",
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
    async with _client(app) as client:
        body = {
            "nodeID": NODE_A,
            "address": ENDPOINT_A.address,
            "totalMemoryMB": 1024,
            "totalCPUPercent": 100,
            "totalDiskMB": 1024,
            "totalProcesses": 64,
        }
        if pid_namespace is not None:
            body["pidNamespace"] = pid_namespace
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": KEY_A},
            json=body,
        )
    return resp


# ------------------------------------------------- the worker's own identity


@pytest.mark.asyncio
async def test_registration_records_the_workers_pid_namespace(workspace) -> None:
    app = _app(workspace, client=_StubAgentClient())
    resp = await _enroll(app)
    assert resp.status_code == 200
    assert app.state.nodes.get(NODE_A).pid_namespace == PID_NAMESPACE


@pytest.mark.asyncio
async def test_a_hostile_pid_namespace_is_refused_at_registration(workspace) -> None:
    """The value is compared against ``/proc`` links and a cgroup: shape first.

    A value that is not a pid namespace identity is refused by name -- never
    stored and later compared loosely.
    """
    app = _app(workspace, client=_StubAgentClient())
    resp = await _enroll(app, pid_namespace="pid:[../../etc/passwd")
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "pidNamespace must be a pid namespace identity such as "
            "'pid:[4026532458]'"
        )
    }
    assert app.state.nodes.get(NODE_A) is None


@pytest.mark.asyncio
async def test_a_heartbeat_refreshes_the_workers_pid_namespace(workspace) -> None:
    """A restarted worker container has a new namespace inode.

    The node id survives that restart, so without the refresh every grant for
    that node would be refused until the control plane forgot it entirely --
    the "the node is pinned" failure mode §11.1 item 9 warns about.
    """
    app = _app(workspace, client=_StubAgentClient())
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/heartbeat",
            headers={"X-Internal-Key": KEY_A},
            json={"pidNamespace": "pid:[4026532709]"},
        )
    assert resp.status_code == 204
    assert app.state.nodes.get(NODE_A).pid_namespace == "pid:[4026532709]"

    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/heartbeat",
            headers={"X-Internal-Key": KEY_A},
            json={"pidNamespace": "nonsense"},
        )
    assert resp.status_code == 400
    # The refused value did not overwrite the good one.
    assert app.state.nodes.get(NODE_A).pid_namespace == "pid:[4026532709]"


# ------------------------------------------------------------- the forwarding


@pytest.mark.asyncio
async def test_the_report_is_forwarded_with_the_records_uid(workspace) -> None:
    agent = _StubAgentClient()
    app = _app(workspace, client=agent)
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242},
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "nodeID": NODE_A,
        "sandboxID": "sbx_forward",
        "uid": UID_X,
        "pid": 4242,
        "agent": {
            "op": "grant-slot",
            "hostPid": 990425,
            "asUid": f"C3-ASUID-OK pid=990425 uid={UID_X}",
        },
    }
    # The uid and the worker identity are the control plane's records; the pid
    # is the only thing that came from the report.
    assert agent.calls == [
        {
            "node_id": NODE_A,
            "sandbox_id": "sbx_forward",
            "container_pid": 4242,
            "uid": UID_X,
            "worker_pid_namespace": PID_NAMESPACE,
        }
    ]


@pytest.mark.asyncio
async def test_a_report_that_names_a_uid_is_refused(workspace) -> None:
    """Hard rule 1/3, on the wire: the worker may not name an identity.

    Silently ignoring the field would leave the *next* reader believing the
    worker's value was considered; the shape is refused by name instead.
    """
    agent = _StubAgentClient()
    app = _app(workspace, client=agent)
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242, "uid": 0},
        )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "a slot-identity report carries {sandbox_id, pid} and no uid: the "
            "identity comes from the control plane's records"
        )
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_the_identity_layer_guards_the_forwarding_endpoint(workspace) -> None:
    """① the credential, and ② the source IP, exactly as the other node-scoped
    handlers."""
    agent = _StubAgentClient()
    app = _app(workspace, client=agent)
    await _enroll(app)
    async with _client(app) as client:
        body = {"sandbox_id": "sbx_forward", "pid": 4242}
        unauthenticated = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity", json=body
        )
        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {"code": 401, "message": "Unauthorized"}
    # A key bound to node A, speaking for node A, but from another address.
    async with _client(app, source_ip="10.0.0.2") as client:
        stolen = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json=body,
        )
    assert stolen.status_code == 403
    assert stolen.json() == {
        "code": 403,
        "message": (
            f"request for node {NODE_A} came from 10.0.0.2, expected 10.0.0.1"
        )
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_the_forwarded_object_must_be_the_controls_own_record(workspace) -> None:
    """③ the object: unknown sandboxes, and sandboxes on another node."""
    agent = _StubAgentClient()
    app = _app(workspace, client=agent)
    await _enroll(app)
    async with _client(app) as client:
        unknown = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_unknown", "pid": 4242},
        )
    assert unknown.status_code == 404
    assert unknown.json() == {"code": 404, "message": "Sandbox sbx_unknown not found"}

    elsewhere = app.state.registry.get("sbx_forward")
    elsewhere.node_id = "node_b"
    app.state.registry.save(elsewhere)
    async with _client(app) as client:
        foreign = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242},
        )
    assert foreign.status_code == 403
    assert foreign.json() == {
        "code": 403,
        "message": "Sandbox sbx_forward belongs to node node_b, not node_a",
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_report_for_a_sandbox_without_a_host_uid_is_refused(workspace) -> None:
    """No uid in the record means no identity to hand out: fail closed, named."""
    agent = _StubAgentClient()
    app = _app(workspace, client=agent)
    await _enroll(app)
    record = app.state.registry.get("sbx_forward")
    record.host_uid = None
    app.state.registry.save(record)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242},
        )
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            "sandbox sbx_forward has no allocated host uid: refusing to "
            "instruct the agent"
        )
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_node_without_a_pid_namespace_is_refused(workspace) -> None:
    """A lane that cannot supply the identity fails closed (D9.3)."""
    agent = _StubAgentClient()
    app = _app(workspace, client=agent)
    await _enroll(app, pid_namespace=None)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242},
        )
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_A} has reported no pid namespace identity: refusing "
            "to instruct the agent without it"
        )
    }
    assert agent.calls == []


@pytest.mark.asyncio
async def test_a_malformed_report_is_refused_before_the_agent(workspace) -> None:
    agent = _StubAgentClient()
    app = _app(workspace, client=agent)
    await _enroll(app)
    async with _client(app) as client:
        for body in (
            {"sandbox_id": "sbx_forward"},
            {"sandbox_id": "sbx_forward", "pid": 0},
            {"sandbox_id": "sbx_forward", "pid": "4242"},
            {"sandbox_id": "../../etc/passwd", "pid": 4242},
            {"pid": 4242},
        ):
            resp = await client.post(
                f"/internal/nodes/{NODE_A}/slot-identity",
                headers={"X-Internal-Key": KEY_A},
                json=body,
            )
            assert resp.status_code == 400
    assert agent.calls == []


@pytest.mark.asyncio
async def test_every_hop_failure_keeps_its_own_name(workspace) -> None:
    """A stuck agent is a 504; an unreachable one is a 502; both are named."""
    agent = _StubAgentClient(
        refuse=(
            f"the agent for node {NODE_A} did not answer within 5.0s: refusing "
            "(the slot identity grant is fail-closed)"
        ),
        status_code=504,
    )
    app = _app(workspace, client=agent)
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242},
        )
    assert resp.status_code == 504
    assert resp.json() == {
        "code": 504,
        "message": (
            f"the agent for node {NODE_A} did not answer within 5.0s: refusing "
            "(the slot identity grant is fail-closed)"
        )
    }

    refused = _StubAgentClient(refuse="沙箱 sbx_forward 的槽位 pid 已不在")
    app = _app(workspace, client=refused)
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242},
        )
    assert resp.status_code == 502
    assert resp.json() == {
        "code": 502,
        "message": "沙箱 sbx_forward 的槽位 pid 已不在",
    }


# --------------------- N83 phase 1: the worker asks for the cgroup delegation
#
# The worker's own side of the handshake (R-C): one POST to the node-scoped
# endpoint, once, at startup -- no retry loop inside (the caller owns retries),
# the same credentials the identity reporter presents, and every refusal named
# (a control plane that refuses must not read as "the worker came up without a
# delegated subtree"). The body is empty on purpose: the control plane derives
# the node, the object and the anchor, so the worker names nothing (hard rules
# 1/3 -- and this is why there is no uid, path or anchor in the signature).

CONTROL_PLANE_URL = "http://control-plane:3000"
DELEGATED = {
    "op": "delegate-cgroup",
    # Task 3's answer shape, verbatim: the agent's own view path, the entries it
    # chowned (never ``cpu.max``), and who still owns the limit file.
    "containerCgroup": "/host-cgroup/docker/3f2a1b0c9d8e",
    "delegated": [".", "cgroup.procs", "cgroup.subtree_control"],
    "cpuMaxOwner": "0:0",
}


def _delegate_request(handler, **overrides) -> dict:
    options = dict(
        control_plane_url=CONTROL_PLANE_URL,
        node_id=NODE_A,
        internal_key=KEY_A,
        timeout_s=5.0,
        transport=httpx.MockTransport(handler),
    )
    options.update(overrides)
    return worker_identity.request_cgroup_delegate(**options)


def test_the_worker_asks_once_and_names_nothing() -> None:
    """One POST, empty body, the worker's own key -- and the answer verbatim."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DELEGATED)

    answer = _delegate_request(handler)
    assert answer == DELEGATED
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == (
        f"{CONTROL_PLANE_URL}/internal/nodes/{NODE_A}/cgroup-delegate"
    )
    assert request.headers["X-Internal-Key"] == KEY_A
    # The body is empty: the control plane derives everything (R-A), and a
    # worker that could name the anchor or a path would be naming privilege.
    assert request.content == b""


def test_a_refused_delegation_is_fail_closed_and_named_once() -> None:
    """The control plane's refusal reaches the caller; nothing is retried here."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            503,
            json={
                "code": 503,
                "message": (
                    f"node {NODE_A} carries no anchor the agent can locate its "
                    "worker container by (compose: the worker's container id; "
                    "k8s: the worker pod uid): refusing to instruct the agent"
                ),
            },
        )

    with pytest.raises(PrivHelperError) as excinfo:
        _delegate_request(handler)
    assert str(excinfo.value) == (
        "the control plane refused the cgroup delegation (HTTP 503): "
        f"node {NODE_A} carries no anchor the agent can locate its worker "
        "container by (compose: the worker's container id; k8s: the worker pod "
        "uid): refusing to instruct the agent"
    )
    # ``no retry loop inside``: the caller owns retries, so exactly one call.
    assert len(calls) == 1


def test_an_unreachable_control_plane_is_a_named_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(PrivHelperError) as excinfo:
        _delegate_request(handler)
    assert str(excinfo.value) == (
        "the control plane is unreachable for the cgroup delegation: "
        "connection refused"
    )


def test_a_worker_that_cannot_name_its_control_plane_refuses_without_dialling(
) -> None:
    """An empty URL (the reporter's own seam, used verbatim) is a named refusal."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DELEGATED)

    with pytest.raises(PrivHelperError) as excinfo:
        _delegate_request(handler, control_plane_url="")
    assert str(excinfo.value) == (
        "this worker does not know which control plane to ask, or which node "
        "it is (E2B_CONTROL_PLANE_URL / E2B_NODE_ID): refusing to request the "
        "cgroup delegation"
    )
    assert seen == []


@pytest.mark.asyncio
async def test_no_agent_client_refuses_by_name(workspace) -> None:
    """A control plane with no agent wired refuses; it does not 500."""
    app = _app(workspace, client=None)
    await _enroll(app)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{NODE_A}/slot-identity",
            headers={"X-Internal-Key": KEY_A},
            json={"sandbox_id": "sbx_forward", "pid": 4242},
        )
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            "this control plane has no C3 agent client configured: refusing to "
            "report a slot identity"
        )
    }
