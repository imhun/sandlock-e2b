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


def test_the_networkpolicy_names_the_control_plane_as_the_only_ingress() -> None:
    """The connection layer of hard rule 5: `worker <-> agent` does not exist.

    A single ingress rule, from exactly the control-plane pods, on exactly the
    agent's port. `policyTypes: [Ingress]` only: the agent's own egress (none
    today -- it reads `/proc` and writes a uid map) is not what this rule is
    about.
    """
    policy = _only(_load_all(AGENT_MANIFEST), "NetworkPolicy", "e2b-c3-agent")
    assert policy["spec"]["podSelector"]["matchLabels"] == AGENT_LABEL
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [
        {
            "from": [{"podSelector": {"matchLabels": CONTROL_PLANE_LABEL}}],
            "ports": [{"protocol": "TCP", "port": AGENT_PORT}],
        }
    ]
    # No wildcard rule: an empty `from` (or an empty `podSelector`) would allow
    # every pod in the namespace, which is the whole property being pinned.
    for rule in policy["spec"]["ingress"]:
        assert rule["from"]
        assert all(
            entry.get("podSelector", {}).get("matchLabels") for entry in rule["from"]
        )


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
    agent fails every slot start by name. The gate is therefore broker -> agent
    -> worker (line order, like the broker-before-worker contract in
    `tests/unit/test_worker_manifest_permissions.py`).
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

    broker = _rollout("ds/e2b-priv-broker")
    agent = _rollout("ds/e2b-c3-agent")
    worker = _rollout("statefulset/e2b-worker")
    assert apply_line < broker < agent < worker


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
    assert env["E2B_C3_AGENT_MAX_CONCURRENCY"]["value"] == "64"
    assert env["E2B_C3_AGENT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "e2b-secrets",
        "key": "E2B_C3_AGENT_TOKEN",
    }


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
    """D14: one agent per host; the multinode stack's three workers share it.

    Compose has no pods, so the two faces are two services. The agent's
    self-identity is the name the control plane dials it by (`c3-agent`), and
    the control plane's URL host must be that same name -- the pair is the
    compose half of D12.
    """
    for path in (COMPOSE_PROD, COMPOSE_MULTINODE):
        compose = _compose(path)
        services = compose["services"]
        assert "c3-agent" in services, path.name
        face_a = services["c3-agent"]
        assert face_a["image"] == "${AGENT_IMAGE:-e2b-sandlock-agent:0.1.0}"
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


def test_the_compose_control_plane_dials_the_agent_by_service_name() -> None:
    """D14: the CP is pointed at the agent service name, token and all."""
    for path in (COMPOSE_PROD, COMPOSE_MULTINODE):
        compose = _compose(path)
        control_plane = compose["services"]["control-plane"]
        env = _compose_env(control_plane)
        url = env["E2B_C3_AGENT_URL"]
        assert url == "${E2B_C3_AGENT_URL:-http://c3-agent:49985}"
        # The identity the agent checks is the URL host: both halves are one
        # name, so an instruction can never be addressed to a host the agent
        # does not believe it is.
        agent_env = _compose_env(compose["services"]["c3-agent"])
        assert urlsplit(url.split(":-", 1)[1].rstrip("}")).hostname == (
            agent_env["E2B_C3_AGENT_NODE_ID"]
        )
        assert "E2B_C3_AGENT_TOKEN" in env
        # `${E2B_C3_AGENT_MAX_CONCURRENCY:-64}`: the example's default is the
        # same factory default the k8s control plane names explicitly.
        assert env["E2B_C3_AGENT_MAX_CONCURRENCY"] == (
            "${E2B_C3_AGENT_MAX_CONCURRENCY:-64}"
        )


def test_no_compose_worker_receives_the_agent_token_or_the_agent_identity() -> None:
    """Hard rule 5, manifest layer: the worker holds no agent credential."""
    for path in (COMPOSE_PROD, COMPOSE_MULTINODE):
        services = _compose(path)["services"]
        workers = [name for name in services if name.startswith("worker")]
        assert workers, path.name
        for name in workers:
            env = _compose_env(services[name])
            assert "E2B_C3_AGENT_TOKEN" not in env, (path.name, name)
            assert "E2B_C3_AGENT_URL" not in env, (path.name, name)
            assert env["E2B_SLOT_IDENTITY"] == "agent-grant", (path.name, name)


def test_the_local_shapes_are_untouched() -> None:
    """Global Constraints: `local://` is out of C3's scope."""
    for path in LOCAL_SHAPES:
        text = path.read_text(encoding="utf-8")
        assert "c3-agent" not in text, path.name
        assert "E2B_SLOT_IDENTITY" not in text, path.name
        assert "E2B_C3_AGENT_TOKEN" not in text, path.name


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


def test_the_worker_image_still_carries_both_binaries_at_this_point() -> None:
    """Task 4 owns the removal; pinning it here keeps that change deliberate.

    Until Task 4 the worker image keeps `/var/lib/e2b-priv/{e2b-slot-spawn,
    e2b-maint}` with their file capabilities -- the C1 shape, which the
    `spawn` fallback still uses.
    """
    envd = (DEPLOY / "docker" / "Dockerfile.envd").read_text(encoding="utf-8")
    assert (
        "COPY --from=builder /tmp/priv/e2b-slot-spawn /tmp/priv/e2b-maint "
        "/var/lib/e2b-priv/" in envd
    )
    assert "setcap cap_setuid,cap_setgid+ep /var/lib/e2b-priv/e2b-slot-spawn" in envd
    assert "setcap cap_chown,cap_dac_override+ep /var/lib/e2b-priv/e2b-maint" in envd


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
