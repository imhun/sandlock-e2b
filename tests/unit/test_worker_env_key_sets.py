"""Every worker-shaped stack declares the fleet's worker env keys (N45).

`docs/open-issues.md` N45: one stack's worker env never declared `E2B_PID_NS`,
so its sandbox shared the worker's PID namespace -- `kill(1, 0)` answered
`EPERM` for the worker's live PID 1, an existence oracle (measured: `getpid=81`,
`kill1=EPERM`; with the key: `getpid=7`, `kill1=ok`). The fleet turns it on in
both manifests (`deploy/k8s/worker.yaml`, `deploy/stack/docker-compose.prod.yml`),
and the ruling on N36/N38 was "unify the shape".

That stack was the autoscaler's local Docker pool; it is gone (2026-09-30 --
the local compose autoscaler was retired and the loop now runs inside the k8s
control plane), so the matrix below covers the stacks that still exist.

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


def _k8s_worker_env() -> dict[str, str]:
    """`deploy/k8s/worker.yaml`'s `- name: E2B_*` entries.

    A `value:` literal is returned as written; an entry sourced from the pod
    (`valueFrom:`: `POD_IP`, the internal-key Secret) is returned as
    `"<valueFrom>"`, since this pin is about which keys exist and what a
    literal switch like `E2B_PID_NS` is set to.
    """
    return _k8s_env(FLEET_K8S)


def _k8s_env(relative: str) -> dict[str, str]:
    """One k8s manifest's (or overlay patch's) `- name: E2B_*` entries.

    The k0s overlay patches are strategic-merge files with the same env-list
    shape as the manifests they patch, so one reader serves both -- and a pin
    that reads the *patch* is what makes "the overlay raises this value" a fact
    about the deployed lane rather than about the baseline.
    """
    lines = (REPO / relative).read_text(encoding="utf-8").splitlines()
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
    # (`tests/unit/test_compose_base_image_shape.py::_k8s_env`).
    # N57 / Task 4 adds `E2B_NODE_STATE_BASE` to the same class: the node-local
    # half of that state (the create's marker and accounting seed, `.route-b`
    # and the uid pool's own files) is a k8s hostPath, and the compose stacks
    # stay on the one-base layout precisely because naming no node-local base
    # is byte-for-byte today's behaviour.
    "k8s_state_layout": {
        "E2B_STATE_BASE",
        "E2B_NODE_STATE_BASE",
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
    # Checkpoint/restore (`docs/deploy-clusters.md` §9): a k8s deployment
    # decision, default-off in the image. `E2B_REAL_ROOT` used to be classified
    # here next to it; N14 S5 (2026-10-04) retired it -- the real root is the
    # shape now, and `E2B_REAL_ROOT=0` is refused at startup -- so the class is
    # named for what is left.
    "k8s_checkpoint": {
        "E2B_PAUSE_CHECKPOINT",
    },
    # N57 / Task 3: the byte bound for one tree copy (a migration archive or a
    # snapshot payload). The k8s fleet names it next to the image-cache bound
    # it is shaped after; the compose stacks run the code default (1 GiB = one
    # sandbox's quota, `docs/create-local-first-design.md` §3.1), which is the
    # same value -- naming it there would be a second place to keep in step
    # for no decision the lane makes differently.
    "k8s_tree_copy_bound": {
        "E2B_TREE_COPY_MAX_BYTES",
        "E2B_TREE_COPY_WINDOW_BYTES",
    },
    # The rotation window (E3.6): the examples name one internal key.
    "rotation_window": {"E2B_INTERNAL_API_KEYS"},
    # N49 step ① (2026-10-03): the per-node credential map
    # (`{"<key>": "<node_id>"}`, one credential per StatefulSet ordinal, minted
    # by `secrets.sh`). k8s-only: the compose examples ship no map, so their
    # workers present the fleet key -- which is what every lane did before this
    # landed. Absent from the Secret, the k8s worker falls back the same way
    # (`optional: true`).
    "k8s_internal_node_keys": {"E2B_INTERNAL_NODE_KEYS"},
    # C3 (Task 4 slice B): which shape performs the worker's privileged file
    # steps. The worker image no longer ships the file-capability binaries, so
    # `auto` would silently resolve none and degrade to the in-process E5.1
    # shape; every worker that has an agent names `agent` in the same change as
    # the binary removal -- the k8s pod and the three C3 compose stacks (the two
    # separated examples and the target host's stack). The arm-lane fleet, the
    # single-machine example and the test runner have no agent.
    "priv_helper_transport": {"E2B_PRIV_HELPER_TRANSPORT"},
    # C1 (wave 2) / Task 4 slice B: the socket rollback lever
    # (`E2B_PRIV_HELPER_SOCKET` + the `wait-for-broker` gate) was the k8s
    # worker's, and only the k8s worker's. C3 Task 7 retired it with the broker
    # DaemonSet, so there is no key to classify any more -- the manifest pin
    # that it stays gone lives in `test_c3_agent_manifest.py`.
    # C3 (Task 3): which path grants a route-B slot its identity. The k8s worker
    # and the two separated production compose stacks (the ones that ship a
    # `c3-agent` service) run `agent-grant`; the arm-lane fleet stack, the
    # single-machine example and the test runner have no agent and keep the
    # code default (`spawn`, the rollback lever).
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
    # N83 phase 1 / Task 2 (2026-10-06): the per-sandbox cgroup switch and the
    # mount root its rw cgroupfs view lands on. The fleet names both
    # (`E2B_SANDBOX_CGROUP=off` in `deploy/k8s/worker.yaml`, flipped to
    # `required` by the k0s overlay) and so do the three C3 compose stacks (the
    # same rw bind, `off` by default). The single-machine build example
    # (`docker-compose.yml`) and the SDK test runner build no per-sandbox cgroup
    # at all, so both carry neither key -- their `.env` classes below name that
    # absence rather than letting a missing switch look like an oversight.
    #
    # N83 phase 2 / Task 1 (2026-10-06) put the ceiling trio
    # (`E2B_MAX_SANDBOX_*`, D5) in this class, because what a *single* sandbox
    # may be configured to only means anything where a per-sandbox cgroup is
    # built. Ruling R17 (2026-10-07) moved them **out of the worker manifests
    # entirely**: the ceiling is the *control plane's* policy, handed down in
    # the register/heartbeat answer, so the keys belong to the control-plane
    # manifests now -- and `test_the_ceiling_trio_lives_on_the_control_plane`
    # below is where they are pinned. A worker that still declares them grants
    # nothing (the worker never reads them), which is why they are no longer
    # classified here: this file's job is the *worker* key set.
    "sandbox_cgroup": {
        "E2B_SANDBOX_CGROUP",
        "E2B_CGROUP_MOUNT",
    },
}

#: N83 phase 2 / R17: the three per-sandbox ceilings, and every manifest that
#: must declare them. The control plane is the owner -- it writes them into
#: every node record and hands them down -- so each lane's *control-plane*
#: service names all three, and the value is pinned nowhere here on purpose:
#: what matters at this layer is that the key exists where the policy lives.
CEILING_KEYS = (
    "E2B_MAX_SANDBOX_CPU_PERCENT",
    "E2B_MAX_SANDBOX_MEMORY_MB",
    "E2B_MAX_SANDBOX_PROCESSES",
)

CONTROL_PLANE_STACKS = (
    ("deploy/k8s/control-plane.yaml", None),
    ("deploy/k8s-k0s/control-plane-capacity.patch.yaml", None),
    ("deploy/compose/docker-compose.multinode.yml", "control-plane"),
    ("deploy/compose/docker-compose.prod.yml", "control-plane"),
    ("deploy/stack/docker-compose.prod.yml", "control-plane"),
)

#: The k8s **worker**-shaped files whose env lists are pinned too. The base
#: manifest is the reference key set the per-stack whitelists are compared
#: against -- and the k0s overlay patches it, so both are manifests an operator
#: reads while deploying. `deploy/k8s-k0s/worker-capacity.patch.yaml` is
#: otherwise only read for its `resources.limits` (the D5b counterpart), so
#: without this list the ceiling trio could be put back into it and nothing
#: would go red -- behaviourally harmless, but it would tell the reader the
#: worker owns the policy.
WORKER_MANIFESTS = (
    "deploy/k8s/worker.yaml",
    "deploy/k8s-k0s/worker-capacity.patch.yaml",
)

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
    # N14 S5 (2026-10-04) retired the single-machine example's one extra key:
    # `E2B_PURE_ROOTFS=off` (N16's retreat lever, which the example used
    # because Docker's own default profile does not admit `unshare`) is
    # refused by name at startup now, and the example carries
    # `deploy/seccomp/sandlock-worker.json` instead -- the same profile every
    # other deployment applies. So the demo stack now declares no key the
    # fleet does not.
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

#: What the **fleet stack** -- `deploy/stack/docker-compose.prod.yml`, the
#: compose half of the shipped host (`FLEET_STACK` above) -- does *not* declare
#: next to the k8s manifest. It is the reference the other compose stacks are
#: compared against, so the two C3 keys it *does* name are deliberately absent
#: from this set (Task 4 slice B):
#:
#: * `E2B_PRIV_HELPER_TRANSPORT=agent` (its workers run the agent shape; the
#:   key is named on all three C3 compose stacks -- see `priv_helper_transport`
#:   in `KEY_CLASSES`);
#: * `E2B_SLOT_IDENTITY=agent-grant` (likewise, `slot_identity`).
#:
#: What is left is k8s-only: the state layout, the disk-enforcement knobs and
#: the real-root/checkpoint switches. (C1's broker socket path used to be in
#: this set too; C3 Task 7 retired it with the DaemonSet.)
_FLEET_STACK_MISSING = (
    KEY_CLASSES["k8s_state_layout"]
    | KEY_CLASSES["k8s_disk_enforcement"]
    | KEY_CLASSES["k8s_checkpoint"]
    | KEY_CLASSES["k8s_tree_copy_bound"]
    | KEY_CLASSES["k8s_internal_node_keys"]
)

#: The compose example stacked with a control plane + Redis: it declares the
#: worker's wiring, cache, capacity, shape and egress, but not the k8s-only
#: classes above (state layout, disk enforcement, real-root/checkpoint), the
#: rotation window or named templates (default: none).
_COMPOSE_EXAMPLE_MISSING = (
    _FLEET_STACK_MISSING
    | KEY_CLASSES["priv_helper_transport"]
    | KEY_CLASSES["rotation_window"]
    | KEY_CLASSES["template_images"]
)

#: The two separated production stacks are the compose half of C3's coverage
#: (Global Constraints): they ship the `c3-agent` service, so unlike the fleet
#: stack above they *do* name both the identity path (instead of inheriting
#: `spawn`) and the transport (instead of inheriting the inert `auto`).
_C3_COMPOSE_MISSING = (
    _COMPOSE_EXAMPLE_MISSING
    - KEY_CLASSES["slot_identity"]
    - KEY_CLASSES["priv_helper_transport"]
)

#: The single-machine build example (`docker-compose.yml`): one `envd`, no
#: control-plane wiring, no node budget, cache-only env plus the shape switch.
#: N52 made its (absent) file-operation capability an *absence*: the knobs that
#: used to declare it are gone, and the example is the in-process (E5.1) shape
#: it always was (D23, named in `test_c3_agent_manifest.py`).
_DEMO_MISSING = (
    _COMPOSE_EXAMPLE_MISSING
    | KEY_CLASSES["worker_wiring"]
    | KEY_CLASSES["node_capacity"]
    | KEY_CLASSES["base_image"]
    | KEY_CLASSES["netns_pair"]
    | KEY_CLASSES["egress_switch"]
    | KEY_CLASSES["route_b_root"]
    | KEY_CLASSES["slot_identity"]
    | KEY_CLASSES["sandbox_cgroup"]
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
    | KEY_CLASSES["slot_identity"]
    | KEY_CLASSES["sandbox_cgroup"]
)

#: Per stack: the k8s keys it may not declare, and the keys it adds.
#:
#: `pid_namespace` appears in none of them on purpose: the per-sandbox PID
#: namespace is the one shape key every worker-shaped stack must carry (N45).
ALLOWED_MISSING: dict[str, set[str]] = {
    FLEET_STACK: _FLEET_STACK_MISSING,
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
    COMPOSE_PROD: (
        EXTRA_CLASSES["compose_quota_agent"]
        | EXTRA_CLASSES["compose_registry_pull"]
        | EXTRA_CLASSES["compose_deny_cidrs"]
        | EXTRA_CLASSES["legacy_enable_netns"]
    ),
    COMPOSE_MULTINODE: set(),
    COMPOSE_DEMO: set(),
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


def test_the_three_multinode_workers_declare_the_same_env_keys() -> None:
    """One whitelist must not hide a per-service drift in the example."""
    workers = {
        name: set(_compose_service_env(COMPOSE_MULTINODE, name))
        for name in ("worker-1", "worker-2", "worker-3")
    }
    assert len(set(map(frozenset, workers.values()))) == 1, workers


def test_the_ceiling_trio_lives_on_the_control_plane() -> None:
    """N83 phase 2 / ruling R17: the policy keys are the control plane's, and
    no worker-shaped manifest declares them any more.

    The ceiling is one deployment fact -- the control plane writes it into every
    node record and hands it down in the register/heartbeat answer -- so a
    worker manifest that declares it can only mislead an operator. Both halves
    are pinned exactly: every lane's *control-plane* service (the k0s overlay
    patch included) names all three, and every *worker* service -- the compose
    services, the k8s base manifest and the k0s worker overlay patch -- names
    none.
    """
    for relative, service in CONTROL_PLANE_STACKS:
        if service is None:
            env = _k8s_env(relative)
        else:
            env = _compose_service_env(relative, service)
        missing = [key for key in CEILING_KEYS if key not in env]
        assert missing == [], (relative, service, missing)
    for path, env in _worker_envs().items():
        declared = [key for key in CEILING_KEYS if key in env]
        assert declared == [], (path, declared)
    for relative in WORKER_MANIFESTS:
        declared = [key for key in CEILING_KEYS if key in _k8s_env(relative)]
        assert declared == [], (relative, declared)


def _resource_limits(relative: str, container: str) -> dict[str, str]:
    """One container's `resources.limits` block, key -> raw value.

    Line-oriented like the env readers above, and scoped to a named container
    so a manifest with a sidecar (the control-plane pod's buildkit) cannot lend
    its limits to the pod the ceiling is about.
    """
    lines = (REPO / relative).read_text(encoding="utf-8").splitlines()
    header = f"        - name: {container}"
    starts = [index for index, line in enumerate(lines) if line == header]
    assert len(starts) == 1, (relative, container, starts)
    limits: dict[str, str] = {}
    inside = False
    for line in lines[starts[0] + 1 :]:
        if line.strip() == "" or line.strip().startswith("#"):
            continue
        if line.startswith("        - name: "):
            break
        if line.strip() == "limits:":
            inside = True
            continue
        if inside:
            match = re.match(r"^\s+([a-zA-Z]+):\s*(\S+)\s*$", line)
            if match:
                limits[match.group(1)] = _unquote(match.group(2))
                continue
            if line.strip() in {"requests:", "resources:"}:
                inside = False
    return limits


def test_the_k0s_overlay_raises_the_handed_down_ceiling_with_the_pod_limits() -> None:
    """The overlay's control-plane policy and the worker pod's kernel, in step.

    The worker refuses a hand-down above its own container's kernel limits
    (D5b), so these two files are one action: raising the ceiling without
    raising the pod turns every create on that node into a named refusal, and
    raising the pod without the ceiling silently under-sells the lane. The
    baseline is pinned the same way (2 cores / 2 GiB), because that is the pair
    the overlay *moves away from*.
    """
    baseline_cp = _k8s_env("deploy/k8s/control-plane.yaml")
    baseline_worker_limits = _resource_limits("deploy/k8s/worker.yaml", "worker")
    assert [baseline_cp[key] for key in CEILING_KEYS] == ["200", "2048", "256"]
    assert baseline_worker_limits["cpu"] == "2"
    assert baseline_worker_limits["memory"] == "2Gi"

    overlay_cp = _k8s_env("deploy/k8s-k0s/control-plane-capacity.patch.yaml")
    overlay_worker_limits = _resource_limits(
        "deploy/k8s-k0s/worker-capacity.patch.yaml", "worker"
    )
    assert [overlay_cp[key] for key in CEILING_KEYS] == ["400", "4096", "1024"]
    assert overlay_worker_limits["cpu"] == "4"
    assert overlay_worker_limits["memory"] == "4Gi"
    # cpuCount is cores on the wire and percent in the policy, memory is MiB.
    assert int(overlay_cp[CEILING_KEYS[0]]) == int(overlay_worker_limits["cpu"]) * 100
    assert int(overlay_cp[CEILING_KEYS[1]]) == int(
        overlay_worker_limits["memory"].removesuffix("Gi")
    ) * 1024
