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

from pathlib import Path

import yaml

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
    runs. The current set is the two file-capability bounding caps (``SETUID``,
    ``SETGID``); Task 4 removes them, and this pin must then be re-narrowed --
    never silently widened.
    """
    docs = _load_all(K8S / "worker.yaml")
    statefulset = next(d for d in docs if d.get("kind") == "StatefulSet")
    caps = _container_capability_texts(statefulset["spec"]["template"]["spec"])
    assert "NET_RAW" not in caps
    for forbidden in FORBIDDEN_CAPABILITY_TOKENS:
        assert forbidden not in caps
    assert sorted(caps) == ["SETGID", "SETUID"]


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


def test_the_autoscaler_never_cap_adds_net_raw() -> None:
    """Workers the autoscaler launches must match the pinned compose shape."""
    source = (REPO / "autoscaler" / "backends" / "local.py").read_text(encoding="utf-8")
    assert "NET_RAW" not in source
    assert "SYS_PTRACE" not in source


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
        REPO / "deploy" / "compose" / "docker-compose.autoscale.yml",
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
