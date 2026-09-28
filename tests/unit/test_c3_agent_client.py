"""C3 Task 3 (controller ruling D9.4/D9.5): finding the agent, and dialling it.

The control plane has to turn "sandbox S is on node N" into an HTTP call to the
agent that runs *on N's host* -- and it has to do that from a trusted source.
The k8s lane reads the worker pod's ``spec.nodeName`` from the API and then the
agent pod on that node by its label (the pod UID it reads on the way is the
extra proof the agent demands, D9.3); the compose lane dials the configured
agent service name. Neither is ever learned from a request body, and a lookup
that cannot answer -- missing, ambiguous, or a k8s lane with no pod UID -- fails
closed with a name.

The client carries the two knobs D9.5 asks for: a typed timeout (a stuck agent
must never look like "create hangs") and a concurrency limit (the knob Task 3's
dispatch exposes now, for slice B to size from the real concurrency arm).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from control_plane.c3_agent_client import (
    AgentClientError,
    AgentTarget,
    C3AgentClient,
    ComposeAgentAddressResolver,
    K8sAgentAddressResolver,
    build_agent_address_resolver,
)

WORKER_POD_UID = "6d3cdd7b-3a5e-4a1f-9a6b-0c1d2e3f4a5b"
NODE = "e2b-worker-0"
AGENT_IP = "10.244.1.7"
NAMESPACE = "sandlock"
AGENT_LABEL = "app=c3-agent"
PID_NAMESPACE = "pid:[4026532458]"
TOKEN = "c3-agent-sekret"


def _pod(
    name: str,
    *,
    node_name: str = "k0s-worker-0",
    uid: str = WORKER_POD_UID,
    pod_ip: str | None = "10.244.1.9",
    labels: dict[str, str] | None = None,
) -> dict:
    return {
        "metadata": {"name": name, "uid": uid, "labels": labels or {"app": "worker"}},
        "spec": {"nodeName": node_name},
        "status": {"podIP": pod_ip},
    }


def _pod_list(*pods: dict) -> dict:
    return {"kind": "PodList", "items": list(pods)}


def _k8s_resolver(handler, **overrides) -> K8sAgentAddressResolver:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://kubernetes.default.svc",
    )
    options = dict(namespace=NAMESPACE, label_selector=AGENT_LABEL, client=client)
    options.update(overrides)
    return K8sAgentAddressResolver(**options)


# ------------------------------------------------------- finding the agent (k8s)


def test_the_agent_is_found_through_the_workers_own_node() -> None:
    """``nodeName`` from the worker pod, then the agent pod on that node.

    The pod UID rides back with the address: it is the proof the agent's lookup
    demands (D9.3), and the only place it can come from in this lane is the API
    -- never the worker.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith(f"/pods/{NODE}"):
            return httpx.Response(200, json=_pod(NODE))
        assert request.url.path.endswith("/pods")
        return httpx.Response(
            200,
            json=_pod_list(
                _pod("c3-agent-abc", uid="11111111-2222-3333-4444-555555555555",
                     pod_ip=AGENT_IP),
            ),
        )

    target = _k8s_resolver(handler).resolve(NODE)
    assert target == AgentTarget(url=f"http://{AGENT_IP}:49985", pod_uid=WORKER_POD_UID)
    assert seen[0].endswith(f"/api/v1/namespaces/{NAMESPACE}/pods/{NODE}")
    # The second lookup is scoped to the node the worker really runs on, and to
    # the agent label -- not to whatever the request may have said.
    assert seen[1].endswith(
        f"/api/v1/namespaces/{NAMESPACE}/pods"
        f"?labelSelector=app%3Dc3-agent&fieldSelector=spec.nodeName%3Dk0s-worker-0"
    )


def test_a_worker_pod_without_a_uid_is_unresolvable_in_the_k8s_lane() -> None:
    """D9.3: this lane *has* the proof, so it must produce one or refuse.

    The pod UID is what the agent checks the candidate's cgroup against; an
    address without it would be an instruction the agent must reject anyway, so
    the lookup refuses to produce one at all.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/pods/{NODE}"):
            return httpx.Response(200, json=_pod(NODE, uid=""))
        return httpx.Response(200, json=_pod_list(_pod("c3-agent", pod_ip=AGENT_IP)))

    assert _k8s_resolver(handler).resolve(NODE) is None
    assert _k8s_resolver(handler, label_selector="app=c3-agent").resolve(NODE) is None


def test_an_agent_pod_without_an_ip_is_unresolvable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/pods/{NODE}"):
            return httpx.Response(200, json=_pod(NODE))
        return httpx.Response(200, json=_pod_list(_pod("c3-agent-abc", pod_ip=None)))

    assert _k8s_resolver(handler).resolve(NODE) is None


def test_two_agent_pods_on_one_node_are_unresolvable() -> None:
    """Ambiguous is not "pick one": it is a named refusal at the handler."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/pods/{NODE}"):
            return httpx.Response(200, json=_pod(NODE))
        return httpx.Response(
            200,
            json=_pod_list(
                _pod("c3-agent-a", pod_ip=AGENT_IP),
                _pod("c3-agent-b", pod_ip="10.244.1.8"),
            ),
        )

    assert _k8s_resolver(handler).resolve(NODE) is None


def test_a_missing_worker_pod_is_unresolvable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"kind": "Status", "code": 404})

    assert _k8s_resolver(handler).resolve(NODE) is None


def test_an_api_error_body_is_unresolvable_not_a_crash() -> None:
    """A "Status" object instead of a Pod is "no address", never an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/pods/{NODE}"):
            return httpx.Response(200, json={"kind": "Status", "code": 403})
        return httpx.Response(200, json=_pod_list())

    assert _k8s_resolver(handler).resolve(NODE) is None


def test_an_unreachable_api_is_unresolvable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to kubernetes.default.svc")

    assert _k8s_resolver(handler).resolve(NODE) is None


def test_a_hostile_node_id_never_reaches_the_api() -> None:
    """The id is interpolated into an API path: shape first, then the call."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_pod(NODE))

    resolver = _k8s_resolver(handler)
    assert resolver.resolve("../../etc/passwd") is None
    assert resolver.resolve("") is None
    assert calls == []


# --------------------------------------------------- finding the agent (compose)


def test_the_compose_agent_is_the_configured_service_name() -> None:
    """Compose addresses the agent by service name; there is no pod UID there."""
    resolver = ComposeAgentAddressResolver("http://c3-agent:49985/")
    assert resolver.resolve(NODE) == AgentTarget(
        url="http://c3-agent:49985", pod_uid=None
    )


def test_an_unconfigured_compose_agent_refuses_to_resolve() -> None:
    """A shape that has not named an agent has none -- fail closed, never guess."""
    assert ComposeAgentAddressResolver("").resolve(NODE) is None
    assert ComposeAgentAddressResolver(None).resolve(NODE) is None


def test_the_resolver_follows_the_deployments_address_mode(monkeypatch) -> None:
    class _Settings:
        node_address_mode = "hostname"
        c3_agent_url = "http://c3-agent:49985"
        c3_agent_namespace = NAMESPACE
        c3_agent_label = AGENT_LABEL
        c3_agent_port = 49985

    resolver = build_agent_address_resolver(_Settings())
    assert isinstance(resolver, ComposeAgentAddressResolver)

    class _K8sSettings(_Settings):
        node_address_mode = "k8s"

    assert isinstance(build_agent_address_resolver(_K8sSettings()), K8sAgentAddressResolver)


# ------------------------------------------------------------------ the client


def _client(handler, **overrides) -> C3AgentClient:
    options = dict(
        resolver=ComposeAgentAddressResolver("http://c3-agent:49985"),
        token=TOKEN,
        timeout_s=2.0,
    )
    options.update(overrides)
    return C3AgentClient(transport=httpx.MockTransport(handler), **options)


def _grant(client: C3AgentClient, *, node_id: str = NODE):
    return asyncio.run(
        client.grant_slot(
            node_id=node_id,
            sandbox_id="sbx_forward",
            container_pid=4242,
            uid=10007,
            worker_pid_namespace=PID_NAMESPACE,
        )
    )


def _k8s_client(handler, *, transport=None) -> C3AgentClient:
    """A client whose lane is k8s, so the target carries the pod UID."""

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/pods/{NODE}"):
            return httpx.Response(200, json=_pod(NODE))
        return httpx.Response(
            200, json=_pod_list(_pod("c3-agent", pod_ip=AGENT_IP))
        )

    return C3AgentClient(
        resolver=_k8s_resolver(api),
        token=TOKEN,
        timeout_s=2.0,
        transport=transport or httpx.MockTransport(handler),
    )


def test_the_instruction_carries_the_records_and_nothing_from_the_caller() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"op": "grant-slot", "hostPid": 990425, "asUid": "C3-ASUID-OK"},
        )

    result = _grant(_k8s_client(handler))
    assert result["hostPid"] == 990425
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == (
        f"http://{AGENT_IP}:49985/internal/nodes/{NODE}/agent/grant-slot"
    )
    assert request.headers["X-Internal-Key"] == TOKEN
    assert json.loads(request.content) == {
        "sandbox_id": "sbx_forward",
        "pid": 4242,
        "uid": 10007,
        "worker": {
            "node_id": NODE,
            "pid_namespace": PID_NAMESPACE,
            "pod_uid": WORKER_POD_UID,
        },
    }


def test_the_compose_instruction_carries_no_pod_uid() -> None:
    """The other half of D9.3: a lane without the proof sends none."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"op": "grant-slot", "hostPid": 7})

    _grant(_client(handler))
    assert len(calls) == 1


def test_the_real_agent_service_accepts_the_clients_instruction() -> None:
    """The wire format, pinned across the hop rather than from one side.

    The control plane's client and the agent's service are two deployables that
    are never imported together in production, so a body key renamed on one side
    would only show up at runtime. Here the client dials the *real* service
    (through an ASGI transport) and the agent's own models judge the request:
    a mismatch fails this test rather than a slot create on a live node.
    """
    from deploy.c3_agent.app import create_app as create_agent_app
    from deploy.c3_agent.config import Settings as AgentSettings

    class _Runner:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def grant(self, uid: int, pid: int) -> str:
            self.calls.append((uid, pid))
            return f"C3-ASUID-OK pid={pid} uid={uid}"

    class _Lookup:
        def host_pid(self, container_pid, identity, *, sandbox_id: str):
            from deploy.c3_agent.lookup import SlotProcess

            assert identity.pid_namespace == PID_NAMESPACE
            assert identity.pod_uid == WORKER_POD_UID
            return SlotProcess(
                host_pid=990425,
                start_time="4242",
                pid_namespace=identity.pid_namespace,
            )

        def still_alive(self, slot) -> bool:
            return True

    runner = _Runner()
    agent_app = create_agent_app(
        settings=AgentSettings(token=TOKEN, node_id=NODE),
        runner=runner,
        lookup=_Lookup(),
    )
    client = _k8s_client(
        lambda request: httpx.Response(200, json={}),
        transport=httpx.ASGITransport(app=agent_app),
    )

    answer = _grant(client)
    assert answer["op"] == "grant-slot"
    assert answer["sandboxID"] == "sbx_forward"
    assert answer["uid"] == 10007
    assert answer["pid"] == 4242
    assert answer["hostPid"] == 990425
    assert runner.calls == [(10007, 990425)]


def test_an_unresolvable_agent_is_a_named_refusal() -> None:
    client = C3AgentClient(
        resolver=ComposeAgentAddressResolver(""),
        token=TOKEN,
        timeout_s=2.0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    with pytest.raises(AgentClientError) as excinfo:
        _grant(client)
    assert str(excinfo.value) == (
        f"cannot determine the agent address for node {NODE}: refusing to "
        "instruct an agent the control plane cannot locate"
    )
    assert excinfo.value.status_code == 503


def test_an_unconfigured_token_refuses_before_dialling() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    with pytest.raises(AgentClientError) as excinfo:
        _grant(_client(handler, token=""))
    assert str(excinfo.value) == (
        "E2B_C3_AGENT_TOKEN is not configured: refusing to instruct an agent"
    )
    assert excinfo.value.status_code == 503
    assert calls == []


def test_a_timeout_names_the_node_and_the_deadline() -> None:
    """A stuck agent must never look like "the create hangs"."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(AgentClientError) as excinfo:
        _grant(_client(handler))
    assert str(excinfo.value) == (
        f"the agent for node {NODE} did not answer within 2.0s: refusing "
        "(the slot identity grant is fail-closed)"
    )
    assert excinfo.value.status_code == 504


def test_an_unreachable_agent_is_a_named_refusal() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AgentClientError) as excinfo:
        _grant(_client(handler))
    assert str(excinfo.value) == (
        f"the agent for node {NODE} is unreachable: connection refused"
    )
    assert excinfo.value.status_code == 502


def test_the_agents_own_refusal_is_forwarded_verbatim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "沙箱 sbx_forward 的槽位 pid 已不在"})

    with pytest.raises(AgentClientError) as excinfo:
        _grant(_client(handler))
    assert str(excinfo.value) == (
        f"the agent for node {NODE} refused the grant: 沙箱 sbx_forward 的槽位 pid 已不在"
    )
    assert excinfo.value.status_code == 502


def test_an_answer_that_is_not_an_object_is_refused_not_wrapped() -> None:
    """A 2xx body the caller cannot read fields out of is not a grant."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["grant-slot"])

    with pytest.raises(AgentClientError) as excinfo:
        _grant(_client(handler))
    assert str(excinfo.value) == (
        f"the agent for node {NODE} answered with a list, not an instruction "
        "answer"
    )
    assert excinfo.value.status_code == 502


def test_an_answer_that_is_not_json_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="C3-ASUID-OK pid=1 uid=1")

    with pytest.raises(AgentClientError) as excinfo:
        _grant(_client(handler))
    assert str(excinfo.value) == (
        f"the agent for node {NODE} answered with a non-JSON body"
    )
    assert excinfo.value.status_code == 502


def test_the_concurrency_limit_is_the_knob_slice_b_sizes() -> None:
    """``max_concurrency=1`` serializes; the default does not."""

    def _handler_for(state: dict) -> object:
        async def handler(request: httpx.Request) -> httpx.Response:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            await asyncio.sleep(0.05)
            state["live"] -= 1
            return httpx.Response(200, json={"op": "grant-slot", "hostPid": 1})

        return handler

    async def _grants(client: C3AgentClient, count: int) -> None:
        await asyncio.gather(
            *(
                client.grant_slot(
                    node_id=NODE,
                    sandbox_id=f"sbx_{index}",
                    container_pid=4242 + index,
                    uid=10007,
                    worker_pid_namespace=PID_NAMESPACE,
                )
                for index in range(count)
            )
        )

    serial = {"live": 0, "peak": 0}
    asyncio.run(_grants(_client(_handler_for(serial), max_concurrency=1), 3))
    assert serial["peak"] == 1

    concurrent = {"live": 0, "peak": 0}
    asyncio.run(_grants(_client(_handler_for(concurrent)), 3))
    assert concurrent["peak"] == 3
