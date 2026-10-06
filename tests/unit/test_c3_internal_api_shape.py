"""C3 Task 2 nails: the two shapes that would silently void N49's source-IP layer.

``docs/c3-privilege-relocation.md`` §11.1 item 9 names the two ways the
second factor (the observed source IP) stops working **without anyone
noticing**:

* a worker pod granted ``CAP_NET_RAW`` can forge the source IP, so the IP the
  control plane reads is no longer the pod's network position. The forbidden
  set for the worker and the agent is ``SYS_ADMIN``/``SYS_PTRACE``/``NET_RAW``/
  ``privileged``/``hostNetwork``/``allowPrivilegeEscalation: true`` (the plan's
  hard rule);
* a proxy/ingress/service-mesh sidecar in front of ``/internal/**`` collapses
  every worker onto one source IP -- the check becomes a constant-true piece of
  dead code. The internal API must be reached *directly*: a plain ClusterIP
  Service selecting the control-plane pods (k8s) and the direct compose service
  name (``http://control-plane:3000``), with no ingress and no sidecar.

These are config-layer assertions on purpose: the failure mode is a one-line
manifest edit that no runtime test would catch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from control_plane.app import create_app as create_control_app
from control_plane.c3_agent_client import (
    AgentClientError,
    AgentTarget,
    C3AgentClient,
    ComposeAgentAddressResolver,
    StaticAgentAddressResolver,
)
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.worker_identity_source import (
    KernelWorkerIdentitySource,
    StaticWorkerIdentitySource,
)

REPO = Path(__file__).resolve().parents[2]
K8S = REPO / "deploy" / "k8s"

#: The forbidden capabilities/fields named by the plan for worker and agent.
FORBIDDEN_CAPABILITY_TOKENS = ("SYS_ADMIN", "SYS_PTRACE", "NET_RAW")
POD_PROXY_CONTAINER_NAMES = (
    "istio-proxy",
    "linkerd-proxy",
    "envoy",
    "nginx",
)
MESH_INJECT_ANNOTATIONS = ("sidecar.istio.io/inject", "linkerd.io/inject")


def _load_all(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _compose_env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        pairs = (entry.split("=", 1) for entry in env if "=" in entry)
        return {k: v for k, v in pairs}
    return {str(k): str(v) for k, v in env.items()}


# --------------------------------------------------------- no CAP_NET_RAW, ever


def _container_capability_texts(pod_spec: dict) -> list[str]:
    texts: list[str] = []
    for container in (pod_spec.get("initContainers") or []) + (
        pod_spec.get("containers") or []
    ):
        security = container.get("securityContext") or {}
        caps = security.get("capabilities") or {}
        texts.extend(caps.get("add") or [])
        if security.get("privileged"):
            texts.append("privileged")
    return texts


def test_worker_pod_manifest_carries_no_net_raw() -> None:
    """The worker manifest's capability additions are a closed, reviewed set.

    ``NET_RAW`` would let a compromised worker forge the source IP the control
    plane reads, defeating N49's second factor before the identity layer even
    runs. Task 4 slice B did what this pin said it must: the worker's two
    file-capability bounding caps are gone with the binaries they served, so
    the reviewed set is now the **empty set** -- and 判据 2/15's "no privileged
    binary, no BND" is what it means.
    """
    docs = _load_all(K8S / "worker.yaml")
    statefulset = next(d for d in docs if d.get("kind") == "StatefulSet")
    caps = _container_capability_texts(statefulset["spec"]["template"]["spec"])
    assert "NET_RAW" not in caps
    for forbidden in FORBIDDEN_CAPABILITY_TOKENS:
        assert forbidden not in caps
    assert sorted(caps) == []


def test_every_compose_worker_service_carries_no_forbidden_privilege() -> None:
    """The same rule for both compose stacks' workers, structurally.

    The stacks declare their workers with YAML anchors, so a capability added to
    the anchor reaches every replica; parsing the rendered service catches that,
    while comments (which *mention* the forbidden names) do not count.
    """
    manifests = [
        REPO / "deploy" / "stack" / "docker-compose.prod.yml",
        REPO / "deploy" / "compose" / "docker-compose.prod.yml",
        REPO / "deploy" / "compose" / "docker-compose.multinode.yml",
    ]
    seen = 0
    for path in manifests:
        services = _compose(path).get("services") or {}
        for name, service in services.items():
            if not name.startswith("worker"):
                continue
            seen += 1
            caps = service.get("cap_add") or []
            for forbidden in FORBIDDEN_CAPABILITY_TOKENS:
                assert forbidden not in caps, f"{path.name}:{name} adds {forbidden}"
            assert service.get("privileged") is not True, f"{path.name}:{name} privileged"
            assert service.get("network_mode") != "host", (
                f"{path.name}:{name} uses host network"
            )
    assert seen == 8  # worker-1..3 (prod, multinode) + worker-1..2 (stack)


def test_no_manifest_grants_a_proxy_sidecar_or_mesh_injection() -> None:
    """No k8s pod in this manifest set injects a proxy sidecar.

    A mesh sidecar (or a hand-added nginx/envoy) in front of ``/internal/**``
    would make every worker present one source IP to the control plane, turning
    N49's second factor into dead code.
    """
    for path in sorted(K8S.glob("*.yaml")):
        for doc in _load_all(path):
            if doc.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            spec = doc["spec"]["template"]
            annotations = ((spec.get("metadata") or {}).get("annotations")) or {}
            for name in MESH_INJECT_ANNOTATIONS:
                assert name not in annotations, f"{path.name} injects {name}"
            pod_spec = spec["spec"]
            container_names = [
                c.get("name")
                for c in (pod_spec.get("initContainers") or [])
                + (pod_spec.get("containers") or [])
            ]
            for proxy in POD_PROXY_CONTAINER_NAMES:
                assert proxy not in container_names, (
                    f"{path.name} runs a proxy container {proxy!r}"
                )


# ------------------------------------- the internal API is reached directly


def test_the_k8s_internal_api_service_is_a_plain_clusterip() -> None:
    """The control-plane Service is the API's only front: no proxy, no ingress.

    Its selector must point straight at the control-plane pods and its type must
    stay the default ClusterIP (a NodePort/LoadBalancer/ExternalName rewrites the
    hop), and no Ingress may exist in this manifest set at all.
    """
    docs = _load_all(K8S / "control-plane.yaml")
    service = next(
        d for d in docs if d.get("kind") == "Service" and d["metadata"]["name"] == "control-plane"
    )
    assert service["spec"]["selector"] == {"app": "control-plane"}
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert service["spec"]["ports"] == [{"port": 3000}]
    for path in sorted(K8S.glob("*.yaml")):
        for doc in _load_all(path):
            assert doc.get("kind") != "Ingress", f"{path.name} adds an Ingress"


def test_workers_dial_the_control_plane_directly_in_k8s() -> None:
    """No proxy hop in the k8s worker: the Service's DNS name, port 3000."""
    docs = _load_all(K8S / "worker.yaml")
    statefulset = next(d for d in docs if d.get("kind") == "StatefulSet")
    env = {
        entry["name"]: entry.get("value")
        for entry in statefulset["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["E2B_CONTROL_PLANE_URL"] == "http://control-plane:3000"


def test_compose_workers_dial_the_control_plane_directly() -> None:
    """Every separated compose stack reaches the API by its service name.

    The worker's control-plane URL is the one value that would hide a proxy: if
    it named a proxy service, every worker's source IP would be the proxy's.
    """
    paths = [
        REPO / "deploy" / "compose" / "docker-compose.prod.yml",
        REPO / "deploy" / "compose" / "docker-compose.multinode.yml",
        REPO / "deploy" / "stack" / "docker-compose.prod.yml",
    ]
    for path in paths:
        compose = _compose(path)
        for name, service in (compose.get("services") or {}).items():
            if not name.startswith("worker"):
                continue
            value = _compose_env(service)["E2B_CONTROL_PLANE_URL"]
            # ``${E2B_CONTROL_PLANE_URL:-http://control-plane:3000}``: the
            # default is the direct Service name, which is what the pinned
            # value here asserts (an override is the operator's own doing).
            default = value.split(":-", 1)[1].rstrip("}") if ":-" in value else value
            assert default == "http://control-plane:3000", f"{path.name}:{name}"


# ------------------------------- the mode is pinned in every production shape


def _k8s_control_plane_env() -> dict[str, str]:
    """The `control-plane` container's env by name (parsed, not grepped)."""
    deployments = [
        doc
        for doc in _load_all(K8S / "control-plane.yaml")
        if doc.get("kind") == "Deployment"
    ]
    assert len(deployments) == 1
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    env = next(c for c in containers if c["name"] == "control-plane")["env"]
    return {entry["name"]: entry.get("value") for entry in env}


def test_the_k8s_control_plane_pins_the_k8s_address_mode() -> None:
    """D5.2/D5.4: production must not be on ``auto``.

    ``auto`` is the dev/local default (``local://`` is explicitly out of C3's
    scope). A production manifest left on ``auto`` could silently pick a mode
    that cannot verify a claim; the explicit value is what the internal API's
    fail-closed/resolve behavior is designed against.
    """
    assert _k8s_control_plane_env()["E2B_NODE_ADDRESS_MODE"] == "k8s"
    assert _k8s_control_plane_env()["E2B_NODE_ADDRESS_NAMESPACE"] == "sandlock"


def test_the_k8s_control_plane_holds_exactly_the_grants_it_uses() -> None:
    """The control plane's RBAC is an exact list, and it grew by one rule set.

    Two readers use it, and the test spells out both so neither can grow
    quietly:

    * the `k8s` node-address resolver needs `get`/`list` on pods in this
      namespace (without it the mode fails closed -- every node-scoped request
      503s; C3 Task 3/D13 added `list` for the agent pod on the worker's node);
    * since 2026-09-30 this pod also hosts the worker fleet's autoscaler, whose
      `ScaleBackend` reads and scales the worker workload and retires a drained
      pod -- the three rules below are the retired `autoscaler` Deployment's,
      moved here verbatim (2026-09-30), so the *set* of grants is unchanged
      even though the workload that holds them is not.

    "Exactly" is the point: no wildcard verbs, no `create` (the loop only reads
    and scales), no secrets, and no second namespace.
    """
    docs = _load_all(K8S / "control-plane.yaml")
    by_kind = {}
    for doc in docs:
        by_kind.setdefault(doc.get("kind"), []).append(doc)
    accounts = by_kind.get("ServiceAccount") or []
    assert [a["metadata"]["name"] for a in accounts] == ["control-plane"]
    roles = by_kind.get("Role") or []
    assert len(roles) == 1
    assert roles[0]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["pods"],
            "verbs": ["get", "list", "patch", "delete"],
        },
        {
            "apiGroups": ["apps"],
            "resources": [
                "statefulsets",
                "statefulsets/scale",
            ],
            "verbs": ["get", "update", "patch"],
        },
    ]
    bindings = by_kind.get("RoleBinding") or []
    assert len(bindings) == 1
    assert bindings[0]["roleRef"] == {
        "kind": "Role",
        "name": "control-plane-pod-reader",
        "apiGroup": "rbac.authorization.k8s.io",
    }
    assert bindings[0]["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "control-plane",
            "namespace": "sandlock",
        }
    ]
    deployment = (by_kind["Deployment"] or [])[0]
    assert (
        deployment["spec"]["template"]["spec"]["serviceAccountName"]
        == "control-plane"
    )
    # No ClusterRole / ClusterRoleBinding: the resolver only ever reads pods in
    # its own namespace, so cluster-wide reads are not granted.
    assert "ClusterRole" not in by_kind
    assert "ClusterRoleBinding" not in by_kind


def test_every_production_compose_control_plane_pins_the_hostname_mode() -> None:
    """Same rule for the compose shapes: explicit ``hostname``, never ``auto``."""
    paths = [
        REPO / "deploy" / "compose" / "docker-compose.prod.yml",
        REPO / "deploy" / "compose" / "docker-compose.multinode.yml",
        REPO / "deploy" / "stack" / "docker-compose.prod.yml",
    ]
    for path in paths:
        services = _compose(path).get("services") or {}
        control_plane = services.get("control-plane")
        assert control_plane is not None, f"{path.name} has no control-plane"
        env = _compose_env(control_plane)
        assert env["E2B_NODE_ADDRESS_MODE"] == "hostname", path.name


def test_no_worker_shape_carries_the_agent_token() -> None:
    """Task 3's rule, pinned early: the agent's credential is CP↔agent only.

    A worker that could read ``E2B_C3_AGENT_TOKEN`` would hold the agent's
    credential, and the ``CP→agent`` channel would be reachable from the
    (untrusted) data plane -- exactly the channel hard rule 5 says does not
    exist.

    Two shapes are checked, because the answer differs by file: manifest sets
    that hold **only** worker-shaped services (the k8s worker StatefulSet, the
    worker image) are scanned as raw text, while the compose stacks that put
    the control plane, the agent and the workers in one file -- the two
    separated examples and, since Task 4 slice B (D17), the target host's
    stack -- have the token legitimately as the *control plane's* and the *two
    agent faces*'; there the scan is per service and reads the worker
    services' own env.

    The autoscaler's local pool used to be a third raw-text source; it is
    retired (2026-09-30) and the loop now runs inside the k8s control plane,
    whose token surface the per-service scan of that stack already covers.
    """
    worker_only_sources = [
        K8S / "worker.yaml",
        REPO / "deploy" / "docker" / "Dockerfile.envd",
    ]
    for path in worker_only_sources:
        assert "E2B_C3_AGENT_TOKEN" not in path.read_text(encoding="utf-8"), path
    for path in (
        REPO / "deploy" / "compose" / "docker-compose.prod.yml",
        REPO / "deploy" / "compose" / "docker-compose.multinode.yml",
        REPO / "deploy" / "stack" / "docker-compose.prod.yml",
    ):
        services = _compose(path)["services"]
        workers = [name for name in services if name.startswith("worker")]
        assert workers, path.name
        for name in workers:
            env = _compose_env(services[name])
            assert "E2B_C3_AGENT_TOKEN" not in env, (path.name, name)
        # ...and the token *is* where it belongs (the faces that must
        # authenticate the CP→agent hop), so the per-service scan above cannot
        # pass by the key having been dropped from the file entirely.
        for face in ("c3-agent", "c3-agent-maint"):
            assert "E2B_C3_AGENT_TOKEN" in _compose_env(services[face]), (
                path.name,
                face,
            )


# ------------------ N83 phase 1: the one-shot cgroup-delegation handshake
#
# Shape W (per-sandbox cgroup, phase 1) gives the worker a one-time handshake:
# at startup it asks the control plane, once, to have the node's agent delegate
# the worker's own container cgroup subtree to it. Everything privileged stays
# where it already was -- the worker names nothing (the body is empty; the
# control plane derives the node, the object and the anchor), the control plane
# dials the agent's **face B** with the lane's own anchor, and every refusal on
# the way is named (a node with no anchor, no client, an agent that refuses).
#
# The anchor is the D21/D25 pair, one shape each: compose carries the worker's
# container id, k8s carries the worker pod's uid -- and a lane that has neither
# refuses by name instead of instructing an agent that cannot locate the
# container the delegation is for.

NODE_ID = "node_a"
KEY_NODE = "key-node-a"
FLEET = "fleet-key"
NODE_ENDPOINT = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
CONTAINER_ID = "3f2a1b0c9d8e"
WORKER_POD_UID = "6d3cdd7b-3a5e-4a1f-9a6b-0c1d2e3f4a5b"
#: The worker's own uid -- the delegation's second discriminator (N83 fix).
WORKER_UID = 65534
WORKER_GID = 65534
DELEGATION_ANSWER = {
    "op": "delegate-cgroup",
    # Task 3's own answer shape: the path inside the *agent's* cgroup view, the
    # entries it chowned (``"."`` is the container directory itself -- and
    # ``cpu.max`` is never among them), and who still owns the limit file.
    "containerCgroup": "/host-cgroup/docker/3f2a1b0c9d8e",
    "delegated": [".", "cgroup.procs", "cgroup.subtree_control"],
    "cpuMaxOwner": "0:0",
}


class _StubDelegateClient:
    """Records the delegation instruction; answers like the agent would."""

    def __init__(
        self,
        *,
        answer: dict | None = None,
        refuse: str | None = None,
        status_code: int = 502,
    ) -> None:
        self.calls: list[dict] = []
        self._answer = DELEGATION_ANSWER if answer is None else answer
        self._refuse = refuse
        self._status_code = status_code

    async def delegate_cgroup(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._refuse is not None:
            raise AgentClientError(self._refuse, status_code=self._status_code)
        return dict(self._answer)


def _delegation_settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        internal_api_key=FLEET,
        internal_api_keys=(),
        internal_node_keys={KEY_NODE: NODE_ID},
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


def _delegation_app(workspace, *, client, settings=None, worker_identity_source=None):
    settings = settings or _delegation_settings()
    return create_control_app(
        settings=settings,
        registry=SandboxRegistry(settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        workspace_base=workspace,
        node_address_resolver=StaticAddressResolver({NODE_ID: NODE_ENDPOINT}),
        c3_agent_client=client,
        # The k8s shape (no kernel deferral -> no anchor travels): the identity
        # is verified by the control plane itself, so the client uses the
        # resolver's pod uid. The compose lane's kernel-deferral shape is what
        # the two lane tests below name explicitly.
        worker_identity_source=(
            StaticWorkerIdentitySource({NODE_ID: (WORKER_UID, WORKER_GID)})
            if worker_identity_source is None
            else worker_identity_source
        ),
    )


def _delegation_client(app, *, source_ip: str = "10.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _enroll_node(
    app, *, key: str = KEY_NODE, container_id=None, sandbox_ceiling=None
) -> None:
    """Register node A -- the node-scoped identity every delegation needs."""
    body = {
        "nodeID": NODE_ID,
        "address": NODE_ENDPOINT.address,
        "totalMemoryMB": 1024,
        "totalCPUPercent": 100,
        "totalDiskMB": 1024,
        "totalProcesses": 64,
    }
    if container_id is not None:
        body["containerID"] = container_id
    if sandbox_ceiling is not None:
        body["sandboxCeiling"] = sandbox_ceiling
    body["workerUID"] = WORKER_UID
    body["workerGID"] = WORKER_GID
    async with _delegation_client(app) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": key},
            json=body,
        )
    assert resp.status_code == 200


async def _post_delegation(app, *, headers, source_ip: str = "10.0.0.1"):
    async with _delegation_client(app, source_ip=source_ip) as client:
        return await client.post(
            f"/internal/nodes/{NODE_ID}/cgroup-delegate",
            headers=headers,
        )


def _delegate(app, *, key: str = KEY_NODE, source_ip: str = "10.0.0.1"):
    return asyncio.run(
        _post_delegation(app, headers={"X-Internal-Key": key}, source_ip=source_ip)
    )


def test_the_delegation_hands_the_agents_answer_back_with_the_node_and_anchor(
    workspace,
) -> None:
    """③ the happy path: the agent's own answer, plus the node and the anchor.

    The control plane does not invent a cgroup path here: the agent located the
    container and performed the chown, so ``containerCgroup`` and ``delegated``
    are *its* words, carried back verbatim. What the control plane adds is the
    identity it proved (the node) and the anchor it derived (``None`` in this
    k8s-shaped lane, where the resolver's pod uid is the anchor instead).
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))
    resp = _delegate(app)
    assert resp.status_code == 200
    assert resp.json() == {
        **DELEGATION_ANSWER,
        "nodeID": NODE_ID,
        "workerAnchor": None,
    }
    # The worker names nothing: the instruction is the control plane's whole
    # derivation (the node from the credential, the anchor from its records).
    assert agent.calls == [
        {
            "node_id": NODE_ID,
            "worker_container_id": None,
            "worker_uid": WORKER_UID,
        }
    ]


def test_a_second_delegation_call_succeeds_the_same_way(workspace) -> None:
    """④ idempotent: a worker that retries its startup handshake is not punished.

    The chown the agent performs is idempotent, so the handshake is too -- the
    second answer is byte-for-byte the first, and nothing about the first call
    is remembered to refuse the second (a restarted worker re-asks).
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))
    first = _delegate(app)
    second = _delegate(app)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()
    assert agent.calls == [
        {
            "node_id": NODE_ID,
            "worker_container_id": None,
            "worker_uid": WORKER_UID,
        },
        {
            "node_id": NODE_ID,
            "worker_container_id": None,
            "worker_uid": WORKER_UID,
        },
    ]


def test_an_agents_refusal_is_forwarded_by_name(workspace) -> None:
    """② the agent refused (or could not be reached): the refusal travels.

    The control plane's answer carries the agent hop's own words and its own
    status, so "the agent would not delegate this subtree" is never flattened
    into a bare 500 -- or worse, into a success.
    """
    agent = _StubDelegateClient(
        refuse=(
            f"the agent for node {NODE_ID} refused the cgroup delegation: "
            "worker container cgroup is ambiguous"
        ),
    )
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))
    resp = _delegate(app)
    assert resp.status_code == 502
    assert resp.json() == {
        "code": 502,
        "message": (
            f"the agent for node {NODE_ID} refused the cgroup delegation: "
            "worker container cgroup is ambiguous"
        ),
    }


def test_a_lane_with_no_anchor_refuses_before_dialling(workspace) -> None:
    """① no anchor, no instruction: a named 503 and nothing on the wire.

    This is the client's own refusal (R-B): a resolved target that carries no
    pod uid and a caller that carried no container id leaves the agent unable
    to locate the worker's container, so the instruction is never sent.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DELEGATION_ANSWER)

    client = C3AgentClient(
        resolver=ComposeAgentAddressResolver(
            "http://c3-agent:49985", "http://c3-agent-maint:49986"
        ),
        token="c3-agent-sekret",
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )
    app = _delegation_app(workspace, client=client)
    asyncio.run(_enroll_node(app))
    resp = _delegate(app)
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_ID} carries no anchor the agent can locate its worker "
            "container by (compose: the worker's container id; k8s: the worker "
            "pod uid): refusing to instruct the agent"
        ),
    }
    assert seen == []


def test_the_compose_lane_carries_the_container_id_anchor_to_face_b() -> None:
    """The compose lane: the worker's container id, addressed to face B."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DELEGATION_ANSWER)

    client = C3AgentClient(
        resolver=ComposeAgentAddressResolver(
            "http://c3-agent:49985", "http://c3-agent-maint:49986"
        ),
        token="c3-agent-sekret",
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )
    answer = asyncio.run(
        client.delegate_cgroup(
            node_id=NODE_ID,
            worker_container_id=CONTAINER_ID,
            worker_uid=WORKER_UID,
        )
    )
    assert answer == DELEGATION_ANSWER
    assert len(seen) == 1
    request = seen[0]
    # Face B -- the same listener the privileged file verbs use (D22).
    assert str(request.url) == (
        f"http://c3-agent-maint:49986/internal/nodes/c3-agent/agent/delegate-cgroup"
    )
    assert request.headers["X-Internal-Key"] == "c3-agent-sekret"
    assert json.loads(request.content) == {
        "worker": {
            "node_id": NODE_ID,
            "container_id": CONTAINER_ID,
            "uid": WORKER_UID,
        }
    }


def test_the_k8s_lane_carries_the_worker_pod_uid_to_face_b() -> None:
    """The k8s lane: the pod uid the resolver read, never the worker's word."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DELEGATION_ANSWER)

    client = C3AgentClient(
        resolver=StaticAgentAddressResolver(
            {
                NODE_ID: AgentTarget(
                    node_identity="k0s-worker-0",
                    url="http://10.244.1.7:49985",
                    pod_uid=WORKER_POD_UID,
                    maint_url="http://10.244.1.7:49986",
                )
            }
        ),
        token="c3-agent-sekret",
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.delegate_cgroup(node_id=NODE_ID, worker_uid=WORKER_UID))
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == (
        "http://10.244.1.7:49986/internal/nodes/k0s-worker-0/agent/delegate-cgroup"
    )
    assert json.loads(request.content) == {
        "worker": {
            "node_id": NODE_ID,
            "pod_uid": WORKER_POD_UID,
            "uid": WORKER_UID,
        }
    }


def test_a_shape_without_a_face_b_address_refuses_the_delegation_by_name() -> None:
    """A compose shape that named only face A cannot delegate."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DELEGATION_ANSWER)

    client = C3AgentClient(
        resolver=ComposeAgentAddressResolver("http://c3-agent:49985"),
        token="c3-agent-sekret",
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AgentClientError) as excinfo:
        asyncio.run(
            client.delegate_cgroup(
                node_id=NODE_ID,
                worker_container_id=CONTAINER_ID,
                worker_uid=WORKER_UID,
            )
        )
    assert str(excinfo.value) == (
        f"cannot determine the cgroup-delegation agent address for node "
        f"{NODE_ID} (E2B_C3_AGENT_MAINT_URL / E2B_C3_AGENT_MAINT_PORT): "
        "refusing to instruct an agent the control plane cannot locate"
    )
    assert excinfo.value.status_code == 503
    assert seen == []


def test_the_identity_layer_guards_the_delegation_endpoint(workspace) -> None:
    """① credential, ② source IP -- the same layers as every node-scoped handler."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))
    unauthenticated = asyncio.run(_post_delegation(app, headers={}))
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"code": 401, "message": "Unauthorized"}
    stolen = _delegate(app, source_ip="10.0.0.2")
    assert stolen.status_code == 403
    assert stolen.json() == {
        "code": 403,
        "message": (
            f"request for node {NODE_ID} came from 10.0.0.2, expected 10.0.0.1"
        ),
    }
    assert agent.calls == []


def test_the_compose_lane_hands_the_recorded_container_id_to_the_client(
    workspace,
) -> None:
    """The compose anchor: the node record's own container id (D25).

    This is the half of R-A's derivation that only the control plane can do:
    the worker names nothing, the resolver has no pod uid in this lane, and the
    value the agent must locate the container by comes from the record the
    worker reported at register/heartbeat -- read back here through
    ``_worker_identity_anchor``, never from the request.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(
        workspace,
        client=agent,
        worker_identity_source=KernelWorkerIdentitySource(),
    )
    asyncio.run(_enroll_node(app, container_id=CONTAINER_ID))
    resp = _delegate(app)
    assert resp.status_code == 200
    assert resp.json() == {
        **DELEGATION_ANSWER,
        "nodeID": NODE_ID,
        "workerAnchor": CONTAINER_ID,
    }
    assert agent.calls == [
        {
            "node_id": NODE_ID,
            "worker_container_id": CONTAINER_ID,
            "worker_uid": WORKER_UID,
        }
    ]


def test_a_compose_node_with_no_container_id_is_a_named_503(workspace) -> None:
    """The kernel-deferral shape with nothing recorded refuses by name.

    R-B's compose clause, and the reason it lives in the control plane: this is
    a *record* problem (an older worker, or a stack that overrode ``hostname:``),
    not a missing pod uid -- and an instruction the agent cannot confirm is
    exactly the one that must not be sent.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(
        workspace,
        client=agent,
        worker_identity_source=KernelWorkerIdentitySource(),
    )
    asyncio.run(_enroll_node(app))
    resp = _delegate(app)
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_ID} has reported no container id for the agent to "
            "confirm its worker identity against (a C3 worker must keep the "
            "runtime's hostname): refusing to instruct the agent"
        ),
    }
    assert agent.calls == []


def test_a_node_with_no_worker_uid_is_a_named_503(workspace) -> None:
    """N83 fix ①: the delegation now needs the worker's own uid as a discriminator.

    A node record with no verified ``worker_uid`` (an older worker, or a shape
    whose pin the control plane could not read) cannot name the value the agent
    must match against ``/proc/<pid>/status`` -- and an instruction without it is
    the one the agent must refuse. The control plane names that *here*, the same
    503 the file-op path answers, and nothing reaches the wire.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(
        workspace,
        client=agent,
        worker_identity_source=StaticWorkerIdentitySource({}),
    )
    asyncio.run(_enroll_node(app))
    resp = _delegate(app)
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"node {NODE_ID} has reported no worker identity (workerUID/"
            "workerGID): refusing to instruct the agent"
        ),
    }
    assert agent.calls == []


def test_no_agent_client_refuses_the_delegation_by_name(workspace) -> None:
    """A control plane with no agent wired refuses; it does not 500."""
    app = _delegation_app(workspace, client=None)
    asyncio.run(_enroll_node(app))
    resp = _delegate(app)
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            "this control plane has no C3 agent client configured: refusing to "
            "delegate the worker's cgroup"
        ),
    }


# ------------------------------- N83 phase 2: the per-sandbox ceiling
#
# D5: the ceiling a *single* sandbox may be configured to is a configured
# policy (``E2B_MAX_SANDBOX_*``) that defaults to the corresponding node total
# -- never 0 and never infinity. The control plane's ``Settings`` fields are
# the in-process ``local://`` node's copy of that rule; a *worker* node gets
# both copies from the worker's own heartbeat (``sandboxCeiling``): the policy
# it resolved and the kernel's read of its own container cgroup (``None`` =
# the kernel sets no limit). They are stored side by side so a reader can see
# both the promise and the physical ceiling it was checked against.

#: One worker's report: a 4-core/4 GiB container with a 2-core/2 GiB policy.
SANDBOX_CEILING = {
    "cpuPercent": 200,
    "memoryMB": 2048,
    "processes": 256,
    "kernelCpuPercent": 400,
    "kernelMemoryMB": 4096,
}

_CEILING_ENVS = (
    "E2B_MAX_SANDBOX_CPU_PERCENT",
    "E2B_MAX_SANDBOX_MEMORY_MB",
    "E2B_MAX_SANDBOX_PROCESSES",
)


async def _heartbeat_node(app, body: dict, *, key: str = KEY_NODE):
    async with _delegation_client(app) as client:
        return await client.post(
            f"/internal/nodes/{NODE_ID}/heartbeat",
            headers={"X-Internal-Key": key},
            json=body,
        )


async def _internal_node_view(app, *, key: str = FLEET):
    async with _delegation_client(app) as client:
        return await client.get("/internal/nodes", headers={"X-Internal-Key": key})


def test_the_control_plane_ceiling_defaults_to_the_node_total(
    monkeypatch,
) -> None:
    """① unset ⇒ the node's own total. Never 0 ("unlimited"), never infinity."""
    for name in _CEILING_ENVS:
        monkeypatch.delenv(name, raising=False)
    settings = ControlSettings(
        api_keys=("local-key",),
        internal_api_key=FLEET,
        max_total_memory_mb=8192,
        max_total_cpu_percent=400,
        max_total_processes=2048,
    )

    assert settings.max_sandbox_cpu_percent == 400
    assert settings.max_sandbox_memory_mb == 8192
    assert settings.max_sandbox_processes == 2048


def test_a_non_positive_control_plane_ceiling_follows_the_node_total(
    monkeypatch,
) -> None:
    """``0``/negative is not "unlimited" here: it follows the node total."""
    monkeypatch.setenv("E2B_MAX_SANDBOX_CPU_PERCENT", "0")
    monkeypatch.setenv("E2B_MAX_SANDBOX_MEMORY_MB", "-1")
    monkeypatch.setenv("E2B_MAX_SANDBOX_PROCESSES", "0")
    settings = ControlSettings(
        api_keys=("local-key",),
        internal_api_key=FLEET,
        max_total_memory_mb=8192,
        max_total_cpu_percent=400,
        max_total_processes=2048,
    )

    assert settings.max_sandbox_cpu_percent == 400
    assert settings.max_sandbox_memory_mb == 8192
    assert settings.max_sandbox_processes == 2048


def test_the_control_plane_ceiling_is_never_zero(monkeypatch) -> None:
    """Even a node that declared no total gets a positive per-sandbox ceiling.

    ``0`` on this field would read downstream as "one sandbox may take
    everything" -- the fail-open the plan's Review Focus §1 names. With no
    declared node total the create default is the only positive signal left.
    """
    for name in _CEILING_ENVS:
        monkeypatch.delenv(name, raising=False)
    settings = ControlSettings(
        api_keys=("local-key",),
        internal_api_key=FLEET,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_processes=0,
        default_cpu_percent=100,
        default_memory_mb=1024,
        default_max_processes=256,
    )

    assert settings.max_sandbox_cpu_percent == 100
    assert settings.max_sandbox_memory_mb == 1024
    assert settings.max_sandbox_processes == 256


def test_an_explicit_control_plane_ceiling_is_independent_of_the_node_total(
    monkeypatch,
) -> None:
    """② an explicit ceiling wins, however big the node is."""
    monkeypatch.setenv("E2B_MAX_SANDBOX_CPU_PERCENT", "200")
    monkeypatch.setenv("E2B_MAX_SANDBOX_MEMORY_MB", "1024")
    monkeypatch.setenv("E2B_MAX_SANDBOX_PROCESSES", "64")
    settings = ControlSettings(
        api_keys=("local-key",),
        internal_api_key=FLEET,
        max_total_memory_mb=65536,
        max_total_cpu_percent=1600,
        max_total_processes=8192,
    )

    assert settings.max_sandbox_cpu_percent == 200
    assert settings.max_sandbox_memory_mb == 1024
    assert settings.max_sandbox_processes == 64


def test_the_heartbeat_carries_both_ceilings_into_the_node_record(
    workspace,
) -> None:
    """⑤ the record keeps the policy copy *and* the kernel copy, side by side.

    Registration carries it too (it is the first heartbeat), and a heartbeat
    that reports nothing leaves what the record already holds alone -- that is
    what keeps a mixed-version rollout from erasing the ceilings.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app, sandbox_ceiling=SANDBOX_CEILING))

    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096

    # A later beat refreshes both copies (a lower policy ceiling is a normal
    # re-deploy, not an error) ...
    resp = asyncio.run(
        _heartbeat_node(
            app,
            {
                "sandboxCeiling": {
                    "cpuPercent": 100,
                    "memoryMB": 1024,
                    "processes": 128,
                    "kernelCpuPercent": None,
                    "kernelMemoryMB": None,
                }
            },
        )
    )
    assert resp.status_code == 204
    assert record.sandbox_cpu_percent_max == 100
    assert record.sandbox_memory_mb_max == 1024
    assert record.sandbox_processes_max == 128
    assert record.kernel_cpu_percent is None
    assert record.kernel_memory_mb is None

    # ... and the internal view exposes both copies under their own names.
    view = asyncio.run(_internal_node_view(app))
    assert view.status_code == 200
    (node,) = [n for n in view.json() if n["nodeID"] == NODE_ID]
    assert node["sandboxCPUPercentMax"] == 100
    assert node["sandboxMemoryMBMax"] == 1024
    assert node["sandboxProcessesMax"] == 128
    assert node["kernelCPUPercent"] is None
    assert node["kernelMemoryMB"] is None


def test_a_heartbeat_without_a_ceiling_leaves_the_record_alone(workspace) -> None:
    """An older worker during a rollout reports nothing -- never erase."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app, sandbox_ceiling=SANDBOX_CEILING))

    assert asyncio.run(_heartbeat_node(app, {"diskUsedMB": 4096})).status_code == 204

    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096


def test_a_zero_sandbox_ceiling_in_a_heartbeat_is_refused_by_name(
    workspace,
) -> None:
    """A 0 is refused rather than stored: downstream it would mean "unlimited"."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))

    resp = asyncio.run(
        _heartbeat_node(
            app,
            {
                "sandboxCeiling": {
                    "cpuPercent": 0,
                    "memoryMB": 2048,
                    "processes": 256,
                }
            },
        )
    )

    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "sandboxCeiling.cpuPercent must be a positive integer: the "
            "per-sandbox ceiling is never 0 (which would read as unlimited) "
            "or negative -- the worker resolves an unset E2B_MAX_SANDBOX_* "
            "to its node total"
        ),
    }
    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 0
