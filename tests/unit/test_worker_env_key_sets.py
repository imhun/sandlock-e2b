"""Every worker-shaped stack declares the fleet's worker env keys (N45).

`docs/open-issues.md` N45: the local pool's worker env never declared
`E2B_PID_NS`, so a pooled sandbox shared the worker's PID namespace --
`kill(1, 0)` answered `EPERM` for the worker's live PID 1, an existence oracle
(measured: `getpid=81`, `kill1=EPERM`; with the key: `getpid=7`, `kill1=ok`).
The fleet turns it on in both manifests (`deploy/k8s/worker.yaml`,
`deploy/stack/docker-compose.prod.yml`), and the ruling on N36/N38 was "pool ==
fleet" / "unify the shape".

The defect is not one key: it is that a *shape key* can be missing from a stack
without anything going red. So this file pins the whole key set of every stack
that hosts a sandbox-building worker against `deploy/k8s/worker.yaml`, the way
`tests/unit/test_compose_base_image_shape.py` pins every base-image
declaration:

* every k8s worker env key belongs to exactly one named class
  (`test_every_k8s_worker_key_is_classified`), so a new key in the manifest
  forces an explicit decision instead of being silently "expected to be
  missing" everywhere;
* each stack's *missing* and *extra* key sets are compared for exact equality
  against the union of the classes it is allowed to differ in -- a key that is
  neither present nor named is red, and a whitelist entry for a key the stack
  already has is red too;
* `E2B_PID_NS` itself is pinned on *and* equal to the fleet's value, read from
  the manifest rather than copied here.

Deliberate shape differences live in the named classes (each carries its
reason); they are also named in the N45 row of `docs/open-issues.md`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

FLEET_K8S = "deploy/k8s/worker.yaml"
FLEET_STACK = "deploy/stack/docker-compose.prod.yml"
POOL_COMPOSE = "deploy/compose/docker-compose.autoscale.yml"
POOL_BACKEND = "autoscaler/backends/local.py"
COMPOSE_PROD = "deploy/compose/docker-compose.prod.yml"
COMPOSE_MULTINODE = "deploy/compose/docker-compose.multinode.yml"
COMPOSE_DEMO = "deploy/compose/docker-compose.yml"
COMPOSE_RUNNER = "deploy/compose/docker-compose.test.yml"

_SERVICE_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):(?:\s*&[\w.-]+)?\s*$")
_ENV_RE = re.compile(r"^(\s+)environment:(?:\s*&[\w.-]+)?\s*$")
_KV_RE = re.compile(r"^(\s+)(E2B_[A-Z0-9_]+):\s*(.*)$")
_K8S_NAME_RE = re.compile(r"^\s+- name: (E2B_[A-Z0-9_]+)\s*$")
_INTERPOLATION_RE = re.compile(r"^\$\{[A-Z0-9_]+:-(.*)\}$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _compose_service_env(relative: str, service: str) -> dict[str, str]:
    """One compose service's `environment:` block, key -> raw value.

    Line-oriented, like the repo's other manifest pins: the block ends at the
    next non-comment line that is not indented deeper than the `environment:`
    key itself (comments and blank lines inside the block do not end it).
    """
    text = (REPO / relative).read_text(encoding="utf-8")
    header = f"  {service}:"
    lines = text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line == header or line.startswith(header + " ")
    ]
    assert len(starts) == 1, (relative, service, starts)
    block_indent: int | None = None
    env: dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        if _SERVICE_RE.match(line):
            break
        match = _ENV_RE.match(line)
        if match and block_indent is None:
            block_indent = len(match.group(1))
            continue
        if block_indent is None:
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if len(line) - len(line.lstrip()) <= block_indent:
                break
        match = _KV_RE.match(line)
        if match:
            env[match.group(2)] = _unquote(match.group(3))
    assert env, (relative, service)
    return env


def _pool_compose_env() -> dict[str, str]:
    """The `E2B_AS_WORKER_ENV` JSON the autoscaler hands each spawned worker."""
    text = (REPO / POOL_COMPOSE).read_text(encoding="utf-8")
    lines = [
        line for line in text.splitlines() if line.strip().startswith("E2B_AS_WORKER_ENV:")
    ]
    assert len(lines) == 1, lines
    payload = lines[0].split(":", 1)[1].strip()
    assert payload[0] == "'" and payload[-1] == "'", payload
    return json.loads(payload[1:-1])


def _pool_backend_env() -> dict[str, str]:
    """The literal `self._env` dict in the local Docker backend.

    This is the second place the pool declares the same shape (N38: the two
    drifted once already). Constructor parameters (`"E2B_NODE_MEMORY_MB":
    node_memory_mb`) are returned as the bare name; `_pool_declarations_agree`
    only compares the quoted literals, which is where the shape keys live.
    """
    text = (REPO / POOL_BACKEND).read_text(encoding="utf-8")
    start = text.index("self._env = {")
    end = text.index("\n        }", start)
    env: dict[str, str] = {}
    for line in text[start:end].splitlines():
        match = re.match(r'\s+"(E2B_[A-Z0-9_]+)":\s*(.+?),?\s*$', line)
        if match:
            env[match.group(1)] = match.group(2)
    assert env, POOL_BACKEND
    return env


def _k8s_worker_env() -> dict[str, str]:
    """`deploy/k8s/worker.yaml`'s `- name: E2B_*` entries.

    A `value:` literal is returned as written; an entry sourced from the pod
    (`valueFrom:`: `POD_IP`, the internal-key Secret) is returned as
    `"<valueFrom>"`, since this pin is about which keys exist and what a
    literal switch like `E2B_PID_NS` is set to.
    """
    lines = (REPO / FLEET_K8S).read_text(encoding="utf-8").splitlines()
    env: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = _K8S_NAME_RE.match(line)
        if not match:
            continue
        cursor = index + 1
        while cursor < len(lines) and (
            lines[cursor].strip() == "" or lines[cursor].strip().startswith("#")
        ):
            cursor += 1
        value_line = lines[cursor].strip()
        if value_line.startswith("value: "):
            env[match.group(1)] = _unquote(value_line[len("value: ") :])
        else:
            assert value_line.startswith("valueFrom:"), (match.group(1), value_line)
            env[match.group(1)] = "<valueFrom>"
    return env


def _worker_envs() -> dict[str, dict[str, str]]:
    """Every stack's worker env, plus the fleet's own two declarations."""
    return {
        FLEET_STACK: _compose_service_env(FLEET_STACK, "worker-1"),
        POOL_COMPOSE: _pool_compose_env(),
        POOL_BACKEND: _pool_backend_env(),
        COMPOSE_PROD: _compose_service_env(COMPOSE_PROD, "worker-1"),
        COMPOSE_MULTINODE: _compose_service_env(COMPOSE_MULTINODE, "worker-1"),
        COMPOSE_DEMO: _compose_service_env(COMPOSE_DEMO, "envd"),
        COMPOSE_RUNNER: _compose_service_env(COMPOSE_RUNNER, "test-runner"),
    }


K8S_KEYS = set(_k8s_worker_env())

#: Every k8s worker env key, in exactly one named class. The classes are what
#: the per-stack whitelists below are built from, so a new key in the manifest
#: has to be classified here (and then decided per stack) instead of quietly
#: counting as "expected to be missing" in all of them.
KEY_CLASSES: dict[str, set[str]] = {
    # N27: the k8s manifest sinks the tree root and moves the platform's own
    # files under `E2B_STATE_BASE`; the compose stacks keep the one-base layout
    # (`tests/unit/test_autoscaler_local_backend_shape.py::_k8s_env`).
    "k8s_state_layout": {
        "E2B_STATE_BASE",
        "E2B_SHARED_VOLUME_ROOT",
        "E2B_IMAGE_OCI_DIR",
    },
    # The disk-accounting knobs of `docs/k8s-deployment.md` §…/`docs/disk-*`,
    # declared in the k8s pod. The compose stacks run the code defaults.
    "k8s_disk_enforcement": {
        "E2B_PLATFORM_DISK_MB",
        "E2B_DISK_ENFORCE_INTERVAL_S",
        "E2B_DISK_ENFORCE_DIRTY",
        "E2B_DISK_DIRTY_GRACE_S",
        "E2B_DISK_RECONCILE_INTERVAL_S",
        "E2B_DISK_EXEC_LIMIT",
        "E2B_DISK_TIGHTEN_STEP_MB",
        "E2B_DISK_TIGHTEN_INTERVAL_S",
        "E2B_DISK_MAX_ENTRIES",
        "E2B_DISK_APPEND_TRIGGER_MB",
        "E2B_DISK_APPEND_MIN_INTERVAL_S",
        "E2B_DISK_OVERRUN_ACTION",
        "E2B_DISK_OVERRUN_DENY_S",
    },
    # N35/N14 real root + checkpoint/restore (`docs/deploy-clusters.md` §7/§9):
    # both are k8s deployment decisions, default-off in the image.
    "k8s_real_root_and_checkpoint": {
        "E2B_REAL_ROOT",
        "E2B_PAUSE_CHECKPOINT",
    },
    # The rotation window (E3.6): the examples name one internal key.
    "rotation_window": {"E2B_INTERNAL_API_KEYS"},
    # Track F/route-B scratch root: the file-capability brokers.
    "priv_helpers": {"E2B_PRIV_HELPERS"},
    # C1 (wave 2): the k8s worker dials the per-node broker DaemonSet over a
    # unix socket instead of running the privileged binaries itself. The
    # compose stacks ship no such daemon -- they keep the file-capability
    # shape (C1's `exec` transport) -- so both keys are k8s-only.
    "priv_broker_transport": {"E2B_PRIV_HELPER_TRANSPORT", "E2B_PRIV_HELPER_SOCKET"},
    # C3 (Task 3): which path grants a route-B slot its identity. The k8s worker
    # and the two separated production compose stacks (the ones that ship a
    # `c3-agent` service) run `agent-grant`; the arm-lane fleet stack, the local
    # pool, the single-machine example and the test runner have no agent and
    # keep the code default (`spawn`, the rollback lever).
    "slot_identity": {"E2B_SLOT_IDENTITY"},
    # Named template images (`docs/HANDOFF.md`: unset = the fixed set only).
    "template_images": {"E2B_TEMPLATE_IMAGES"},
    # Per-worker wiring: who the worker is and which control plane it dials.
    "worker_wiring": {
        "E2B_NODE_ID",
        "E2B_NODE_ADDRESS",
        "E2B_CONTROL_PLANE_URL",
        "E2B_INTERNAL_API_KEY",
    },
    # The extracted-rootfs cache (Z-F7 C1) and the per-worker node budget.
    "image_cache": {
        "E2B_IMAGE_CACHE_DIR",
        "E2B_IMAGE_CACHE_MAX_BYTES",
        "E2B_IMAGE_CACHE_EVICT_MIN_AGE_S",
        "E2B_IMAGE_CACHE_OWNER_UID",
    },
    "node_capacity": {
        "E2B_NODE_MEMORY_MB",
        "E2B_NODE_CPU_PERCENT",
        "E2B_NODE_DISK_MB",
        "E2B_NODE_PROCESSES",
    },
    # What a sandbox is built from.
    "base_image": {"E2B_BASE_IMAGE"},
    "workspace_base": {"E2B_WORKSPACE_BASE"},
    # The per-sandbox shape, split by what each piece needs: `pid_ns` stands
    # alone (the fork creates the user namespace itself; there is no pairing
    # guard and no failure mode that takes a sandbox offline -- fork
    # `crates/sandlock-core/src/sandbox.rs`, and `envd_service/config.py:239`),
    # while the netns pair is refused unpaired by `create_app`.
    "pid_namespace": {"E2B_PID_NS"},
    "netns_pair": {"E2B_ENABLE_NET_ISOLATION", "E2B_FD_INJECT_CONNECT"},
    "egress_switch": {"E2B_ENABLE_NETWORK"},
    "route_b_root": {"E2B_ROUTE_B_TMP_ROOT"},
}

#: Keys a *stack* declares that the k8s manifest does not, in exactly one named
#: class. The per-stack whitelists below are unions of these.
EXTRA_CLASSES: dict[str, set[str]] = {
    # The compose stacks serve quota through the quota-agent service, or run
    # the local XFS path; the k8s pod has no such sidecar.
    "compose_quota_agent": {
        "E2B_QUOTA_VIA_AGENT",
        "E2B_QUOTA_AGENT_URL",
        "E2B_QUOTA_AGENT_TOKEN",
        "E2B_QUOTA_AGENT_TIMEOUT_S",
    },
    # Pull credentials scoped to the registry host (Track Z).
    "compose_registry_pull": {
        "E2B_IMAGE_REGISTRY",
        "E2B_IMAGE_REGISTRY_USERNAME",
        "E2B_IMAGE_REGISTRY_PASSWORD",
    },
    # The deny list `envd_service/config.py:123` defaults to the same value;
    # the compose stacks spell it out.
    "compose_deny_cidrs": {"E2B_NETWORK_DENY_CIDRS"},
    # E3.2 per-sandbox host uids: the compose stacks pin the pool range (several
    # workers share one volume), the k8s worker follows the code default.
    "compose_uid_pool": {
        "E2B_PER_SANDBOX_UID",
        "E2B_UID_POOL_START",
        "E2B_UID_POOL_SIZE",
    },
    # Per-sandbox defaults the control plane of these examples sets (the k8s
    # control plane carries its own copy).
    "compose_template_defaults": {
        "E2B_DEFAULT_MEMORY_MB",
        "E2B_DEFAULT_CPU_PERCENT",
        "E2B_DEFAULT_DISK_MB",
        "E2B_DEFAULT_MAX_PROCESSES",
    },
    # Legacy, accepted and ignored (the fork dropped per-sandbox veth/netns).
    "legacy_enable_netns": {"E2B_ENABLE_NETNS"},
    # The pool chooses its executor (N38's rollback lever).
    "pool_executor": {"E2B_EXECUTOR"},
    # N16's default flip (2026-09-27): `E2B_PURE_ROOTFS` is `synth` in the
    # product now, and the single-machine example is a *pure* stack (no base
    # image) run under Docker's own default seccomp profile -- which does not
    # admit `unshare`, so the real root that fills the synthesized skeleton
    # cannot be built there (measured: `unshare(CLONE_NEWUSER): Operation not
    # permitted`). The example names the retreat lever
    # (`${E2B_PURE_ROOTFS:-off}`; set it to `synth` after installing
    # `deploy/seccomp/sandlock-worker.json` on that service).
    "demo_pure_rootfs_lever": {"E2B_PURE_ROOTFS"},
    # The test runner is an SDK client, not a worker: these are its own
    # switches (the shape it runs is selected by the runner image and the
    # suite's fixtures -- `deploy/docker/Dockerfile.test-runner`).
    "runner_client": {
        "E2B_API_KEY",
        "E2B_API_URL",
        "E2B_SANDBOX_URL",
        "E2B_REQUIRE_SECCOMP_FILTER",
    },
}

_FLEET_STACK_MISSING = (
    KEY_CLASSES["k8s_state_layout"]
    | KEY_CLASSES["k8s_disk_enforcement"]
    | KEY_CLASSES["k8s_real_root_and_checkpoint"]
    | KEY_CLASSES["priv_broker_transport"]
    | KEY_CLASSES["slot_identity"]
)

#: The compose example stacked with a control plane + Redis: it declares the
#: worker's wiring, cache, capacity, shape and egress, but not the k8s-only
#: classes above (state layout, disk enforcement, real-root/checkpoint, and the
#: C1 broker socket), the rotation window, the broker opt-in (default `auto`)
#: or named templates (default: none).
_COMPOSE_EXAMPLE_MISSING = (
    _FLEET_STACK_MISSING
    | KEY_CLASSES["rotation_window"]
    | KEY_CLASSES["priv_helpers"]
    | KEY_CLASSES["template_images"]
)

#: The two separated production stacks are the compose half of C3's coverage
#: (Global Constraints): they ship the `c3-agent` service, so unlike the fleet
#: stack above they *do* name the identity path instead of inheriting `spawn`.
_C3_COMPOSE_MISSING = _COMPOSE_EXAMPLE_MISSING - KEY_CLASSES["slot_identity"]

#: The local pool: the autoscaler builds the worker's `docker run` argv itself,
#: so its env JSON is the worker's whole environment -- no workspace base, no
#: per-worker wiring (those are `-e` flags), no templates/brokers.
_POOL_MISSING = _COMPOSE_EXAMPLE_MISSING | KEY_CLASSES["worker_wiring"] | {
    "E2B_WORKSPACE_BASE",
}

#: The single-machine build example (`docker-compose.yml`): one `envd`, no
#: control-plane wiring, no node budget, cache-only env plus the shape switch.
_DEMO_MISSING = (
    _COMPOSE_EXAMPLE_MISSING
    | KEY_CLASSES["worker_wiring"]
    | KEY_CLASSES["node_capacity"]
    | KEY_CLASSES["base_image"]
    | KEY_CLASSES["netns_pair"]
    | KEY_CLASSES["egress_switch"]
    | KEY_CLASSES["route_b_root"]
)

#: The test runner: it names only what the in-container suite needs to build
#: sandboxes from this checkout (the workspace root, the base image, the pid
#: namespace); everything else is selected by the runner image and the
#: fixtures, and this last class is why the runner is not "the fleet".
_RUNNER_MISSING = (
    _COMPOSE_EXAMPLE_MISSING
    | KEY_CLASSES["worker_wiring"]
    | KEY_CLASSES["image_cache"]
    | KEY_CLASSES["node_capacity"]
    | KEY_CLASSES["netns_pair"]
    | KEY_CLASSES["egress_switch"]
    | KEY_CLASSES["route_b_root"]
)

#: Per stack: the k8s keys it may not declare, and the keys it adds.
#:
#: `pid_namespace` appears in none of them on purpose: the per-sandbox PID
#: namespace is the one shape key every worker-shaped stack must carry (N45).
ALLOWED_MISSING: dict[str, set[str]] = {
    FLEET_STACK: _FLEET_STACK_MISSING,
    POOL_COMPOSE: _POOL_MISSING,
    POOL_BACKEND: _POOL_MISSING | KEY_CLASSES["base_image"],
    COMPOSE_PROD: _C3_COMPOSE_MISSING,
    COMPOSE_MULTINODE: _C3_COMPOSE_MISSING,
    COMPOSE_DEMO: _DEMO_MISSING,
    COMPOSE_RUNNER: _RUNNER_MISSING,
}

ALLOWED_EXTRA: dict[str, set[str]] = {
    FLEET_STACK: (
        EXTRA_CLASSES["compose_quota_agent"]
        | EXTRA_CLASSES["compose_registry_pull"]
        | EXTRA_CLASSES["compose_deny_cidrs"]
        | EXTRA_CLASSES["compose_uid_pool"]
        | EXTRA_CLASSES["compose_template_defaults"]
        | EXTRA_CLASSES["legacy_enable_netns"]
    ),
    POOL_COMPOSE: EXTRA_CLASSES["pool_executor"],
    POOL_BACKEND: set(),
    COMPOSE_PROD: (
        EXTRA_CLASSES["compose_quota_agent"]
        | EXTRA_CLASSES["compose_registry_pull"]
        | EXTRA_CLASSES["compose_deny_cidrs"]
        | EXTRA_CLASSES["legacy_enable_netns"]
    ),
    COMPOSE_MULTINODE: set(),
    COMPOSE_DEMO: EXTRA_CLASSES["demo_pure_rootfs_lever"],
    COMPOSE_RUNNER: EXTRA_CLASSES["runner_client"],
}


def _effective_default(value: str) -> str:
    """`${VAR:-default}` -> `default`; anything else -> itself."""
    match = _INTERPOLATION_RE.match(value)
    return match.group(1) if match else value


def test_every_k8s_worker_key_is_classified() -> None:
    """The reference key set is partitioned: a new manifest key must be named.

    Without this, a key added to `deploy/k8s/worker.yaml` would land in every
    stack's "missing" set and the per-stack equality below would call it
    "expected" -- the exact silence that let N45 happen.
    """
    owner: dict[str, str] = {}
    for name, keys in KEY_CLASSES.items():
        for key in keys:
            assert key not in owner, f"{key} is classified twice: {owner[key]}, {name}"
            owner[key] = name
    assert set(owner) == K8S_KEYS, sorted(set(owner) ^ K8S_KEYS)
    # The extras are named the same way, so a whitelist literal cannot point at
    # a key no class accounts for.
    known_extras = set().union(*EXTRA_CLASSES.values())
    for path, keys in ALLOWED_EXTRA.items():
        assert keys <= known_extras, (path, sorted(keys - known_extras))


def test_every_worker_stack_declares_the_fleets_keys_except_a_named_whitelist() -> None:
    """Each stack's key set differs from the k8s worker's only as documented.

    Both directions are exact: a fleet key that is neither declared nor named
    is a defect (N45, and N38/N42 before it), and a whitelist entry for a key
    the stack already declares would silently re-license its removal.
    """
    envs = _worker_envs()
    assert set(envs) == set(ALLOWED_MISSING) == set(ALLOWED_EXTRA)
    for path, env in envs.items():
        allowed_missing = ALLOWED_MISSING[path]
        # A whitelist may only excuse a key the fleet actually names.
        assert allowed_missing <= K8S_KEYS, (path, sorted(allowed_missing - K8S_KEYS))
        missing = K8S_KEYS - set(env)
        assert missing == allowed_missing, (
            path,
            "missing != whitelist",
            sorted(missing - allowed_missing),
            sorted(allowed_missing - missing),
        )
        extra = set(env) - K8S_KEYS
        assert extra == ALLOWED_EXTRA[path], (
            path,
            "extra != whitelist",
            sorted(extra - ALLOWED_EXTRA[path]),
            sorted(ALLOWED_EXTRA[path] - extra),
        )


def test_every_worker_stack_turns_on_the_per_sandbox_pid_namespace() -> None:
    """N45: the switch is present *and* on, with the fleet's own value.

    `envd_service/config.py:246` defaults it off, so "declared" is not enough:
    a stack that carried `"false"` would pass the key-set pin above and still
    leave every sandbox sharing the worker's pid namespace.
    """
    fleet = _k8s_worker_env()["E2B_PID_NS"]
    assert fleet == "true"
    for path, env in _worker_envs().items():
        assert "E2B_PID_NS" in env, path
        # `_unquote` because the backend's declaration is a Python string
        # literal (`"E2B_PID_NS": "true"`), where the compose/YAML ones arrive
        # already unquoted.
        declared = _effective_default(_unquote(env["E2B_PID_NS"]))
        assert declared == fleet, (path, env["E2B_PID_NS"])
    # The fleet's own compose stack keeps its per-node rollback lever for
    # worker-2 (`E2B_PID_NS_WORKER2`), which the anchors above inherit.
    worker2 = _compose_service_env(FLEET_STACK, "worker-2")
    assert _effective_default(worker2["E2B_PID_NS"]) == fleet


def test_the_pools_two_declarations_agree() -> None:
    """The hand-built `DockerPoolBackend()` must not fall behind the JSON.

    N38 was exactly this drift in reverse: the compose JSON was the entry
    point, the backend's own dict was not, and the shape only held when an
    operator used the former.
    """
    compose_env = _pool_compose_env()
    backend_env = _pool_backend_env()
    assert set(backend_env) <= set(compose_env), sorted(set(backend_env) - set(compose_env))
    literals = {
        key: value for key, value in backend_env.items() if value.startswith('"')
    }
    # Every quoted literal in the dict is the same string the JSON declares
    # (the two unquoted ones are the constructor parameters for the node
    # budget, pinned by `test_spawned_worker_argv_...` in the pool's own file).
    assert {key: _unquote(value) for key, value in literals.items()} == {
        key: compose_env[key] for key in literals
    }
    # The shape keys are declared as literals, so they are covered above; spell
    # out that the list is not empty (an all-parameterised dict would vacate it).
    assert "E2B_PID_NS" in literals


def test_the_three_multinode_workers_declare_the_same_env_keys() -> None:
    """One whitelist must not hide a per-service drift in the example."""
    workers = {
        name: set(_compose_service_env(COMPOSE_MULTINODE, name))
        for name in ("worker-1", "worker-2", "worker-3")
    }
    assert len(set(map(frozenset, workers.values()))) == 1, workers
