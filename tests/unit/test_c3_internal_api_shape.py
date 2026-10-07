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
import logging
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
    app, *, key: str = KEY_NODE, container_id=None, kernel_ceiling=None
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
    if kernel_ceiling is not None:
        body["kernelCeiling"] = kernel_ceiling
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
# -- never 0 and never infinity. Ruling R17 (2026-10-07) makes that policy the
# **control plane's**: it resolves the policy per node (``Settings
# .sandbox_ceiling_for``, called where that node's record is stamped), writes it
# into every node record and returns it in the register/heartbeat **response**
# (the hand-down a worker adopts), and a worker's own report of the same three
# names is ignored. What a worker still reports is the *physical* half -- the
# kernel's own limits on its container cgroup (``null`` = no limit), under
# ``kernelCeiling``. Both halves are stored side by side on the node record, so
# a reader can see the promise and the physical ceiling it was checked against.

#: One worker's hand-down: a 2-core/2 GiB policy for every node, with a
#: kernel reading of a 4-core/4 GiB container.
SANDBOX_CEILING = {
    "cpuPercent": 200,
    "memoryMB": 2048,
    "processes": 256,
    "kernelCpuPercent": 400,
    "kernelMemoryMB": 4096,
}

#: ...and that same hand-down as it goes the other way: the *response* body of a
#: register/heartbeat, built from this control plane's own Settings.
HAND_DOWN = {"cpuPercent": 200, "memoryMB": 2048, "processes": 256}

#: What a worker on this build reports now: the physical half, under its own
#: name. The kernel says 4 cores / 4 GiB, the control plane's policy is smaller.
KERNEL_CEILING = {"cpuPercent": 400, "memoryMB": 4096}

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


def _declared_no_trio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the three ``E2B_MAX_SANDBOX_*``: the shape Task 10 is about.

    A bare ``python -m control_plane``, the SDK test-runner and the embedded
    shapes declare none of them, so the per-sandbox ceiling has to come from
    somewhere else -- the node that reports.
    """
    for name in _CEILING_ENVS:
        monkeypatch.delenv(name, raising=False)


#: That same shape's ``Settings``: ``E2B_MAX_TOTAL_*`` are 0 too ("derive",
#: Task 9), and the create defaults are deliberately distinctive so a silent
#: fall-through to them is visible in the numbers.
def _bare_ceiling_settings(**overrides) -> ControlSettings:
    defaults = dict(
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_processes=0,
        default_cpu_percent=50,
        default_memory_mb=777,
        default_max_processes=99,
    )
    defaults.update(overrides)
    return _delegation_settings(**defaults)


def _node_totals(memory_mb: int, cpu_percent: int, processes: int) -> dict[str, int]:
    """The totals a node reports about itself -- the register body's keys."""
    return {
        "totalMemoryMB": memory_mb,
        "totalCPUPercent": cpu_percent,
        "totalProcesses": processes,
    }


def test_the_hand_down_ceiling_follows_the_nodes_own_total(
    workspace, monkeypatch
) -> None:
    """① no trio ⇒ the ceiling is *this node's* total. Never 0, never infinity.

    Task 10: resolving the ceiling once in ``Settings.__post_init__`` made this
    shape fall through to the create default -- the control plane's own total is
    0 there ("derive"), so the middle rung was skipped and a 2 GiB create was
    refused with a named 400 against a 1024 MiB promise. The resolution now
    happens where the node record is stamped, against the totals the node
    reported.
    """
    _declared_no_trio(monkeypatch)
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_bare_ceiling_settings())
    # Task 10's contract: the trio stays **raw** on ``Settings`` (0 = "not
    # declared"), and all resolving happens per node at stamping time. A
    # ``__post_init__`` that resolved them again -- the defect this task
    # removed -- would make these red instead of quietly changing which rung
    # the resolution takes.
    assert app.state.settings.max_sandbox_cpu_percent == 0
    assert app.state.settings.max_sandbox_memory_mb == 0
    assert app.state.settings.max_sandbox_processes == 0
    expected = {"cpuPercent": 200, "memoryMB": 2048, "processes": 128}

    registered = asyncio.run(
        _register_node(
            app,
            body={
                "nodeID": NODE_ID,
                "address": NODE_ENDPOINT.address,
                "totalDiskMB": 1024,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
                **_node_totals(2048, 200, 128),
            },
        )
    )
    assert registered.status_code == 200
    assert registered.json() == {"nodeID": NODE_ID, "sandboxCeiling": expected}
    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 128

    # The hand-down rides every beat, and the number is the same one: the
    # record's own totals are the resolution's only input.
    beat = asyncio.run(_heartbeat_node(app, {"diskUsedMB": 4096}))
    assert beat.status_code == 200
    assert beat.json() == {"sandboxCeiling": expected}


def test_a_ceiling_follows_its_nodes_total_when_that_total_changes(
    workspace, monkeypatch
) -> None:
    """① again, across a change: the resolution is per node **and per beat**.

    D5's middle rung is the node's own reported total, and a node's total can
    change (a re-created container with a different ``memory.max``, a new
    ``E2B_NODE_*``). The re-registration carries the new totals, so the answer
    and the record move with them -- and every beat after it keeps the new
    number, because the beat resolves against the record's totals.
    """
    _declared_no_trio(monkeypatch)
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_bare_ceiling_settings())
    small = {"cpuPercent": 100, "memoryMB": 1024, "processes": 64}
    large = {"cpuPercent": 800, "memoryMB": 8192, "processes": 2048}

    first = asyncio.run(
        _register_node(
            app,
            body={
                "nodeID": NODE_ID,
                "address": NODE_ENDPOINT.address,
                "totalDiskMB": 1024,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
                **_node_totals(1024, 100, 64),
            },
        )
    )
    assert first.status_code == 200
    assert first.json() == {"nodeID": NODE_ID, "sandboxCeiling": small}

    again = asyncio.run(
        _register_node(
            app,
            body={
                "nodeID": NODE_ID,
                "address": NODE_ENDPOINT.address,
                "totalDiskMB": 1024,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
                **_node_totals(8192, 800, 2048),
            },
        )
    )
    assert again.status_code == 200
    assert again.json() == {"nodeID": NODE_ID, "sandboxCeiling": large}

    beat = asyncio.run(_heartbeat_node(app, {"diskUsedMB": 4096}))
    assert beat.status_code == 200
    assert beat.json() == {"sandboxCeiling": large}
    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 800
    assert record.sandbox_memory_mb_max == 8192
    assert record.sandbox_processes_max == 2048


def test_a_node_that_reports_no_total_falls_to_the_create_default(
    workspace, monkeypatch
) -> None:
    """② a node with no total of its own ⇒ the create default, and never 0.

    The middle rung is the node's *own* report, not the control plane's fleet
    total: ``E2B_MAX_TOTAL_*`` says how much the fleet may sell (and its 0 now
    means "derive from the nodes"), so it is not a per-node promise. A node
    that declares nothing leaves the create default as the only positive
    signal -- never a ``0``, which downstream would read as "one sandbox may
    take everything" (the plan's Review Focus §1).
    """
    _declared_no_trio(monkeypatch)
    agent = _StubDelegateClient()
    app = _delegation_app(
        workspace,
        client=agent,
        settings=_bare_ceiling_settings(
            max_total_memory_mb=8192, max_total_cpu_percent=400
        ),
    )

    registered = asyncio.run(
        _register_node(
            app,
            body={
                "nodeID": NODE_ID,
                "address": NODE_ENDPOINT.address,
                "totalDiskMB": 1024,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
                **_node_totals(0, 0, 0),
            },
        )
    )
    assert registered.status_code == 200
    assert registered.json() == {
        "nodeID": NODE_ID,
        "sandboxCeiling": {"cpuPercent": 50, "memoryMB": 777, "processes": 99},
    }
    assert 0 not in registered.json()["sandboxCeiling"].values()


def test_an_explicit_ceiling_wins_and_is_not_clamped_to_the_node(workspace) -> None:
    """③ an explicit trio is handed down verbatim -- above the node total too.

    The only clamp in this chain is D5b, on the *worker*: a handed-down ceiling
    above that worker's kernel limit is refused there, by name. Clamping here
    would turn a loud configuration error into a quieter, smaller promise.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(
        workspace,
        client=agent,
        settings=_delegation_settings(
            max_sandbox_cpu_percent=1600,
            max_sandbox_memory_mb=2048,
            max_sandbox_processes=256,
        ),
    )

    registered = asyncio.run(
        _register_node(
            app,
            body={
                "nodeID": NODE_ID,
                "address": NODE_ENDPOINT.address,
                "totalDiskMB": 1024,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
                # The node reports less than two of the three explicit values.
                **_node_totals(1024, 800, 64),
            },
        )
    )
    assert registered.status_code == 200
    assert registered.json() == {
        "nodeID": NODE_ID,
        "sandboxCeiling": {"cpuPercent": 1600, "memoryMB": 2048, "processes": 256},
    }
    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 1600
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256


#: A second node with its own address and credential, for the heterogeneous
#: case below: D5 resolves per *node*, so two nodes may legitimately differ.
NODE_B_ID = "node_b"
KEY_NODE_B = "key-node-b"
NODE_B_ENDPOINT = NodeEndpoint("http://10.0.0.2:49983", "10.0.0.2")


async def _register_and_beat(
    app, *, node_id: str, key: str, endpoint, totals: tuple[int, int, int]
) -> tuple[httpx.Response, httpx.Response]:
    """One node's registration and the heartbeat that follows it."""
    async with _delegation_client(app, source_ip=endpoint.ip) as client:
        registered = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": key},
            json={
                "nodeID": node_id,
                "address": endpoint.address,
                "totalDiskMB": 1024,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
                **_node_totals(*totals),
            },
        )
    async with _delegation_client(app, source_ip=endpoint.ip) as client:
        beat = await client.post(
            f"/internal/nodes/{node_id}/heartbeat",
            headers={"X-Internal-Key": key},
            json={"diskUsedMB": 1024},
        )
    return registered, beat


def test_a_heterogeneous_fleet_gets_its_own_ceiling_per_node(
    workspace, monkeypatch
) -> None:
    """⑤ per node and per beat: two nodes with different totals keep their own.

    With no trio declared, each node's ceiling is resolved from *its own*
    totals, so a fleet of unequal nodes hands down unequal ceilings (each no
    larger than its own total) and each record keeps the number its node was
    handed. The two answers differing is the point, not a leak: R17 makes the
    ceiling the control plane's policy, and D5 resolves that policy against the
    node it is stamped on.
    """
    _declared_no_trio(monkeypatch)
    settings = _delegation_settings(
        internal_node_keys={KEY_NODE: NODE_ID, KEY_NODE_B: NODE_B_ID}
    )
    app = create_control_app(
        settings=settings,
        registry=SandboxRegistry(settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        workspace_base=workspace,
        node_address_resolver=StaticAddressResolver(
            {NODE_ID: NODE_ENDPOINT, NODE_B_ID: NODE_B_ENDPOINT}
        ),
        c3_agent_client=_StubDelegateClient(),
        worker_identity_source=StaticWorkerIdentitySource(
            {NODE_ID: (WORKER_UID, WORKER_GID), NODE_B_ID: (WORKER_UID, WORKER_GID)}
        ),
    )
    small = {"cpuPercent": 100, "memoryMB": 1024, "processes": 64}
    large = {"cpuPercent": 800, "memoryMB": 8192, "processes": 2048}
    nodes = (
        (NODE_ID, KEY_NODE, NODE_ENDPOINT, (1024, 100, 64), small),
        (NODE_B_ID, KEY_NODE_B, NODE_B_ENDPOINT, (8192, 800, 2048), large),
    )

    for node_id, key, endpoint, totals, expected in nodes:
        registered, beat = asyncio.run(
            _register_and_beat(app, node_id=node_id, key=key, endpoint=endpoint, totals=totals)
        )
        assert registered.status_code == 200
        assert registered.json() == {"nodeID": node_id, "sandboxCeiling": expected}
        # Each node's own heartbeat repeats only its own number.
        assert beat.status_code == 200
        assert beat.json() == {"sandboxCeiling": expected}

    assert app.state.nodes.get(NODE_ID).sandbox_memory_mb_max == 1024
    assert app.state.nodes.get(NODE_B_ID).sandbox_memory_mb_max == 8192
    assert app.state.nodes.get(NODE_ID).sandbox_processes_max == 64
    assert app.state.nodes.get(NODE_B_ID).sandbox_processes_max == 2048


#: A control plane whose own policy is the 2-core/2 GiB pair above, so the
#: hand-down's numbers are the *Settings*' and not this host's.
def _ceiling_settings(**overrides) -> ControlSettings:
    defaults = dict(
        max_total_memory_mb=8192,
        max_total_cpu_percent=400,
        max_total_processes=2048,
        max_sandbox_cpu_percent=200,
        max_sandbox_memory_mb=2048,
        max_sandbox_processes=256,
    )
    defaults.update(overrides)
    return _delegation_settings(**defaults)


async def _register_node(app, *, key: str = KEY_NODE, body: dict | None = None):
    async with _delegation_client(app) as client:
        return await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": key},
            json=body
            or {
                "nodeID": NODE_ID,
                "address": NODE_ENDPOINT.address,
                "totalMemoryMB": 1024,
                "totalCPUPercent": 100,
                "totalDiskMB": 1024,
                "totalProcesses": 64,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
            },
        )


def test_the_register_and_heartbeat_answers_carry_the_handed_down_ceiling(
    workspace,
) -> None:
    """⑤ R17: the policy travels **down**, on every lane, from the CP's own env.

    Registration's answer is the first hand-down (the worker adopts it before
    any create can arrive), and every heartbeat's answer repeats it -- which is
    how a re-deploy that lowers ``E2B_MAX_SANDBOX_*`` reaches a running worker
    without a restart. The values are the control plane's, never a worker's.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())

    registered = asyncio.run(_register_node(app))
    assert registered.status_code == 200
    assert registered.json() == {"nodeID": NODE_ID, "sandboxCeiling": HAND_DOWN}

    beat = asyncio.run(_heartbeat_node(app, {"diskUsedMB": 4096}))
    assert beat.status_code == 200
    assert beat.json() == {"sandboxCeiling": HAND_DOWN}

    # ... and the record the create path reads carries the same three numbers,
    # beside the physical reading the worker reported.
    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256


def test_the_record_keeps_the_policy_and_the_kernel_read_side_by_side(
    workspace,
) -> None:
    """The two halves have one owner each: the CP's policy, the worker's kernel.

    The worker's report is the *physical* half only (``kernelCeiling``: the
    kernel's limits on its container cgroup), and a beat that carries none
    leaves the reading the record already holds alone -- that is what keeps a
    mixed-version rollout from erasing the kernel side.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())
    asyncio.run(_enroll_node(app, kernel_ceiling=KERNEL_CEILING))

    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096

    # A later beat refreshes the kernel read (a new container cgroup) ...
    resp = asyncio.run(
        _heartbeat_node(app, {"kernelCeiling": {"cpuPercent": 800, "memoryMB": None}})
    )
    assert resp.status_code == 200
    assert record.kernel_cpu_percent == 800
    assert record.kernel_memory_mb is None
    # ... and the policy is untouched by anything the worker sends: it is the
    # control plane's, and this beat carried no policy at all.
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256

    # The internal view exposes both halves under their own names.
    view = asyncio.run(_internal_node_view(app))
    assert view.status_code == 200
    (node,) = [n for n in view.json() if n["nodeID"] == NODE_ID]
    assert node["sandboxCPUPercentMax"] == 200
    assert node["sandboxMemoryMBMax"] == 2048
    assert node["sandboxProcessesMax"] == 256
    assert node["kernelCPUPercent"] == 800
    assert node["kernelMemoryMB"] is None


def test_a_heartbeat_without_a_kernel_reading_leaves_the_record_alone(
    workspace,
) -> None:
    """An older worker during a rollout reports nothing -- never erase."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())
    asyncio.run(_enroll_node(app, kernel_ceiling=KERNEL_CEILING))

    assert asyncio.run(_heartbeat_node(app, {"diskUsedMB": 4096})).status_code == 200

    record = app.state.nodes.get(NODE_ID)
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256


def test_a_workers_own_policy_report_grants_nothing(workspace) -> None:
    """R17: the *worker's* copy of the trio is ignored, never stored.

    An older worker during a rollout still sends the five-field
    ``sandboxCeiling`` report. Its kernel pair is the reading this control plane
    wants; its policy trio is another process's opinion about a number the
    control plane owns, so it is dropped -- including a ``0``, which downstream
    would read as "one sandbox may take everything".
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())
    asyncio.run(_enroll_node(app))

    resp = asyncio.run(
        _heartbeat_node(
            app,
            {
                "sandboxCeiling": {
                    "cpuPercent": 0,
                    "memoryMB": 65536,
                    "processes": 4096,
                    "kernelCpuPercent": 400,
                    "kernelMemoryMB": 4096,
                }
            },
        )
    )

    assert resp.status_code == 200
    assert resp.json() == {"sandboxCeiling": HAND_DOWN}
    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096


def test_a_non_positive_kernel_reading_costs_only_that_dimension(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """A reading that cannot be read is "no reading", not a refused beat.

    A container's cgroup can be written with a literal that the worker reads as
    ``0`` (``memory.max=0``), and ``0`` elsewhere in this repo means "that
    dimension is not policed" -- so it can never stand in for a ceiling.
    Refusing the **beat** over it would stale the node, and a stale node's live
    sandboxes are reaped as orphans -- the exact consequence ``sandboxEvents``
    cites for choosing per-entry tolerance. The kernel half grants nothing (the
    policy half is this control plane's own), so the bad dimension is simply
    dropped: the record keeps what it already holds and one WARN names the
    field and the raw value.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())
    asyncio.run(_enroll_node(app, kernel_ceiling=KERNEL_CEILING))

    with caplog.at_level(logging.WARNING, logger="control_plane.api.internal"):
        resp = asyncio.run(
            _heartbeat_node(
                app, {"kernelCeiling": {"cpuPercent": 400, "memoryMB": 0}}
            )
        )

    assert resp.status_code == 200
    record = app.state.nodes.get(NODE_ID)
    # The dimension beside it still lands; the unreadable one keeps 4096.
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096
    assert _warnings_from(caplog, "control_plane.api.internal") == [
        "internal API: kernelCeiling.memoryMB is not a positive integer or "
        "null (0); leaving the node's stored kernel reading for that "
        "dimension alone",
    ]


def test_a_wrong_typed_kernel_reading_is_dropped_the_same_way(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """The other arm of the same rule: a string is not a reading either.

    ``"400"`` must not be coerced into a ceiling, and it must not cost the
    beat: it is the same "no reading on this dimension" as the ``0`` above, so
    the record keeps the number the enrollment gave it.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())
    asyncio.run(_enroll_node(app, kernel_ceiling=KERNEL_CEILING))

    with caplog.at_level(logging.WARNING, logger="control_plane.api.internal"):
        resp = asyncio.run(
            _heartbeat_node(
                app, {"kernelCeiling": {"cpuPercent": "400", "memoryMB": 4096}}
            )
        )

    assert resp.status_code == 200
    record = app.state.nodes.get(NODE_ID)
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096
    assert _warnings_from(caplog, "control_plane.api.internal") == [
        "internal API: kernelCeiling.cpuPercent is not a positive integer or "
        "null ('400'); leaving the node's stored kernel reading for that "
        "dimension alone",
    ]


def test_a_non_object_kernel_ceiling_section_is_refused_by_name(workspace) -> None:
    """A section of the wrong *shape* is still refused, like ``sandboxEvents``.

    Only a per-dimension value is tolerant: a ``kernelCeiling`` that is not a
    JSON object at all is a typo, and it must not read as "no kernel reading
    this beat" (that would leave the record silently describing the old
    container).
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())
    asyncio.run(_enroll_node(app, kernel_ceiling=KERNEL_CEILING))

    resp = asyncio.run(_heartbeat_node(app, {"kernelCeiling": []}))

    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": "kernelCeiling must be a JSON object",
    }
    # The refusal is the whole beat's: the kernel reading is not touched.
    record = app.state.nodes.get(NODE_ID)
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096


def test_an_old_workers_first_registration_stores_only_its_kernel_reading(
    workspace,
) -> None:
    """The five-key report is read on the **register** path as well.

    An older worker's *first* beat **is** its registration -- there is no
    earlier heartbeat for the new ``kernelCeiling`` key to have arrived on --
    so the register path has to accept that shape, and read it the same way the
    heartbeat path does: the kernel pair is the reading, the policy trio is
    another process's opinion about a number this control plane owns and is
    dropped (a ``0`` there would otherwise read as "one sandbox may take
    everything"). The answer is the hand-down, exactly as for a new worker.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())

    registered = asyncio.run(
        _register_node(
            app,
            body={
                "nodeID": NODE_ID,
                "address": NODE_ENDPOINT.address,
                "totalMemoryMB": 1024,
                "totalCPUPercent": 100,
                "totalDiskMB": 1024,
                "totalProcesses": 64,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
                "sandboxCeiling": {
                    "cpuPercent": 0,
                    "memoryMB": 65536,
                    "processes": 4096,
                    "kernelCpuPercent": 400,
                    "kernelMemoryMB": 4096,
                },
            },
        )
    )

    assert registered.status_code == 200
    assert registered.json() == {"nodeID": NODE_ID, "sandboxCeiling": HAND_DOWN}
    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256
    assert record.kernel_cpu_percent == 400
    assert record.kernel_memory_mb == 4096


def test_a_malformed_kernel_reading_in_the_old_shape_names_that_spelling(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """The tolerance is the same read through the old five-field report.

    The two shapes are one meaning, and the *message* has to point at the
    worker's own spelling: "sandboxCeiling.kernelCpuPercent" is what an
    operator running an older worker has in front of them. The bad dimension
    is dropped (this is the node's **first** beat, so there is nothing stored
    to keep) and one WARN names it; the policy trio in the same body still
    grants nothing.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent, settings=_ceiling_settings())

    with caplog.at_level(logging.WARNING, logger="control_plane.api.internal"):
        registered = asyncio.run(
            _register_node(
                app,
                body={
                    "nodeID": NODE_ID,
                    "address": NODE_ENDPOINT.address,
                    "totalMemoryMB": 1024,
                    "totalCPUPercent": 100,
                    "totalDiskMB": 1024,
                    "totalProcesses": 64,
                    "workerUID": WORKER_UID,
                    "workerGID": WORKER_GID,
                    "sandboxCeiling": {
                        "cpuPercent": 0,
                        "memoryMB": 65536,
                        "processes": 4096,
                        "kernelCpuPercent": 0,
                        "kernelMemoryMB": 4096,
                    },
                },
            )
        )

    assert registered.status_code == 200
    assert registered.json() == {"nodeID": NODE_ID, "sandboxCeiling": HAND_DOWN}
    record = app.state.nodes.get(NODE_ID)
    # The policy trio in the same body is still the worker's opinion, not a
    # grant: the record carries the control plane's own numbers.
    assert record.sandbox_cpu_percent_max == 200
    assert record.sandbox_memory_mb_max == 2048
    assert record.sandbox_processes_max == 256
    assert record.kernel_cpu_percent is None
    assert record.kernel_memory_mb == 4096
    assert _warnings_from(caplog, "control_plane.api.internal") == [
        "internal API: sandboxCeiling.kernelCpuPercent is not a positive "
        "integer or null (0); leaving the node's stored kernel reading for "
        "that dimension alone",
    ]


# ------------- N83 phase 2 (Task 5): the kernel's per-sandbox event counters
#
# ``sbx_<id>/memory.events`` counts the SIGKILLs (``oom_kill`` /
# ``oom_group_kill``) and ``sbx_<id>/pids.events`` counts the ``EAGAIN``s
# (``max`` -- tasks, threads included). The worker ships them per sandbox on
# every heartbeat (``sandboxEvents``); the control plane stores the maximum it
# has ever seen per sandbox and counter, and one WARN per growth names the
# sandbox and the counter. Review Focus §4: the sandbox record still says
# ``running`` after a kernel kill, so this line is the only thing standing
# between the operator and "the process mysteriously disappeared".

#: One worker's report: two kills in ``alpha`` and three task-creation
#: refusals in a second box -- the kernel's own counter names, verbatim
#: (``pids_max`` is ``pids.events``'s ``max`` line, prefixed by its file).
SANDBOX_EVENTS = {"alpha": {"oom_kill": 2, "oom_group_kill": 1, "pids_max": 3}}


def _warnings_from(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[str]:
    """The WARN lines one module emitted, in order (the app logs its own)."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name and record.levelno == logging.WARNING
    ]


def test_the_heartbeat_carries_the_event_counters_into_the_node_record(
    workspace,
) -> None:
    """The counters land on the node record and are exposed by the node view."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))

    resp = asyncio.run(_heartbeat_node(app, {"sandboxEvents": SANDBOX_EVENTS}))
    assert resp.status_code == 200

    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_events == SANDBOX_EVENTS

    view = asyncio.run(_internal_node_view(app))
    assert view.status_code == 200
    (node,) = [n for n in view.json() if n["nodeID"] == NODE_ID]
    assert node["sandboxEvents"] == SANDBOX_EVENTS


def test_a_heartbeat_without_events_leaves_the_record_alone(workspace) -> None:
    """An older worker during a rollout reports nothing -- never erase."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))
    assert (
        asyncio.run(_heartbeat_node(app, {"sandboxEvents": SANDBOX_EVENTS})).status_code
        == 200
    )

    assert asyncio.run(_heartbeat_node(app, {"diskUsedMB": 4096})).status_code == 200

    assert app.state.nodes.get(NODE_ID).sandbox_events == SANDBOX_EVENTS


def test_a_growing_counter_logs_one_named_warn_per_growth(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """The WARN names the sandbox *and* the counter, and only a growth warns."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))

    def _warns() -> list[str]:
        return _warnings_from(caplog, "control_plane.registry.nodes")

    with caplog.at_level(logging.WARNING, logger="control_plane.registry.nodes"):
        # 0 -> 1 is the event: the kernel killed a sandbox in this box.
        assert (
            asyncio.run(
                _heartbeat_node(app, {"sandboxEvents": {"alpha": {"oom_kill": 1}}})
            ).status_code
            == 200
        )
        assert _warns() == [
            "sandbox alpha on node node_a: oom_kill grew from 0 to 1 -- the "
            "kernel's own account of this sandbox hitting its cgroup wall "
            "(memory.events/pids.events; the sandbox record may still read "
            "'running')"
        ]

        # The same number again is not a new event: no second line.
        assert (
            asyncio.run(
                _heartbeat_node(app, {"sandboxEvents": {"alpha": {"oom_kill": 1}}})
            ).status_code
            == 200
        )
        assert len(_warns()) == 1

        # ... and the pids wall is named the same way, by its own counter.
        assert (
            asyncio.run(
                _heartbeat_node(
                    app,
                    {"sandboxEvents": {"beta": {"pids_max": 4}}},
                )
            ).status_code
            == 200
        )
        assert _warns()[1:] == [
            "sandbox beta on node node_a: pids_max grew from 0 to 4 -- the "
            "kernel's own account of this sandbox hitting its cgroup wall "
            "(memory.events/pids.events; the sandbox record may still read "
            "'running')"
        ]

    record = app.state.nodes.get(NODE_ID)
    assert record.sandbox_events == {
        "alpha": {"oom_kill": 1},
        "beta": {"pids_max": 4},
    }


def test_a_smaller_counter_never_erases_what_was_seen(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """The counters only ever move forward on the record.

    A smaller report is not a refusal and not a new event: this record is
    simply newer than the worker that reports it (a rollout, or a restarted
    worker whose cgroup subtree is fresh). It cannot lower what the kernel
    already said, and it must not fail the heartbeat -- a node that fails
    heartbeats has its live sandboxes orphaned.
    """
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))

    with caplog.at_level(logging.WARNING, logger="control_plane.registry.nodes"):
        assert (
            asyncio.run(
                _heartbeat_node(app, {"sandboxEvents": {"alpha": {"oom_kill": 5}}})
            ).status_code
            == 200
        )
        warns = len(_warnings_from(caplog, "control_plane.registry.nodes"))
        assert (
            asyncio.run(
                _heartbeat_node(app, {"sandboxEvents": {"alpha": {"oom_kill": 2}}})
            ).status_code
            == 200
        )
        assert len(_warnings_from(caplog, "control_plane.registry.nodes")) == warns

    assert app.state.nodes.get(NODE_ID).sandbox_events == {"alpha": {"oom_kill": 5}}


def test_a_malformed_counter_entry_does_not_fail_the_heartbeat(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """Unlike the ceiling, this section grants nothing: a garbled entry is
    dropped by name, and the entries beside it still land."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))

    with caplog.at_level(logging.WARNING):
        resp = asyncio.run(
            _heartbeat_node(
                app,
                {
                    "sandboxEvents": {
                        "alpha": {"oom_kill": -1},
                        "beta": {"oom_kill": "2"},
                        "gamma": {"oom_kill": 7},
                    }
                },
            )
        )

    assert resp.status_code == 200
    assert _warnings_from(caplog, "control_plane.api.internal") == [
        "internal API: sandboxEvents[alpha].oom_kill is not a non-negative "
        "integer (-1); ignoring this counter report",
        "internal API: sandboxEvents[beta].oom_kill is not a non-negative "
        "integer ('2'); ignoring this counter report",
    ]
    assert app.state.nodes.get(NODE_ID).sandbox_events == {"gamma": {"oom_kill": 7}}


def test_a_non_object_sandbox_events_section_is_refused_by_name(workspace) -> None:
    """A section of the wrong *shape* is still refused by name, like
    ``sandboxCeiling``: a typo must not read as "no sandbox hit a wall"."""
    agent = _StubDelegateClient()
    app = _delegation_app(workspace, client=agent)
    asyncio.run(_enroll_node(app))

    resp = asyncio.run(_heartbeat_node(app, {"sandboxEvents": []}))

    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": "sandboxEvents must be a JSON object",
    }
