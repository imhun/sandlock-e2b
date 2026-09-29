"""C3 Task 3 slice B: the deployment surface of the per-node agent.

`deploy/c3_agent/` ships the *service*; this file pins the two deployment
shapes that run it (`deploy/k8s/c3-agent.yaml` and the two separated compose
stacks), because every property the plan relies on here is a *manifest*
property -- the pod's `hostPID`, which container gets which capability, and
which pods are allowed to reach the agent at all. None of them is observable
from the service's own tests, and each of them fails silently when it drifts:

* face A needs `hostPID` (to see the worker's pids) and the container BND
  `SETUID`/`SETGID` (a **bounding** declaration: the file capabilities on
  `as_uid` are refused at `exec` unless they are a subset of it). A
  privilege-escalation block would make the kernel ignore those file
  capabilities outright and the identity grant would fail *silently*
  (`plan §2.3`, 判据 11/12);
* face B is C1's file-operation face, moved: uid 0 (NFS AUTH_SYS only honours
  root for `chown`) with exactly C1's three capabilities (`plan §2.2`);
* the agent must be reachable from exactly one place. `worker <-> agent` does
  not exist (hard rule 5), and the NetworkPolicy is the *connection-layer*
  half of that: the service's token check is a second half, not a substitute.

The forbidden set (`SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/host network/privileged,
and never an escalation block) is asserted on the raw manifest *text*, so a
comment cannot whisper a capability in either.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
K8S = DEPLOY / "k8s"
AGENT_MANIFEST = K8S / "c3-agent.yaml"
WORKER_MANIFEST = K8S / "worker.yaml"
CONTROL_PLANE_MANIFEST = K8S / "control-plane.yaml"
KUSTOMIZATION = K8S / "kustomization.yaml"

COMPOSE_PROD = DEPLOY / "compose" / "docker-compose.prod.yml"
COMPOSE_MULTINODE = DEPLOY / "compose" / "docker-compose.multinode.yml"
#: In C3's coverage by ruling D17: the target host's stack relies on the
#: privileged binary too, so leaving it out would break it silently.
COMPOSE_STACK = DEPLOY / "stack" / "docker-compose.prod.yml"
COMPOSE_STACKS = (COMPOSE_PROD, COMPOSE_MULTINODE, COMPOSE_STACK)
#: The shapes C3 deliberately does *not* cover: the single-machine example and
#: the autoscaler's local pool. `local://` is out of scope (Global
#: Constraints), so the agent must not leak into them.
LOCAL_SHAPES = (
    DEPLOY / "compose" / "docker-compose.yml",
    DEPLOY / "compose" / "docker-compose.autoscale.yml",
)

#: The agent's pod label. It is also the resolver's lookup key
#: (`E2B_C3_AGENT_LABEL`), so it must exist on exactly one workload and never
#: on a worker pod.
AGENT_LABEL = {"app": "c3-agent"}
CONTROL_PLANE_LABEL = {"app": "control-plane"}
AGENT_PORT = 49985
#: D22: face B's own port. The two faces are two processes (uid 65534 for the
#: `uid_map` owner rule; uid 0 for NFS AUTH_SYS `chown`), so they are two
#: listeners -- a shared address would collide on the pod netns.
AGENT_MAINT_PORT = 49986
AGENT_IMAGE = "registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-agent:0.1.0"

#: The plan's forbidden set, verbatim (`plan §2.3`). Checked against the raw
#: text so a comment cannot smuggle one in.
FORBIDDEN_TOKENS = ("SYS_ADMIN", "SYS_PTRACE", "NET_RAW", "privileged")


def _load_all(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _only(docs: list[dict], kind: str, name: str | None = None) -> dict:
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind
        and (name is None or doc["metadata"]["name"] == name)
    ]
    assert len(matches) == 1, (kind, name, [d["metadata"]["name"] for d in matches])
    return matches[0]


def _containers(workload: dict) -> dict[str, dict]:
    spec = workload["spec"]["template"]["spec"]
    return {c["name"]: c for c in spec["containers"]}


def _pod_containers(workload: dict) -> dict[str, dict]:
    """Every container in the pod -- the regular ones *and* the inits.

    The inits are where the review's widening hid (`workspace-root-init` kept
    the runtime default capability set because a pin read only `containers`).
    """
    spec = workload["spec"]["template"]["spec"]
    return {
        c["name"]: c
        for c in [*spec["containers"], *(spec.get("initContainers") or [])]
    }


def _pod_spec(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]


def _env(container: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in (container.get("env") or [])}


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _compose_env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return {k: v for k, v in (e.split("=", 1) for e in env if "=" in e)}
    return env


# --------------------------------------------------------- the k8s DaemonSet


def test_the_agent_is_one_daemonset_with_two_containers_and_a_pod_scoped_host_pid() -> None:
    """D13: one DaemonSet, two faces, `hostPID` at the pod level.

    `hostPID` is a pod-level field, so face B gets it too -- the plan names
    that explicitly rather than letting it happen quietly (`plan §2.0`).
    """
    docs = _load_all(AGENT_MANIFEST)
    kinds = sorted(doc["kind"] for doc in docs)
    assert kinds == ["DaemonSet", "NetworkPolicy"]
    agent = _only(docs, "DaemonSet", "e2b-c3-agent")
    assert agent["metadata"]["labels"] == AGENT_LABEL
    assert agent["spec"]["selector"]["matchLabels"] == AGENT_LABEL
    assert agent["spec"]["template"]["metadata"]["labels"] == AGENT_LABEL
    pod = _pod_spec(agent)
    assert pod["hostPID"] is True
    assert sorted(_containers(agent)) == ["agent", "maint"]


def test_face_a_is_the_unprivileged_identity_giver() -> None:
    """判据 12 + §2.1: uid 65534, BND `SETUID`/`SETGID`, no other cap.

    The bounding set is what makes the file capabilities on `as_uid`
    executable at all; the process itself still holds no effective
    capability. The node identity is the *host's* name (`spec.nodeName`),
    which is D12: with two worker pods on one node a worker-pod-name identity
    would be ambiguous.
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    face_a = _containers(agent)["agent"]
    assert face_a["image"] == AGENT_IMAGE
    security = face_a["securityContext"]
    assert security["runAsUser"] == 65534
    assert security["capabilities"] == {"add": ["SETUID", "SETGID"]}
    assert "allowPrivilegeEscalation" not in security
    env = _env(face_a)
    assert env["E2B_C3_AGENT_NODE_ID"] == {
        "name": "E2B_C3_AGENT_NODE_ID",
        "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
    }
    assert env["E2B_C3_AGENT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "e2b-secrets",
        "key": "E2B_C3_AGENT_TOKEN",
    }


def test_face_b_is_the_file_face_with_c1s_capability_set() -> None:
    """§2.2: root + `drop: [ALL]` + exactly C1's three capabilities.

    Same three verbs, same three capabilities, same roots: the plan moves the
    file face, it does not widen it.
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    face_b = _containers(agent)["maint"]
    assert face_b["image"] == AGENT_IMAGE
    security = face_b["securityContext"]
    assert security["runAsUser"] == 0
    assert security["capabilities"] == {
        "drop": ["ALL"],
        "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
    }
    # The four whitelist roots the C side validates against (`priv_common.c`).
    env = _env(face_b)
    assert env["E2B_WORKSPACE_BASE"]["value"] == "/var/lib/e2b-sandboxes/workspaces"
    assert env["E2B_STATE_BASE"]["value"] == "/var/lib/e2b-sandboxes/state"
    assert env["E2B_SHARED_VOLUME_ROOT"]["value"] == "/var/lib/e2b-sandboxes"
    assert env["E2B_IMAGE_CACHE_DIR"]["value"] == "/var/lib/e2b-images"
    # Task 4 slice B: the payload. Face B runs **the same service** face A runs
    # (one app, one op table) and holds every input `priv_common.c` reads: the
    # two whitelist roots above plus the uid pool `--uid` is checked against
    # (the k8s worker follows the code default).
    assert "command" not in face_b
    assert env["E2B_UID_POOL_START"]["value"] == "10000"
    assert env["E2B_UID_POOL_SIZE"]["value"] == "1000"
    # D12: the same host identity face A carries -- every instruction's URL
    # path says which host it is for, and each face answers only for itself.
    assert env["E2B_C3_AGENT_NODE_ID"] == {
        "name": "E2B_C3_AGENT_NODE_ID",
        "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
    }
    assert env["E2B_C3_AGENT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "e2b-secrets",
        "key": "E2B_C3_AGENT_TOKEN",
    }
    # D22: face B's own port -- the two containers share the pod netns, so a
    # second listener on face A's 49985 would be `EADDRINUSE`.
    assert env["E2B_C3_AGENT_PORT"]["value"] == "49986"
    assert face_b["ports"] == [{"containerPort": 49986}]
    mounts = {m["name"]: m["mountPath"] for m in face_b["volumeMounts"]}
    assert mounts == {
        "shared": "/var/lib/e2b-sandboxes",
        "image-cache": "/var/lib/e2b-images",
    }


def test_face_a_carries_no_mounts_and_the_pod_ships_only_the_two_it_needs() -> None:
    """§2.1: face A shares no path with the worker -- that channel is absent.

    The agent's mounts are the shared PVC and the node-local cache (both for
    face B); face A reads `/proc` of the host through `hostPID` and needs
    nothing else.
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    face_a = _containers(agent)["agent"]
    assert face_a.get("volumeMounts") in (None, [])
    volumes = {v["name"]: v for v in _pod_spec(agent)["volumes"]}
    assert sorted(volumes) == ["image-cache", "shared"]
    assert volumes["shared"]["persistentVolumeClaim"]["claimName"] == "sandbox-shared"


def test_the_agent_manifest_never_names_a_forbidden_privilege() -> None:
    """判据 5 + 11: nothing forbidden, and no escalation block at all.

    The escalation block is the one that fails *silently*: NNP=1 makes the
    kernel ignore file capabilities, so face A would look installed and never
    grant anything. The text assertions below therefore exclude the words even
    inside comments.
    """
    text = AGENT_MANIFEST.read_text(encoding="utf-8")
    for token in FORBIDDEN_TOKENS:
        assert token not in text, token
    assert "hostNetwork" not in text
    # Not `false`, not `true`: the field must not exist anywhere.
    assert "allowPrivilegeEscalation" not in text
    assert "no-new-privileges" not in text
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    pod = _pod_spec(agent)
    assert pod.get("hostNetwork") is None
    for container in pod["containers"]:
        assert "allowPrivilegeEscalation" not in container["securityContext"]
        assert not container["securityContext"].get("privileged")


def test_the_manifest_set_ships_no_priv_broker_any_more() -> None:
    """C3 Task 7: C1's per-node broker is retired, and nothing may revive it.

    Three assertions, because each catches a different half:

    * the manifest is **deleted** (a leftover file is one `kubectl apply -f`
      away from a DaemonSet nobody expects);
    * the baseline's `resources:` list no longer pulls one in;
    * the *rendered* set -- what `deploy/k8s-k0s/apply.sh` actually applies --
      contains no object called `e2b-priv-broker`, whichever kind it grew into.

    Comments that *mention* the retired DaemonSet (history, the storage-init
    move) are deliberately allowed: they are prose, not a resource.
    """
    assert not (K8S / "priv-broker.yaml").exists()
    assert "- priv-broker.yaml" not in KUSTOMIZATION.read_text(encoding="utf-8")
    if shutil.which("kubectl") is None:
        return
    for manifests in ("deploy/k8s", "deploy/k8s-k0s"):
        rendered = subprocess.run(
            ["kubectl", "kustomize", str(REPO / manifests)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert rendered.returncode == 0, rendered.stderr
        names = [
            (doc.get("kind"), doc.get("metadata", {}).get("name"))
            for doc in yaml.safe_load_all(rendered.stdout)
            if isinstance(doc, dict)
        ]
        assert ("DaemonSet", "e2b-priv-broker") not in names, manifests
        assert not [name for _kind, name in names if name == "e2b-priv-broker"], manifests


#: The file face's capability set, verbatim (face B = C1's broker, moved). Every
#: container in the agent pod that still runs as uid 0 must carry exactly this:
#: `drop: [ALL]` first, so the runtime's default set (which contains `NET_RAW`)
#: is not inherited, then the three verbs the file scripts actually use.
ROOT_FILE_FACE_CAPS = {
    "drop": ["ALL"],
    "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
}


def test_the_worker_and_every_agent_container_stay_outside_the_forbidden_set() -> None:
    """判据 5 + 11 on the worker and on **every** container in the agent pod.

    §2.3's forbidden list, verbatim, and the escalation flag has to be
    **absent** -- not `false`. `allowPrivilegeEscalation: false` sets NNP=1,
    the kernel then ignores `as_uid`'s file capabilities, and face A stops
    granting identities *silently* (the failure looks like a missing
    capability at the first slot start, not like a mis-set flag).

    Review round 2 (item 1): this pin used to read only the worker and face A,
    so the two root inits (`storage-init`, moved in Task 5, and
    `workspace-root-init`, moved verbatim from the retired broker in Task 7)
    were outside it -- and `workspace-root-init` was shipping `runAsUser: 0`
    with **no** `capabilities:` block, i.e. the runtime default BND. It now
    reads the initContainers too, and every uid-0 container has to declare the
    file face's set rather than lean on the runtime default.
    """
    worker = _containers(
        _only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker")
    )["worker"]
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    containers = _pod_containers(agent)
    # The pod is exactly the two faces and the two root inits -- if a third
    # init or a sidecar appears, the coverage below has to grow with it.
    assert sorted(containers) == [
        "agent",
        "maint",
        "storage-init",
        "workspace-root-init",
    ]
    for name, container in [("worker", worker), *containers.items()]:
        text = json.dumps(container)
        for token in FORBIDDEN_TOKENS:
            assert token not in text, (name, token)
        assert "hostNetwork" not in text, name
        security = container["securityContext"]
        assert "allowPrivilegeEscalation" not in security, name
        assert not security.get("privileged"), name
        if security.get("runAsUser") == 0:
            assert security.get("capabilities") == ROOT_FILE_FACE_CAPS, name


def test_the_retired_socket_rollback_lever_is_gone() -> None:
    """Task 7: `E2B_PRIV_HELPER_TRANSPORT=socket` is not a shape any more.

    The environment variable that named the broker's socket is not declared
    anywhere, and the only transport the shipped worker pins is `agent`. The
    transport *switch* itself stays -- it is what selects the agent shape --
    but `socket` is no longer a value the worker accepts
    (`envd_service/priv_helpers.py::TRANSPORTS`), which its own unit lane
    pins.
    """
    if shutil.which("kubectl") is not None:
        # ...and in what is actually applied, so a value added in an overlay
        # patch (or a second, straggler manifest) cannot slip past the file.
        for manifests in ("deploy/k8s", "deploy/k8s-k0s"):
            rendered = subprocess.run(
                ["kubectl", "kustomize", str(REPO / manifests)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert rendered.returncode == 0, rendered.stderr
            workers = [
                doc
                for doc in yaml.safe_load_all(rendered.stdout)
                if isinstance(doc, dict)
                and doc.get("kind") == "StatefulSet"
                and doc.get("metadata", {}).get("name") == "e2b-worker"
            ]
            assert len(workers) == 1, manifests
            for container in workers[0]["spec"]["template"]["spec"]["containers"]:
                assert "E2B_PRIV_HELPER_SOCKET" not in json.dumps(container)
    worker = _containers(
        _only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker")
    )["worker"]
    assert _env(worker)["E2B_PRIV_HELPER_TRANSPORT"]["value"] == "agent"


def test_the_networkpolicy_names_the_control_plane_as_the_only_ingress() -> None:
    """The connection layer of hard rule 5: `worker <-> agent` does not exist.

    A single ingress rule, from exactly the control-plane pods, on exactly the
    agent's ports. `policyTypes` carries `Egress` as well since Task 6, because
    the agent gained **one** connection it initiates (the inventory report);
    that direction is pinned separately below, and this rule is still the whole
    ingress story.
    """
    policy = _only(_load_all(AGENT_MANIFEST), "NetworkPolicy", "e2b-c3-agent")
    assert policy["spec"]["podSelector"]["matchLabels"] == AGENT_LABEL
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"] == [
        {
            "from": [{"podSelector": {"matchLabels": CONTROL_PLANE_LABEL}}],
            # D22: both faces, still from exactly one source. Two ports in one
            # rule (rather than two rules) is what keeps the source list the
            # thing that bounds who may connect.
            "ports": [
                {"protocol": "TCP", "port": AGENT_PORT},
                {"protocol": "TCP", "port": AGENT_MAINT_PORT},
            ],
        }
    ]
    # No wildcard rule: an empty `from` (or an empty `podSelector`) would allow
    # every pod in the namespace, which is the whole property being pinned.
    for rule in policy["spec"]["ingress"]:
        assert rule["from"]
        assert all(
            entry.get("podSelector", {}).get("matchLabels") for entry in rule["from"]
        )


def test_the_agents_new_egress_is_one_narrow_named_rule() -> None:
    """Task 6 flips "the agent makes no connection" into one *named* one.

    The agent is the eyes: it reports the trees it can see to the control plane
    (``POST /internal/nodes/<host>/agent/inventory``). The property that must not
    quietly change is the *shape* of that new freedom, and it is two clauses:

    * the control-plane pods on 3000 (the Service ``E2B_CONTROL_PLANE_URL``
      points at), and
    * **cluster DNS** (UDP+TCP 53, ``kube-system``/``k8s-app: kube-dns``).

    The second one is load-bearing, not decoration: the URL carries a Service
    *name*, this pod has no ``hostAliases``/``dnsConfig``/``hostNetwork``, and
    k8s egress isolation drops everything not listed -- including the resolver.
    Drop the DNS clause and the feature is dead on any CNI that enforces the
    ingress half this design relies on (measured review finding: the scan would
    report ``the control plane ... is unreachable`` forever).

    A wildcard egress rule would be the silent regression this pin exists for;
    so would dropping the DNS clause, which is why the whole rule set is
    asserted verbatim rather than "contains the control plane".
    """
    policy = _only(_load_all(AGENT_MANIFEST), "NetworkPolicy", "e2b-c3-agent")
    assert policy["spec"]["egress"] == [
        {
            "to": [{"podSelector": {"matchLabels": CONTROL_PLANE_LABEL}}],
            "ports": [{"protocol": "TCP", "port": 3000}],
        },
        {
            # Both selectors in one entry: they AND together, so this names the
            # cluster's DNS pods, not "anything in kube-system" and not "any
            # pod labelled kube-dns".
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            # UDP is the query path; TCP is what a truncated answer retries over.
            "ports": [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ],
        }
    ]


def test_only_face_b_scans_the_workspaces() -> None:
    """The scan needs the shared mount; face A has none, so it must not scan.

    An agent that reported an empty inventory because it cannot see the
    workspaces would look exactly like an agent that sees no orphans -- so the
    knob lives on the one container that carries the mount, and the control
    plane's URL it reports to is the same name the service answers on.
    """
    agent = _only(_load_all(AGENT_MANIFEST), "DaemonSet", "e2b-c3-agent")
    containers = _containers(agent)
    face_a = _env(containers["agent"])
    face_b = _env(containers["maint"])
    assert "E2B_C3_AGENT_SCAN" not in face_a
    assert "E2B_CONTROL_PLANE_URL" not in face_a
    assert face_b["E2B_C3_AGENT_SCAN"]["value"] == "on"
    assert face_b["E2B_CONTROL_PLANE_URL"]["value"] == "http://control-plane:3000"
    # 30s + 120s ⇒ "worker crashed and never restarts" converges in 2–3 minutes.
    assert face_b["E2B_C3_AGENT_SCAN_INITIAL_DELAY_S"]["value"] == "30"
    assert face_b["E2B_C3_AGENT_SCAN_INTERVAL_S"]["value"] == "120"
    assert face_b["E2B_C3_AGENT_SCAN_BACKOFF_MAX_S"]["value"] == "600"


def test_the_agent_is_in_the_baseline_kustomization() -> None:
    """D13: the baseline carries it, so the k0s overlay inherits it unchanged."""
    resources = [
        line.strip()
        for line in KUSTOMIZATION.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- ")
    ]
    assert "- c3-agent.yaml" in resources


def test_the_k0s_apply_gate_converges_the_agent_before_the_worker() -> None:
    """The worker's new upstream has to be up before the worker rolls.

    `E2B_SLOT_IDENTITY=agent-grant` is fail-closed: a worker whose node has no
    agent fails every slot start by name. The gate is therefore agent ->
    worker, and **nothing else**: C1's broker-before-worker gate is retired
    (C3 Task 7), so a `ds/e2b-priv-broker` line here would be a write against
    a DaemonSet no manifest ships any more (it would fail, not no-op).
    """
    lines = [
        line.strip()
        for line in (DEPLOY / "k8s-k0s" / "apply.sh").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    apply_line = next(
        index
        for index, line in enumerate(lines)
        if line == "printf '%s\\n' \"$rendered\" | kubectl apply -f -"
    )

    def _rollout(target: str) -> int:
        matches = [
            index
            for index, line in enumerate(lines)
            if f"rollout status {target}" in line
        ]
        assert len(matches) == 1, (target, matches)
        return matches[0]

    assert not [
        line for line in lines if "rollout status ds/e2b-priv-broker" in line
    ], "the retired broker's rollout gate is still in apply.sh"
    agent = _rollout("ds/e2b-c3-agent")
    worker = _rollout("statefulset/e2b-worker")
    assert apply_line < agent < worker


def test_the_control_plane_role_may_read_and_list_pods() -> None:
    """D13: the resolver lists *agent* pods by label on the worker's node.

    `get` alone could answer "what is worker pod X's nodeName" but not "which
    agent pod runs on that node": that is a label-scoped `list`. The Role stays
    namespaced and reads pods and nothing else.
    """
    docs = _load_all(CONTROL_PLANE_MANIFEST)
    roles = [doc for doc in docs if doc.get("kind") == "Role"]
    assert len(roles) == 1
    assert roles[0]["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}
    ]


def test_the_control_plane_is_given_the_agent_channel_and_a_sized_limit() -> None:
    """D13/D16: the CP knows the agent's label/namespace and its own bound."""
    deployment = _only(_load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane")
    container = next(
        c for c in _containers(deployment).values() if c["name"] == "control-plane"
    )
    env = _env(container)
    assert env["E2B_C3_AGENT_LABEL"]["value"] == "app=c3-agent"
    assert env["E2B_C3_AGENT_NAMESPACE"]["value"] == "sandlock"
    # D22: face B's own port. The k8s lane derives the address from the same
    # worker-pod → node → agent-pod lookup face A uses, so this is the only
    # thing the manifest has to name.
    assert env["E2B_C3_AGENT_MAINT_PORT"]["value"] == "49986"
    assert env["E2B_C3_AGENT_MAX_CONCURRENCY"]["value"] == "64"
    assert env["E2B_C3_AGENT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "e2b-secrets",
        "key": "E2B_C3_AGENT_TOKEN",
    }


def test_the_k8s_control_plane_names_route_b_and_splits_the_cache() -> None:
    """Task 4 slice B's two CP-side values, both of which are file-op inputs.

    `scope-slot-document`'s path is derived by the *control plane* (the worker
    may not report one), so the CP has to know route B's scratch root: unset,
    that op answers a named 503 and no route-B slot ever comes up.

    The image cache is subtler. The worker writes each sandbox's secret under
    `<its own E2B_IMAGE_CACHE_DIR>/secrets/<id>/` -- the **node-local**
    `/var/lib/e2b-images` in this manifest set -- and `chown-secret` is derived
    from the control plane's own `E2B_IMAGE_CACHE_DIR`, so the two have to be
    spelled identically. But that same setting doubles as the OCI tar directory
    (`image_oci_dir or image_cache_dir`), and the tar has to stay on the shared
    volume every worker can read -- so the split is named explicitly instead of
    inherited.
    """
    deployment = _only(_load_all(CONTROL_PLANE_MANIFEST), "Deployment", "control-plane")
    env = _env(next(c for c in _containers(deployment).values()))
    assert env["E2B_ROUTE_B_TMP_ROOT"]["value"] == "/var/lib/e2b-sandboxes/state/.route-b"
    # ...and it is the same value the worker names, which is the whole point
    # (`worker.yaml` is the manifest that writes the documents).
    worker = _only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker")
    worker_env = _env(_containers(worker)["worker"])
    assert env["E2B_ROUTE_B_TMP_ROOT"]["value"] == worker_env["E2B_ROUTE_B_TMP_ROOT"]["value"]
    assert env["E2B_IMAGE_CACHE_DIR"]["value"] == worker_env["E2B_IMAGE_CACHE_DIR"]["value"]
    assert env["E2B_IMAGE_OCI_DIR"]["value"] == worker_env["E2B_IMAGE_OCI_DIR"]["value"]
    # ...and the split is real: the CP's cache is *not* its OCI directory here,
    # which is the property "pointing the cache at the worker's node-local
    # secrets root" would have silently broken.
    assert (
        env["E2B_IMAGE_CACHE_DIR"]["value"]
        != env["E2B_IMAGE_OCI_DIR"]["value"]
    )


def test_the_clients_concurrency_default_is_a_named_decision(monkeypatch) -> None:
    """D16: the factory default is part of the shipped shape, not a guess.

    ``0`` would mean "unbounded"; the shipped default is 64, and the reasoning
    is written where the value is (`control_plane/config.py`): at least the
    outstanding-creates any shape can present, at least the agent's own 40-slot
    handler pool, below the CP's 100-create admission cap.
    """
    monkeypatch.delenv("E2B_C3_AGENT_MAX_CONCURRENCY", raising=False)
    from control_plane.config import Settings

    assert Settings().c3_agent_max_concurrency == 64


# ------------------------------------------------------------- the worker side


def test_the_worker_is_on_the_agent_grant_path_and_carries_no_agent_secret() -> None:
    """§4 rule 2 + hard rule 5: the worker asks the CP, never the agent.

    `hostPID` on the worker is the trap this pins: it would put the slot's
    child in the host's pid namespace, and the agent's `NSpid` discriminator
    would stop distinguishing two workers (its chain would be one entry long).
    """
    worker = _only(_load_all(WORKER_MANIFEST), "StatefulSet", "e2b-worker")
    pod = _pod_spec(worker)
    assert "hostPID" not in pod
    container = _containers(worker)["worker"]
    env = _env(container)
    assert env["E2B_SLOT_IDENTITY"]["value"] == "agent-grant"
    # Task 4 slice B: the file steps are the agent's too. The two switches move
    # together with the binary removal -- a worker without the binaries and
    # still on the `spawn`/`exec` path is the broken intermediate state.
    assert env["E2B_PRIV_HELPER_TRANSPORT"]["value"] == "agent"
    # ...and the identity the control plane's trusted source reads (Task 4
    # slice A's D21 option 1) has to be pinned *in the pod spec*: the CP reads
    # `securityContext.runAsUser`/`runAsGroup` through the pod API, so the
    # image's `USER 65534:65534` alone would leave it answering "unknown".
    assert container["securityContext"]["runAsUser"] == 65534
    assert container["securityContext"]["runAsGroup"] == 65534
    # Review round 2 (item 4): the BND is declared empty with `drop: [ALL]`
    # rather than inherited from the runtime default (`CapBnd` was 0x…a80425fb).
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert "E2B_C3_AGENT_TOKEN" not in env
    assert "E2B_C3_AGENT_URL" not in env
    assert "E2B_C3_AGENT_TOKEN" not in WORKER_MANIFEST.read_text(encoding="utf-8")
    # The agent's label is the resolver's lookup key: a worker pod carrying it
    # would be listed as an agent.
    labels = worker["spec"]["template"]["metadata"]["labels"]
    assert labels != AGENT_LABEL
    assert labels.get("app") != "c3-agent"


def test_no_pod_other_than_the_agent_claims_the_agent_label() -> None:
    """A second pod with `app: c3-agent` would make the lookup ambiguous."""
    carriers: list[str] = []
    for path in sorted(K8S.glob("*.yaml")):
        for doc in _load_all(path):
            if doc.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
                continue
            labels = (doc["spec"]["template"].get("metadata") or {}).get("labels") or {}
            if labels.get("app") == "c3-agent":
                carriers.append(doc["metadata"]["name"])
    assert carriers == ["e2b-c3-agent"]


# ----------------------------------------------------------- the compose stacks


def test_each_compose_stack_runs_exactly_one_agent_facing_the_control_plane() -> None:
    """D14 + D22: one agent per host, two faces, each with its own listener.

    Compose has no pods, so the two faces are two services. The agent's
    self-identity is the name the control plane dials it by (`c3-agent`), and
    the control plane's URL host must be that same name -- the pair is the
    compose half of D12. Both faces carry that identity: the instruction's URL
    *path* names the host for either endpoint.
    """
    for path in COMPOSE_STACKS:
        compose = _compose(path)
        services = compose["services"]
        assert "c3-agent" in services, path.name
        face_a = services["c3-agent"]
        assert face_a["image"] in (
            "${AGENT_IMAGE:-e2b-sandlock-agent:0.1.0}",
            "${AGENT_IMAGE}",
        )
        # The host's pid table is what the reverse lookup reads.
        assert face_a["pid"] == "host"
        assert face_a["user"] == "65534:65534"
        assert face_a["cap_drop"] == ["ALL"]
        assert face_a["cap_add"] == ["SETUID", "SETGID"]
        env = _compose_env(face_a)
        assert env["E2B_C3_AGENT_NODE_ID"] == "c3-agent"
        assert "E2B_C3_AGENT_TOKEN" in env
        assert env["E2B_C3_AGENT_PORT"] == "49985"
        face_b = services["c3-agent-maint"]
        assert face_b["image"] == face_a["image"]
        assert face_b["user"] == "0:0"
        assert face_b["cap_drop"] == ["ALL"]
        assert face_b["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
        # Task 4 slice B: the payload is the same service, on its own port
        # (D22), with the four roots and the pool `priv_common.c` reads.
        assert "command" not in face_b, path.name
        assert face_b["pid"] == "host"
        b_env = _compose_env(face_b)
        assert b_env["E2B_C3_AGENT_NODE_ID"] == "c3-agent"
        assert b_env["E2B_C3_AGENT_TOKEN"] == env["E2B_C3_AGENT_TOKEN"]
        assert b_env["E2B_C3_AGENT_PORT"] == "49986"
        assert b_env["E2B_WORKSPACE_BASE"] == "/var/lib/e2b-sandboxes"
        assert b_env["E2B_SHARED_VOLUME_ROOT"] == "/var/lib/e2b-sandboxes"
        assert b_env["E2B_IMAGE_CACHE_DIR"] == "/var/lib/e2b-sandboxes/_images"
        # The pool has to cover every worker this one agent serves. The stack
        # is the one that ships two disjoint per-worker slices -- worker-1
        # 10000..10999, worker-2 11000..11999 -- so its agent's pool is the
        # union (start 10000, size 2000); the other two stacks leave the code
        # default (10000/1000).
        if path == COMPOSE_STACK:
            assert b_env["E2B_UID_POOL_START"] == "${E2B_UID_POOL_START:-10000}"
            assert b_env["E2B_UID_POOL_SIZE"] == (
                "${E2B_C3_AGENT_UID_POOL_SIZE:-2000}"
            )
        else:
            assert b_env["E2B_UID_POOL_START"] == "${E2B_UID_POOL_START:-10000}"
            assert b_env["E2B_UID_POOL_SIZE"] == "${E2B_UID_POOL_SIZE:-1000}"
        # Ruling D4's relaxation + **D25**: face B resolves the worker's own
        # uid/gid from files that are *world-readable* -- candidates matched by
        # the container id in ``/proc/<pid>/cgroup``, the identity read from
        # ``/proc/<pid>/status`` -- so it needs no ``CAP_SYS_PTRACE``, no
        # ``SETUID``/``SETGID`` and no matching ``user:``. What it does need is
        # the runtime's hostname: the anchor *is* "my hostname", so a stack that
        # overrides ``hostname:`` on a worker makes every file operation on that
        # node a named refusal ("reported no container id"). That is the whole
        # constraint, and it is pinned here -- the pin that used to couple a
        # worker's ``user:`` to the agent's resolver uid is gone with the
        # resolver's uid switch.
        workers = {
            name: service
            for name, service in services.items()
            if name.startswith("worker")
        }
        assert workers, path.name
        for name, service in workers.items():
            assert "hostname" not in service, (path.name, name)
        # C3 Task 6: the same split as k8s -- only the face that mounts the
        # workspaces scans, and it reports to the control plane's own name.
        assert "E2B_C3_AGENT_SCAN" not in env
        assert "E2B_CONTROL_PLANE_URL" not in env
        if path == COMPOSE_MULTINODE:
            # This stack has no Redis, i.e. no shared record store -- and leg (a)
            # of the staleness gate makes the sweep inert in that shape by
            # design. Enabling the scan here would defer on every round (log
            # noise that looks like self-healing), so the honest state is "off,
            # with the trigger written down in the manifest". The pin checks the
            # premise, not the spelling: no store, no scan.
            assert "redis" not in compose["services"]
            assert "E2B_REDIS_URL" not in _compose_env(compose["services"]["control-plane"])
            assert "E2B_C3_AGENT_SCAN" not in b_env
            assert "E2B_CONTROL_PLANE_URL" not in b_env
            continue
        assert b_env["E2B_C3_AGENT_SCAN"] == "on"
        assert b_env["E2B_CONTROL_PLANE_URL"] in (
            "http://control-plane:3000",
            "${E2B_CONTROL_PLANE_URL:-http://control-plane:3000}",
        )


def test_the_compose_control_plane_dials_the_agent_by_service_name() -> None:
    """D14 + D22: the CP is pointed at both service names, token and all."""
    for path in COMPOSE_STACKS:
        compose = _compose(path)
        control_plane = compose["services"]["control-plane"]
        env = _compose_env(control_plane)
        url = env["E2B_C3_AGENT_URL"]
        assert url == "${E2B_C3_AGENT_URL:-http://c3-agent:49985}", path.name
        # ...and the file verbs are addressed at the *face-B* service (D22):
        # a shared address could not serve both, and this is the variable the
        # client reads for `chown`/`rm`/`walk`.
        assert env["E2B_C3_AGENT_MAINT_URL"] == (
            "${E2B_C3_AGENT_MAINT_URL:-http://c3-agent-maint:49986}"
        ), path.name
        # The identity the agent checks is the URL host: both halves are one
        # name, so an instruction can never be addressed to a host the agent
        # does not believe it is.
        agent_env = _compose_env(compose["services"]["c3-agent"])
        assert urlsplit(url.split(":-", 1)[1].rstrip("}")).hostname == (
            agent_env["E2B_C3_AGENT_NODE_ID"]
        )
        # Both faces carry that same identity, so the path identity of a
        # file-op instruction is the same name a grant carries.
        maint_env = _compose_env(compose["services"]["c3-agent-maint"])
        assert maint_env["E2B_C3_AGENT_NODE_ID"] == agent_env["E2B_C3_AGENT_NODE_ID"]
        assert "E2B_C3_AGENT_TOKEN" in env
        # `${E2B_C3_AGENT_MAX_CONCURRENCY:-64}`: the example's default is the
        # same factory default the k8s control plane names explicitly.
        assert env["E2B_C3_AGENT_MAX_CONCURRENCY"] == (
            "${E2B_C3_AGENT_MAX_CONCURRENCY:-64}"
        )


def test_no_compose_worker_receives_the_agent_token_or_the_agent_identity() -> None:
    """Hard rule 5, manifest layer: the worker holds no agent credential."""
    for path in COMPOSE_STACKS:
        services = _compose(path)["services"]
        workers = [name for name in services if name.startswith("worker")]
        assert workers, path.name
        for name in workers:
            env = _compose_env(services[name])
            assert "E2B_C3_AGENT_TOKEN" not in env, (path.name, name)
            assert "E2B_C3_AGENT_URL" not in env, (path.name, name)
            assert "E2B_C3_AGENT_MAINT_URL" not in env, (path.name, name)
            assert env["E2B_SLOT_IDENTITY"] == "agent-grant", (path.name, name)
            # Task 4 slice B: the file steps moved to the agent in the same
            # change as the binary removal -- a worker without the binaries and
            # still on `auto` would silently degrade to the E5.1 shape.
            assert env["E2B_PRIV_HELPER_TRANSPORT"] == "agent", (path.name, name)


def test_the_local_shapes_are_untouched() -> None:
    """Global Constraints: `local://` is out of C3's scope."""
    for path in LOCAL_SHAPES:
        text = path.read_text(encoding="utf-8")
        assert "c3-agent" not in text, path.name
        assert "E2B_SLOT_IDENTITY" not in text, path.name
        assert "E2B_C3_AGENT_TOKEN" not in text, path.name


def test_the_shapes_excluded_from_c3_declare_that_they_have_no_file_ops() -> None:
    """D23: an excluded shape says so in its own manifest, not by silence.

    The single-machine example and the autoscaler's docker pool both relied on
    the worker image's file-capability binaries. Task 4 slice B removed them,
    so `E2B_PRIV_HELPERS=auto` would now resolve nothing, log a single warning
    and keep the in-process (E5.1) shape -- no per-sandbox host uid, no
    route-B. For the example that is also a *downgrade in loudness*: its
    default route-B root is outside the broker whitelist, so it used to refuse
    to start.

    Ruling D23: both are excluded from C3's coverage **by name** (like
    `local://`), and each has to *declare* its absent file-operation capability
    in its own manifest -- `off` is the value `E2B_PRIV_HELPERS` already has
    for "never use the brokers". A reader can then answer "what does this shape
    do for privileged file ops?" from the file alone.
    """
    # The single-machine example: a plain env key on its only worker.
    demo = _compose(LOCAL_SHAPES[0])["services"]["envd"]
    assert _compose_env(demo)["E2B_PRIV_HELPERS"] == "off"
    # The pool: the declaration lives in the JSON the autoscaler hands every
    # worker it spawns (the only place that shape's env is written).
    pool = _compose(LOCAL_SHAPES[1])["services"]["autoscaler"]
    assert json.loads(_compose_env(pool)["E2B_AS_WORKER_ENV"])["E2B_PRIV_HELPERS"] == "off"
    # ...and neither shape smuggles in a C3 key that would imply a privileged
    # path it does not have.
    for path in LOCAL_SHAPES:
        text = path.read_text(encoding="utf-8")
        for key in (
            "E2B_SLOT_IDENTITY",
            "E2B_PRIV_HELPER_TRANSPORT",
            "E2B_C3_AGENT_URL",
            "E2B_C3_AGENT_MAINT_URL",
            "E2B_C3_AGENT_TOKEN",
        ):
            assert key not in text, (path.name, key)


# --------------------------------------------------- the images (D15) and pins


def test_the_agent_image_is_built_by_the_repos_own_scripts() -> None:
    """D15/Task 1 minor ⑦: the image is referenced by the build flow."""
    assert (DEPLOY / "docker" / "Dockerfile.agent").is_file()
    build_images = (DEPLOY / "scripts" / "build-images.sh").read_text(encoding="utf-8")
    assert "docker/Dockerfile.agent" in build_images
    assert "$REGISTRY/e2b-sandlock-agent:$VERSION" in build_images
    # The DaemonSet names the same repository, so a build that renames the
    # image cannot leave the manifest pointing at an image nobody publishes.
    image_repo = AGENT_IMAGE.rsplit(":", 1)[0]
    assert image_repo.rsplit("/", 1)[1] == "e2b-sandlock-agent"


def test_the_worker_image_has_no_privileged_binary_and_the_agent_image_has_both() -> None:
    """判据 2/15, the deferred D1 pins: the two copies live in different images.

    Task 4 slice B is where this lands. The worker image must not contain
    `/var/lib/e2b-priv/` at all -- not the directory, not the two binaries, not
    even a `setcap` line (a `COPY --from` would not carry the capability xattr,
    so a leftover instruction would ship two unprivileged helpers that look
    installed). That is why the agent got its *own* Dockerfile in Task 1: with
    a shared image the property would be a statement about a build arg instead
    of about a file.

    Read as *directives*, not as substrings: the worker Dockerfile explains in
    a comment why the block is gone, and a comment must not be able to satisfy
    the pin in either direction.
    """
    envd_lines = [
        line.strip()
        for line in (DEPLOY / "docker" / "Dockerfile.envd")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.lstrip().startswith("#")
    ]
    worker_text = "\n".join(envd_lines)
    assert "e2b-slot-spawn" not in worker_text
    assert "e2b-maint" not in worker_text
    assert "/var/lib/e2b-priv" not in worker_text
    assert "setcap" not in worker_text
    assert "libcap2-bin" not in worker_text
    # The agent image is the one place they exist, with the same caps the
    # C1 broker and Task 1's as_uid shipped.
    agent_lines = [
        line.strip()
        for line in (DEPLOY / "docker" / "Dockerfile.agent")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.lstrip().startswith("#")
    ]
    agent_text = "\n".join(agent_lines)
    assert (
        "COPY --from=builder /tmp/priv/as_uid /tmp/priv/e2b-maint "
        "/var/lib/e2b-priv/" in agent_text
    )
    assert "setcap cap_setuid,cap_setgid+ep /var/lib/e2b-priv/as_uid" in agent_text
    assert "setcap cap_chown,cap_dac_override+ep /var/lib/e2b-priv/e2b-maint" in agent_text


def test_the_build_and_push_script_names_the_agent_image() -> None:
    """D15: the release flow builds and pushes it with the rest."""
    text = (DEPLOY / "scripts" / "build-and-push.sh").read_text(encoding="utf-8")
    # The naming convention line is the one place a reader looks for "which
    # images does this produce", so `agent` has to be in it -- that is what
    # makes the release flow's coverage of the new image assertable rather than
    # a claim in a report.
    assert (
        "e2b-sandlock-{control-plane-gateway,worker,agent,autoscaler,quota-agent}"
        in text
    )
