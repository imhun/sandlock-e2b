"""Control plane configuration from environment variables."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from gateway_common.env import registry_host
from gateway_common.archive import DEFAULT_TREE_COPY_MAX_BYTES
from gateway_common.env import (
    _env_bool,
    _env_float,
    _env_int,
    _env_json_dict,
    _env_json,
    _env_list,
)
from gateway_common.sandbox_ceiling import (
    MAX_SANDBOX_CPU_PERCENT_ENV,
    MAX_SANDBOX_MEMORY_MB_ENV,
    MAX_SANDBOX_PROCESSES_ENV,
    resolve_sandbox_ceiling,
)

#: Default per-key budget for the *resource-creating* control-plane endpoints
#: (sandbox create, snapshot create, volume create). One constant so the three
#: cannot drift apart; each endpoint keeps its own env override and, per repo
#: convention, ``0`` disables its limiter.
DEFAULT_CREATE_RATE_LIMIT_PER_MIN = 120

#: The **in-process** node's own totals when ``E2B_MAX_TOTAL_*`` names none.
#:
#: N83 phase 2 / Task 9 turned those four into optional overrides for the
#: *fleet* ladder (unset/``0`` = Σ of the healthy nodes' own totals). The
#: in-process node (``E2B_ENABLE_LOCAL_NODE``) is the one node that has nothing
#: to derive from: there is no worker report behind it and no separate container
#: of its own to read -- *this* process is the node -- so it keeps the numbers
#: ``E2B_MAX_TOTAL_*`` defaulted to before that ruling, and a combined
#: deployment's admission behaviour is unchanged. A deployment that wants a
#: different number for this node sets ``E2B_MAX_TOTAL_*``, which the local node
#: still reads first. Leaving it at ``0`` instead would read, one line later, as
#: "unbounded" -- the fail-open the plan's Review Focus §1 names.
IN_PROCESS_NODE_DEFAULT_TOTALS: dict[str, int] = {
    "memory_mb": 8192,
    "cpu_percent": 400,
    "disk_mb": 10240,
    "processes": 2048,
}


def _state_base_from_env() -> Path | None:
    """``E2B_STATE_BASE``, resolved, or ``None`` when the deployment has none.

    ``None`` means "the platform's files stay under the workspace base" --
    :meth:`Settings.__post_init__` fills the field in with *that object's*
    ``workspace_base``. Deliberately not done here: a factory cannot see the
    field the caller passed, and a base read out of the environment while the
    trees sit under a different one is exactly the split this switch is about.
    """
    raw = os.getenv("E2B_STATE_BASE")
    return Path(raw).resolve() if raw else None


#: The values ``E2B_TREES_SHARED`` reads as "on". Anything else non-empty --
#: including ``0``, ``no``, ``off`` -- is off, so a typo cannot silently turn
#: the trees local: the variable's *presence* is the claim, and only an
#: explicit on-value keeps them shared.
_TREES_SHARED_ON = frozenset({"1", "true", "yes", "on"})


def _trees_shared_from_env() -> bool | None:
    """``E2B_TREES_SHARED``, or ``None`` when the deployment does not name it.

    ``None`` is not ``False``: it means "ask the old question"
    (``bool(shared_workspace_root)``), which is what keeps every deployment
    that predates this switch on exactly the shape it has today. An empty
    value counts as unnamed -- the repo's rule for every other base
    (``resolve_state_base``), and the reason a k8s ``value: ""`` cannot flip a
    fleet's trees onto node-local disk.
    """
    raw = os.getenv("E2B_TREES_SHARED")
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TREES_SHARED_ON


@dataclass
class Settings:
    """All tunables of the control plane.

    Defaults match spec section 7.2. A total limit of ``0`` disables that
    dimension.
    """

    api_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_API_KEYS", ("local-key",))
    )
    api_key: str | None = field(default_factory=lambda: os.getenv("E2B_API_KEY"))
    control_plane_port: int = field(
        default_factory=lambda: _env_int("E2B_CONTROL_PLANE_PORT", 3000)
    )
    envd_port: int = field(default_factory=lambda: _env_int("E2B_ENVD_PORT", 49983))
    warm_timeout_s: int = field(
        default_factory=lambda: _env_int("E2B_WARM_TIMEOUT_S", 180)
    )
    #: How long a worker's heartbeat may be silent before its node is marked
    #: ``unhealthy`` (and, via E6.1, its sandboxes are treated as orphans).
    #: 15s matches the historical hard-coded ``NodeRegistry`` default, which is
    #: fine when every node's image rootfs is on local disk (a 0.3s extraction).
    #: It is NOT fine when the node's image cache is a network filesystem: the
    #: first use of a built template unpacks thousands of small files
    #: (measured on Aliyun NAS 2026-09-17: 61s for a python-slim rootfs, 240x the
    #: local-overlay 0.26s), and the worker resolves that rootfs *on its event
    #: loop*, so it cannot heartbeat for minutes -- a healthy node then looks
    #: dead and its live sandboxes get reaped. Raise this above the slowest
    #: extraction the storage can produce.
    node_heartbeat_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_NODE_HEARTBEAT_TIMEOUT", 15.0)
    )
    #: How long an ``orphaned`` sandbox record may sit before the TTL sweep is
    #: allowed to collect it (seconds; 0 = never). Orphaned records are exempt
    #: from expiry on purpose -- the lost worker may still be running them, and
    #: deleting the workspace underneath a live process orphans live inodes --
    #: but a node that *never* comes back then leaves its records, its rows and
    #: its trees behind forever (N22). A deployment that retires workers
    #: (autoscaling down, replacing a machine) can set this to a grace period it
    #: is comfortable with; the default keeps today's behaviour.
    orphan_record_ttl_s: float = field(
        default_factory=lambda: _env_float("E2B_ORPHAN_RECORD_TTL", 0.0)
    )
    #: Directory the control plane exports a locally built template's OCI layout
    #: tar into (``<dir>/_oci/<slug>.oci.tar``). Empty means "same as
    #: ``image_cache_dir``". Set it when the extracted rootfs is node-local: the
    #: tar has to stay on the shared volume every worker can read, while a worker
    #: unpacks it into its own cache (``E2B_IMAGE_OCI_DIR`` on the worker side).
    image_oci_dir: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["E2B_IMAGE_OCI_DIR"]).resolve()
            if os.getenv("E2B_IMAGE_OCI_DIR")
            else None
        )
    )
    executor: str = field(
        default_factory=lambda: os.getenv("E2B_EXECUTOR", "auto").lower()
    )
    workspace_base: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_WORKSPACE_BASE", "tmp/sandboxes")
        ).resolve()
    )
    #: The base the platform's *own* files live under (N27): the sandboxes'
    #: runtime records, their command logs and their checkpoint images -- the
    #: same fact ``envd_service.config.Settings.state_base`` names on the
    #: worker side, and deliberately spelled the same way, because the two
    #: processes have to agree about it or a record is written where nobody
    #: reads it.
    #:
    #: ``E2B_STATE_BASE`` moves them out from under the tree root, so "a sandbox
    #: cannot reach the platform's state" stops depending on the sandbox's shape
    #: (``docs/pure-shape-decision.md`` §4). Unset = the workspace base, which is
    #: today's layout -- and the field has to be ``None`` until
    #: :meth:`__post_init__` fills it in, because the default is *this object's*
    #: ``workspace_base`` and not whatever ``E2B_WORKSPACE_BASE`` says.
    #:
    #: ``resolve()`` mirrors ``workspace_base`` above, and it is load-bearing:
    #: ``tmp/`` and the cluster's NFS export both carry symlinks, and a relative
    #: or unnormalised spelling of one base makes "are these two the same
    #: directory?" answer wrong.
    state_base: Path | None = field(default_factory=_state_base_from_env)
    image_cache_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_IMAGE_CACHE_DIR", "tmp/sandboxes/_images")
        ).resolve()
    )
    max_sandboxes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_SANDBOXES", 100)
    )
    default_timeout: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_TIMEOUT", 300)
    )
    max_command_timeout: int = field(
        default_factory=lambda: _env_int("E2B_MAX_COMMAND_TIMEOUT", 3600)
    )
    default_memory_mb: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MEMORY_MB", 1024)
    )
    default_cpu_percent: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_CPU_PERCENT", 100)
    )
    default_disk_mb: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_DISK_MB", 1024)
    )
    default_max_processes: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MAX_PROCESSES", 256)
    )
    base_image: str | None = field(default_factory=lambda: os.getenv("E2B_BASE_IMAGE"))
    template_images: dict[str, str] = field(
        default_factory=lambda: _env_json_dict("E2B_TEMPLATE_IMAGES")
    )
    #: N83 phase 2 / Task 9 (the user's ruling, 2026-10-07: "max total 是不是没
    #: 必要了，其实就是 worker 的上限加一起，可以自动计算"): these three are
    #: **optional overrides** for the *fleet* ladder. An explicit positive value
    #: wins -- the one reason left to set one is to deliberately sell less --
    #: and unset/``0`` derives from the registered healthy nodes' own
    #: ``total_*`` (`SandboxRegistry._fleet_limits`, one implementation).
    #:
    #: So ``0`` no longer means "unlimited" here: it means "derive". With no
    #: node left to derive from the fleet ladder polices nothing and the create
    #: is refused by *placement*, by name (``503 No resources available``) --
    #: the fail-closed direction, and one message instead of two. The one
    #: reader that still takes ``0`` as "no number named" is the **in-process
    #: node's own row** -- see :data:`IN_PROCESS_NODE_DEFAULT_TOTALS`.
    max_total_memory_mb: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_MEMORY_MB", 0)
    )
    max_total_cpu_percent: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_CPU_PERCENT", 0)
    )
    max_total_disk_mb: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_DISK_MB", 10240)
    )
    # Per-sandbox host uid pool (E3.2). Same names as the worker's settings on
    # purpose: the control plane is now the allocator (OBS-9 -- the pool used
    # to be derived from files inside the shared volume, which any root on any
    # mounting node can rewrite), so both sides must agree on the range.
    uid_pool_start: int = field(
        default_factory=lambda: _env_int("E2B_UID_POOL_START", 10000)
    )
    uid_pool_size: int = field(
        default_factory=lambda: _env_int("E2B_UID_POOL_SIZE", 1000)
    )
    #: Mirrors the worker's switch (same env name, same default): a deployment
    #: that is not putting sandboxes on per-sandbox uids must not consume the
    #: pool, or a thousand creates would exhaust it and start refusing work for
    #: a feature that is switched off.
    per_sandbox_uid: bool = field(
        default_factory=lambda: _env_bool("E2B_PER_SANDBOX_UID", True)
    )
    max_total_processes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_PROCESSES", 0)
    )
    #: N83 phase 2 (D5): what ONE sandbox may be configured to -- the
    #: per-sandbox **policy** ceiling, never the node. `0`/unset follows the
    #: node's own total for the same dimension (``max_total_*`` above), and a
    #: node that declared no total at all falls back to the per-sandbox create
    #: default, so the resolved value is never 0: 0 would read downstream as
    #: "one sandbox may take everything", the fail-open the plan's Review Focus
    #: §1 names. Ruling R17 (2026-10-07) makes this **the** ceiling: it is the
    #: control plane's policy, written into every node record and handed down to
    #: every worker in the register/heartbeat response. The worker no longer
    #: reads these names at all -- a create request's ``cpuCount``/``memoryMB``
    #: is the *requirement*, and ``requirement <= ceiling`` is the whole rule.
    #: The names come from `gateway_common.sandbox_ceiling` (the three
    #: ``MAX_SANDBOX_*_ENV`` constants), so the Python side spells them once --
    #: the manifests and the acceptance script still write the strings out
    #: literally, which is what that module's constants cannot reach. The
    #: resolution rule is `gateway_common.sandbox_ceiling.resolve_sandbox_ceiling`.
    max_sandbox_cpu_percent: int = field(
        default_factory=lambda: _env_int(MAX_SANDBOX_CPU_PERCENT_ENV, 0)
    )
    max_sandbox_memory_mb: int = field(
        default_factory=lambda: _env_int(MAX_SANDBOX_MEMORY_MB_ENV, 0)
    )
    max_sandbox_processes: int = field(
        default_factory=lambda: _env_int(MAX_SANDBOX_PROCESSES_ENV, 0)
    )
    create_rate_limit_per_min: int = field(
        default_factory=lambda: _env_int(
            "E2B_CREATE_RATE_LIMIT_PER_MIN", DEFAULT_CREATE_RATE_LIMIT_PER_MIN
        )
    )
    # Per-key budgets for the other *resource-creating* endpoints. They share
    # the create budget's default on purpose: each one allocates durable
    # platform state (a snapshot copies a sandbox filesystem, a volume takes a
    # quota slice), so leaving them unlimited made an authenticated key able to
    # loop them for free while sandbox create was already throttled. Each has
    # its own override and, per repo convention, 0 disables it.
    snapshot_rate_limit_per_min: int = field(
        default_factory=lambda: _env_int(
            "E2B_SNAPSHOT_RATE_LIMIT_PER_MIN", DEFAULT_CREATE_RATE_LIMIT_PER_MIN
        )
    )
    volume_rate_limit_per_min: int = field(
        default_factory=lambda: _env_int(
            "E2B_VOLUME_RATE_LIMIT_PER_MIN", DEFAULT_CREATE_RATE_LIMIT_PER_MIN
        )
    )
    # E9.1: idle detection (resource-contention.md §3.1). A running sandbox
    # whose last observed activity is older than this becomes an eviction
    # candidate once the fleet is out of capacity. 0 disables idleness.
    sandbox_idle_threshold_s: int = field(
        default_factory=lambda: _env_int("E2B_SANDBOX_IDLE_THRESHOLD_S", 300)
    )
    # E9.1: activity timestamps are high-churn while the idle threshold they
    # feed is minutes wide, so the registry keeps them in memory and writes
    # through to the shared store at most this often. 0 = write every update.
    activity_persist_interval_s: int = field(
        default_factory=lambda: _env_int("E2B_ACTIVITY_PERSIST_INTERVAL_S", 30)
    )
    #: E9.1 x E9.2: pause a ``running`` sandbox whose *measured* inactivity
    #: passed this many seconds (``E2B_IDLE_PAUSE_AFTER_S``). ``0`` -- the
    #: default -- turns the sweep off inertly (no task, no claim). A paused
    #: sandbox keeps its frozen session and gives its admission reservation
    #: back; ``Sandbox.connect`` resumes it by buying the capacity back, and
    #: answers 503 while the node it is pinned to has no room.
    idle_pause_after_s: float = field(
        default_factory=lambda: _env_float("E2B_IDLE_PAUSE_AFTER_S", 0.0)
    )
    # E9.3: resource-driven eviction (resource-contention.md §5). Default ON
    # is a user decision (2026-09-01) that overrides the earlier design doc's
    # "default off" draft: when the fleet is full, idle low-priority sandboxes
    # are evicted to make room for a new create.
    eviction_enabled: bool = field(
        default_factory=lambda: _env_bool("E2B_EVICTION_ENABLED", True)
    )
    # E9.3: prefer pausing an idle victim (state preserved, reservation
    # returned) over killing it. The caller still kills a paused victim when
    # pausing alone did not make room.
    eviction_prefer_pause: bool = field(
        default_factory=lambda: _env_bool("E2B_EVICTION_PREFER_PAUSE", False)
    )
    # E9.3: how many idle sandboxes a single create request may evict at most
    # (storm bound; see E2B_EVICTION_MIN_INTERVAL_S for the temporal bound).
    eviction_max_per_create: int = field(
        default_factory=lambda: _env_int("E2B_EVICTION_MAX_PER_CREATE", 3)
    )
    # E9.3: minimum wall-clock gap between eviction rounds, per control-plane
    # process. Replicas do NOT share this throttle (known limitation, see
    # docs/resource-contention.md §8).
    eviction_min_interval_s: int = field(
        default_factory=lambda: _env_int("E2B_EVICTION_MIN_INTERVAL_S", 1)
    )
    # E9.3: how long a kill-eviction notice stays queryable so GET on an
    # evicted sandbox can explain the 404. Redis keys get this as a real TTL;
    # the in-memory fallback expires lazily and is capacity-capped.
    eviction_notice_ttl_s: int = field(
        default_factory=lambda: _env_int("E2B_EVICTION_NOTICE_TTL_S", 3600)
    )
    # E9.3: cross-tenant eviction is OFF by default (security decision): a
    # tenant key may only evict idle sandboxes of its own tenant, otherwise
    # "create a sandbox" would be a weapon to evict other tenants' sandboxes.
    # Admin keys and this switch may cross tenants.
    eviction_cross_tenant: bool = field(
        default_factory=lambda: _env_bool("E2B_EVICTION_CROSS_TENANT", False)
    )
    # E9.4: create queue (resource-contention.md §3.5/§5). When admission
    # still fails after an eviction round, wait up to this long for capacity
    # to be released before answering the original 503. 0 disables queueing
    # (today's direct-503 behavior after eviction).
    create_queue_timeout_s: float = field(
        default_factory=lambda: _env_int("E2B_CREATE_QUEUE_TIMEOUT_S", 30)
    )
    # E9.4: max number of create requests waiting for capacity at once. A
    # full queue answers 429 immediately (no ordering/fairness guarantee;
    # queue state is per replica, see docs/resource-contention.md §8).
    create_queue_max: int = field(
        default_factory=lambda: _env_int("E2B_CREATE_QUEUE_MAX", 100)
    )
    #: How long a ``DELETE`` waits for an in-flight create of the same sandbox
    #: before treating it as abandoned (design v2 §4.5). The window exists
    #: because the control plane materializes the tree *before* it dials the
    #: worker, so a teardown that arrives in between would otherwise tear down
    #: a tree a live create is still finishing onto. Bounded, and a claim that
    #: outlives the bound is abandoned rather than waited on forever.
    create_window_wait_s: float = field(
        default_factory=lambda: _env_float("E2B_CREATE_WINDOW_WAIT_S", 60.0)
    )
    volume_token_ttl_s: int = field(
        default_factory=lambda: _env_int("E2B_VOLUME_TOKEN_TTL_S", 0)
    )
    template_build_concurrency: int = field(
        default_factory=lambda: _env_int("E2B_TEMPLATE_BUILD_CONCURRENCY", 2)
    )
    template_build_rate_limit_per_min: int = field(
        default_factory=lambda: _env_int(
            "E2B_TEMPLATE_BUILD_RATE_LIMIT_PER_MIN", 0
        )
    )
    # E4.2: max bytes accepted by file-write endpoints (volumecontent PUT,
    # template archive upload). 0 disables the limit (repo convention).
    max_file_write_mb: int = field(
        default_factory=lambda: _env_int("E2B_MAX_FILE_WRITE_MB", 512)
    )
    # E5.3: sandbox metadata/envVars size caps (serialized JSON bytes) so
    # sandbox.json cannot be inflated by hostile create bodies. 0 disables
    # the limit (repo convention).
    max_metadata_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_METADATA_BYTES", 64 * 1024)
    )
    max_envvars_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_ENVVARS_BYTES", 64 * 1024)
    )
    # E5.3: cap on JSON request bodies for create/build/fork endpoints
    # (bounded read, 413 beyond the limit).
    max_json_body_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_JSON_BODY_BYTES", 256 * 1024)
    )
    # E5.3: max UTF-8 bytes for user-supplied names (template/snapshot).
    max_name_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_NAME_BYTES", 1024)
    )
    enable_network: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETWORK", False)
    )
    log_level: str = field(default_factory=lambda: os.getenv("E2B_LOG_LEVEL", "INFO"))
    internal_api_key: str = field(
        default_factory=lambda: os.getenv("E2B_INTERNAL_API_KEY", "internal-key")
    )
    # E3.6: rotation window. When E2B_INTERNAL_API_KEYS is set, every listed
    # key authenticates X-Internal-Key (old + new valid during rotation);
    # once the old key is removed from the list it stops working.
    internal_api_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_INTERNAL_API_KEYS", ())
    )
    # C3 Task 2 / N49: the near-term per-node credential (design §11.1 item 9
    # option (a)). A JSON object ``{"<key>": "<node_id>"}``; a key listed here
    # is *node-bound* -- the node-scoped internal handlers require the request's
    # self-declared node to equal this mapping and enforce the source-IP second
    # factor against the resolver. Keys absent from this map (including the
    # shared ``E2B_INTERNAL_API_KEY``) remain fleet credentials: they keep the
    # pre-C3 behavior and log an explicit degradation once (see
    # ``control_plane/api/internal.py``). Empty by default so an existing
    # deployment is untouched until it opts in.
    internal_node_keys: dict[str, str] = field(
        default_factory=lambda: _env_json_dict("E2B_INTERNAL_NODE_KEYS")
    )
    # C3 Task 2 / D4: where the *expected* node address and source IP come from.
    # ``k8s`` queries the pod API by node id (StatefulSet pod name) with the
    # mounted ServiceAccount; ``hostname`` resolves the node id as a compose
    # service name; ``auto`` picks k8s when a ServiceAccount is mounted and
    # hostname otherwise. Never learned from the request (N49).
    node_address_mode: str = field(
        default_factory=lambda: os.getenv("E2B_NODE_ADDRESS_MODE", "auto")
    )
    node_address_port: int = field(
        default_factory=lambda: _env_int("E2B_NODE_ADDRESS_PORT", 49983)
    )
    node_address_namespace: str = field(
        default_factory=lambda: os.getenv("E2B_NODE_ADDRESS_NAMESPACE", "sandlock")
    )
    # C3 Task 3 (rulings D9.2/D9.4/D9.5): the CP→agent instruction channel.
    # The agent is found the same way the node's own address is (k8s: the worker
    # pod's spec.nodeName, then the agent pod on that host by label; compose:
    # the configured service name) -- never from a request body.
    c3_agent_url: str | None = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_URL")
    )
    c3_agent_namespace: str = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_NAMESPACE", "sandlock")
    )
    c3_agent_label: str = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_LABEL", "app=c3-agent")
    )
    c3_agent_port: int = field(
        default_factory=lambda: _env_int("E2B_C3_AGENT_PORT", 49985)
    )
    #: Face B's own address (C3 Task 4 slice B, ruling D22). The two faces are
    #: two processes by necessity -- face A must be a non-root uid 65534 for
    #: the ``uid_map`` owner rule (§14.2.7), face B must be uid 0 for NFS
    #: AUTH_SYS ``chown`` and is the only one with the four roots mounted -- so
    #: they listen on **two** ports. A single address would either collide
    #: (``EADDRINUSE``: both containers share the pod netns) or route a file
    #: operation to the 65534 process, where every chown on NFS is ``EPERM``.
    #:
    #: compose names the face-B service outright (``c3-agent-maint``); k8s
    #: derives it from the same lookup face A uses -- the *same node's* agent
    #: pod, second port -- never from a request. An unset compose value is a
    #: named 503 on every file operation, not a guess.
    c3_agent_maint_url: str | None = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_MAINT_URL")
    )
    c3_agent_maint_port: int = field(
        default_factory=lambda: _env_int("E2B_C3_AGENT_MAINT_PORT", 49986)
    )
    #: The credential the agent demands on every instruction. Unset means "no
    #: agent instructions": the client refuses by name rather than dialling
    #: without auth.
    c3_agent_token: str = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_TOKEN", "")
    )
    #: One deadline per instruction. A stuck agent must never read as "the
    #: sandbox create hangs" (D9.5); the refusal is a named 504.
    c3_agent_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_C3_AGENT_TIMEOUT_S", 5.0)
    )
    #: Face B's deadline (Task 4). A teardown, a recursive chown or a tree walk
    #: is bounded by the tree, not by a syscall: ``maint.c``'s own per-verb
    #: budgets are 300 s (and its walk cap is larger), so the CP's wait has to
    #: outlast them or a legitimate operation would be abandoned mid-flight --
    #: which on this path is *not* harmless, because the instruction does not
    #: stop when the CP stops waiting.
    c3_agent_file_op_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_C3_AGENT_FILE_OP_TIMEOUT_S", 600.0)
    )
    #: The create path's materialization deadline. Much shorter than face B's
    #: 600 s on purpose: it runs *inside* a create, and the caller on the other
    #: end of that create is an SDK request the platform bounds at 60 s -- so
    #: waiting longer than that only produces a named 504 nobody is there to
    #: read, while the agent keeps copying. Defaulted to the same envelope as
    #: the worker provisioning call (``app.state.remote_http`` timeout).
    c3_agent_materialize_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_C3_AGENT_MATERIALIZE_TIMEOUT_S", 60.0)
    )
    #: How many CP→agent instructions may be in flight at once. ``0`` is
    #: unbounded; the shipped default is **64**, and the number is bounded on
    #: both sides rather than picked to taste (D16): it must be at least the
    #: largest number of outstanding slot starts one control plane can have
    #: (the autoscaler's 16-replica ceiling; the create admission cap is 100, so
    #: the semaphore must not be the first thing a create waits on), while the
    #: agent is a **synchronous** FastAPI service whose handlers run on the
    #: anyio threadpool (default 40) -- a limit above 40 buys no throughput
    #: there, and one below it would queue grants the agent could have served in
    #: parallel. 64 sits above the agent's own 40 and below the CP's 100. Slice
    #: B2 measures the N-concurrent arm against this and forces 1 to prove the
    #: queueing it must *not* show; this is the value it revises if so.
    c3_agent_max_concurrency: int = field(
        default_factory=lambda: _env_int("E2B_C3_AGENT_MAX_CONCURRENCY", 64)
    )
    # E5.4: secret-at-rest encryption. When E2B_SECRET_MASTER_KEY is unset
    # the secret registry degrades to the previous in-memory + plaintext
    # disk behavior with a startup warning and is never persisted to Redis.
    # During rotation keep the previous key in E2B_SECRET_MASTER_KEYS so
    # records encrypted with it still decrypt until every replica has
    # re-encrypted them with the primary key.
    secret_master_key: str | None = field(
        default_factory=lambda: os.getenv("E2B_SECRET_MASTER_KEY") or None
    )
    secret_master_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_SECRET_MASTER_KEYS", ())
    )
    tls_cert_file: str | None = field(
        default_factory=lambda: os.getenv("E2B_TLS_CERT")
    )
    tls_key_file: str | None = field(
        default_factory=lambda: os.getenv("E2B_TLS_KEY")
    )
    enable_local_node: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_LOCAL_NODE", True)
    )
    # ------------------------------------------------------------------
    # The worker fleet's autoscaler (``E2B_AS_*``).
    #
    # It used to be a Deployment of its own, reading this control plane over
    # ``GET /internal/fleet/metrics``; since 2026-09-30 the k8s path hosts the
    # loop *in* this app (``control_plane/autoscaler_service.py``), so its
    # knobs arrive here under the names ``deploy/k8s/autoscaler.yaml`` already
    # used. ``E2B_AS_ENABLED`` is the one switch that did not exist before: it
    # is what makes this control plane -- rather than a second pod -- the actor
    # that reconciles the worker fleet.
    #
    # The Docker pool backend went with the standalone process (the local
    # compose stack no longer autoscales), so the backend these coordinates
    # name is the cluster's worker workload and nothing else.
    # ------------------------------------------------------------------
    autoscaler_enabled: bool = field(
        default_factory=lambda: _env_bool("E2B_AS_ENABLED", False)
    )
    autoscaler_poll_s: int = field(
        default_factory=lambda: _env_int("E2B_AS_POLL_S", 5)
    )
    autoscaler_min_replicas: int = field(
        default_factory=lambda: _env_int("E2B_AS_MIN_REPLICAS", 1)
    )
    autoscaler_max_replicas: int = field(
        default_factory=lambda: _env_int("E2B_AS_MAX_REPLICAS", 16)
    )
    autoscaler_util_threshold: float = field(
        default_factory=lambda: _env_float("E2B_AS_UTIL_THRESHOLD", 0.70)
    )
    autoscaler_scale_up_cooldown_s: int = field(
        default_factory=lambda: _env_int("E2B_AS_SCALE_UP_COOLDOWN_S", 60)
    )
    autoscaler_scale_down_cooldown_s: int = field(
        default_factory=lambda: _env_int("E2B_AS_SCALE_DOWN_COOLDOWN_S", 600)
    )
    autoscaler_scale_down_util: float = field(
        default_factory=lambda: _env_float("E2B_AS_SCALE_DOWN_UTIL", 0.40)
    )
    autoscaler_node_scale_down_util: float = field(
        default_factory=lambda: _env_float("E2B_AS_NODE_SCALE_DOWN_UTIL", 0.0)
    )
    autoscaler_warmup_buffer: int = field(
        default_factory=lambda: _env_int("E2B_AS_WARMUP_BUFFER", 1)
    )
    autoscaler_k8s_namespace: str = field(
        default_factory=lambda: os.getenv("E2B_AS_K8S_NAMESPACE", "default")
    )
    #: The worker workload's name. The *kind* is not a knob any more: the repo
    #: deploys a StatefulSet (stable pod names = stable node ids, N20), and the
    #: pre-N20 Deployment branch went with `E2B_AS_K8S_KIND` on 2026-09-30.
    autoscaler_k8s_deployment: str = field(
        default_factory=lambda: os.getenv("E2B_AS_K8S_DEPLOYMENT", "e2b-worker")
    )
    redis_url: str | None = field(default_factory=lambda: os.getenv("E2B_REDIS_URL"))
    gateway_url: str | None = field(default_factory=lambda: os.getenv("E2B_GATEWAY_URL"))
    buildkit_addr: str = field(
        default_factory=lambda: os.getenv(
            "E2B_BUILDKIT_ADDR", "unix:///run/buildkit/buildkitd.sock"
        )
    )
    shared_workspace_root: str | None = field(
        default_factory=lambda: os.getenv("E2B_SHARED_WORKSPACE_ROOT")
    )
    image_registry: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY")
    )
    image_registry_username: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY_USERNAME")
    )
    image_registry_password: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY_PASSWORD")
    )
    # Pull credentials belong to one registry host; public images are pulled
    # anonymously (see ``oci_registry.RegistryClient``). The worker needs the
    # host too, not just the user/password pair.
    image_registry: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY")
    )

    @property
    def image_registry_host(self) -> str | None:
        """Host the ``image_registry_*`` credentials are meant for."""
        return registry_host(self.image_registry)
    shared_volume_root: str | None = field(
        default_factory=lambda: os.getenv("E2B_SHARED_VOLUME_ROOT")
    )
    #: N73: the control plane's volume **store** root, named outright. Before
    #: this variable existed the control plane read ``E2B_SHARED_VOLUME_ROOT``
    #: as the store root *itself* -- but that name means the shared *export*
    #: root everywhere else (worker, agent: ``_snapshots``/``_migrate``/
    #: ``_volumes`` sit below it), and on k8s the export root is mounted
    #: read-only, so a control plane that set it built failed volumes
    #: (0.1.0-943, ``POST /volumes`` 500/``EROFS``). This variable is the one
    #: authoritative store root; when it is unset the shared root is only a
    #: judge to derive ``<shared_volume_root>/"_volumes"`` from. Unset *and*
    #: no shared root keeps the pre-N73 default ``<platform_root>/"_volumes"``.
    volume_store_root: str | None = field(
        default_factory=lambda: os.getenv("E2B_VOLUME_STORE_ROOT")
    )
    #: The reslice's named judge: whether the sandbox **trees** live on shared
    #: storage. Deliberately *not* ``bool(shared_workspace_root)`` -- after the
    #: reslice the shared root is still named (it holds ``_snapshots``,
    #: ``_migrate``, ``_volumes``, …) while the trees sit on node-local disk, so
    #: deriving this from that would answer "shared" for a deployment whose
    #: trees are not. One variable decides three things on the migration path
    #: (export or not, write the tree or not, keep the source's tree or not), and
    #: reading the wrong one there builds an *empty* tree on the target and
    #: leaves the source's tree behind unreclaimed.
    #:
    #: Unset, or empty (which is what the environment reads an unset variable as
    #: -- the same rule :func:`gateway_common.paths.resolve_state_base` states),
    #: keeps today's behaviour: the trees are shared exactly when a shared
    #: workspace root is named.
    trees_shared: bool | None = field(default_factory=_trees_shared_from_env)
    #: Task 3: the byte cap for one tree copy through the control plane (the
    #: migration archive it streams from a source node into a target one). A
    #: tree is copied *through* this process, so the cap is what keeps "one
    #: 1 GiB tree = one 2 GiB control plane" from being possible; crossing it
    #: is the named refusal ``tree-copy-too-large`` (413), never a truncated
    #: copy. ``0`` disables it. The default (1.25 GiB) is one sandbox's own
    #: quota (``E2B_DEFAULT_DISK_MB`` = 1 GiB) plus the archive's own overhead:
    #: a cap at exactly the quota would refuse the copy of a sandbox that is
    #: merely full, which is the one case the cap must not catch
    #: (``docs/create-local-first-design.md`` §3.1).
    tree_copy_max_bytes: int = field(
        default_factory=lambda: _env_int(
            "E2B_TREE_COPY_MAX_BYTES", DEFAULT_TREE_COPY_MAX_BYTES
        )
    )
    #: ...and how many bytes are streamed before that stretch of page cache is
    #: dropped (``POSIX_FADV_DONTNEED``). ``0`` disables the windowed drop.
    tree_copy_window_bytes: int = field(
        default_factory=lambda: _env_int(
            "E2B_TREE_COPY_WINDOW_BYTES", 64 * 1024 * 1024
        )
    )
    #: Node-local platform state (``E2B_NODE_STATE_BASE``) -- the create marker,
    #: the disk-stat seed, ``.route-b`` and the uid pool's local files. Named on
    #: the control plane because :meth:`ControlPaths.roots` is the list the
    #: privileged helper is asked to act under, and that list has to carry every
    #: root an op can name. Unset = no such root (today's shape).
    node_state_base: str | None = field(
        default_factory=lambda: os.getenv("E2B_NODE_STATE_BASE")
    )
    #: Route B's scratch root -- where the per-slot ``policy.json`` /
    #: ``program.json`` documents live, and therefore the directory the C3
    #: agent has to scope to each slot's uid (C3 Task 4's
    #: ``scope-slot-document``). The *worker* names the same root with the same
    #: variable (``E2B_ROUTE_B_TMP_ROOT``); the control plane has to know it
    #: too, because the worker may not report a path (hard rule 3 / §14.4).
    #: Unset means "this control plane cannot derive a slot document's path",
    #: and that op then fails closed by name rather than guessing the worker's
    #: layout -- the shipped manifests set it (Task 4 slice B).
    route_b_tmp_root: str = field(
        default_factory=lambda: os.getenv("E2B_ROUTE_B_TMP_ROOT", "")
    )
    # Tenant isolation (E3.1). When E2B_TENANTS is unset the control plane
    # runs in single-tenant compatible mode: all keys share every resource
    # and resources are created with tenant_id=None.
    tenant_map: dict[str, list[str]] = field(
        default_factory=lambda: _env_json("E2B_TENANTS", {}) or {}
    )
    admin_api_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_ADMIN_API_KEYS", ())
    )
    tenant_limits: dict[str, dict[str, int]] = field(
        default_factory=lambda: _env_json("E2B_TENANT_LIMITS", {}) or {}
    )
    tenant_rate_limits: dict[str, int] = field(
        default_factory=lambda: _env_json("E2B_TENANT_RATE_LIMITS", {}) or {}
    )

    def __post_init__(self) -> None:
        """Give ``state_base`` its default: *this* object's workspace base.

        No second base unless ``E2B_STATE_BASE`` names one. A caller that passes
        ``workspace_base`` explicitly (a test, an embedder, a combined
        deployment) must not end up with the platform's record and checkpoint
        directories under some other base the environment happened to name:
        that split is silent, and it makes every "same base?" answer wrong.
        """
        if self.state_base is None:
            self.state_base = self.workspace_base
        # N83 phase 2 (D5): the per-sandbox ceiling is resolved **here**, once,
        # so every reader (the in-process ``local://`` node's record, a default
        # view, a test) sees the same positive number instead of a raw env
        # value that somebody else still has to interpret. An explicit value
        # wins; `0`/unset follows the node's own total; a node that declared no
        # total follows the per-sandbox create default -- never 0 (unlimited).
        self.max_sandbox_cpu_percent = resolve_sandbox_ceiling(
            configured=self.max_sandbox_cpu_percent,
            node_total=self.max_total_cpu_percent,
            create_default=self.default_cpu_percent,
        )
        self.max_sandbox_memory_mb = resolve_sandbox_ceiling(
            configured=self.max_sandbox_memory_mb,
            node_total=self.max_total_memory_mb,
            create_default=self.default_memory_mb,
        )
        self.max_sandbox_processes = resolve_sandbox_ceiling(
            configured=self.max_sandbox_processes,
            node_total=self.max_total_processes,
            create_default=self.default_max_processes,
        )
        # N57: the trees' shared/not question, settled once. ``None`` means the
        # deployment did not name it, so it keeps the old answer -- which is
        # what makes the reslice a no-op for every deployment that has not been
        # re-deployed. A named value wins outright, which is the whole point:
        # after the reslice the shared root is *still* named while the trees are
        # not, so the old question can no longer be asked.
        if self.trees_shared is None:
            self.trees_shared = bool(self.shared_workspace_root)
        normalized: dict[str, list[str]] = {}
        for tenant, keys in (self.tenant_map or {}).items():
            if not isinstance(keys, list):
                raise ValueError(
                    f"E2B_TENANTS[{tenant!r}] must be a list of API keys"
                )
            normalized[str(tenant)] = [str(k) for k in keys]
        self.tenant_map = normalized
        self.tenant_limits = {
            str(t): {str(k): int(v) for k, v in limits.items()}
            for t, limits in (self.tenant_limits or {}).items()
        }
        self.tenant_rate_limits = {
            str(t): int(v) for t, v in (self.tenant_rate_limits or {}).items()
        }

    @property
    def all_api_keys(self) -> tuple[str, ...]:
        keys = list(self.api_keys)
        if self.api_key:
            keys.append(self.api_key)
        keys.extend(self.admin_api_keys)
        for tenant_keys in self.tenant_map.values():
            keys.extend(tenant_keys)
        return tuple(dict.fromkeys(keys))

    @property
    def all_internal_api_keys(self) -> tuple[str, ...]:
        """Active X-Internal-Key credentials (list first, single fallback).

        C3 Task 2: the per-node keys (``E2B_INTERNAL_NODE_KEYS``) authenticate
        ``X-Internal-Key`` like every other internal credential -- they are the
        same mechanism, only *additionally* bound to a node (see
        ``control_plane.auth.node_id_for_key``). Leaving them out here would
        make a node-bound worker 401 before the binding was ever consulted.
        """
        keys = list(self.internal_api_keys)
        if self.internal_api_key:
            keys.append(self.internal_api_key)
        keys.extend(self.internal_node_keys)
        return tuple(dict.fromkeys(keys))

    @property
    def tenants_enabled(self) -> bool:
        """True when tenant isolation is configured (E2B_TENANTS non-empty)."""
        return bool(self.tenant_map)

    def tenant_of_key(self, key: str) -> str | None:
        """Map an API key to its tenant, ``None`` when unassigned."""
        for tenant, keys in self.tenant_map.items():
            if key in keys:
                return tenant
        return None

    @property
    def tls_enabled(self) -> bool:
        """HTTPS is on only when both cert and key are configured."""
        return bool(self.tls_cert_file and self.tls_key_file)

    def resolve_template_image(self, template_id: str) -> str | None:
        """Resolve ``templateID`` to a base image, ``None`` for pure Sandlock."""
        if template_id in self.template_images:
            return self.template_images[template_id]
        if template_id == "mcp-gateway":
            return self.base_image
        if template_id == "base":
            return self.base_image
        return None


def uvicorn_ssl_kwargs(settings: Settings) -> dict[str, str]:
    """uvicorn.run kwargs enabling HTTPS; empty dict keeps plain HTTP.

    Both ``E2B_TLS_CERT`` and ``E2B_TLS_KEY`` must be set together; a
    half-configured pair is a deployment error, not a silent HTTP fallback.
    """
    cert_file = settings.tls_cert_file
    key_file = settings.tls_key_file
    if (cert_file is None) != (key_file is None):
        raise ValueError("E2B_TLS_CERT and E2B_TLS_KEY must be set together")
    if cert_file is None:
        return {}
    return {"ssl_certfile": cert_file, "ssl_keyfile": key_file}


def configure_logging(settings: Settings) -> int:
    """Make the control plane's own INFO logging visible (N27).

    ``uvicorn.run(log_level=...)`` only configures the ``uvicorn*`` loggers;
    ``control_plane.*`` inherits the root logger, whose default WARNING level
    drops every INFO line -- including the startup ``workspace base = ...`` /
    ``platform state base = ...`` pair an operator reconciles against the
    worker's, and any ``E2B_LOG_LEVEL=DEBUG``. This is the same arrangement the
    worker's entry point has (``envd_service.__main__._configure_logging``);
    the control plane needs it for the same reason.

    ``logging.basicConfig`` is a no-op when the root logger already has handlers
    (e.g. under pytest), so the level is pinned explicitly as well. Returns the
    numeric level applied.
    """
    # An unknown name falls back to INFO (getattr's default) instead of raising
    # the way ``basicConfig(level="BOGUS")`` would.
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s:%(name)s:%(message)s")
    logging.getLogger().setLevel(level)
    return level


def local_node_quota_via_agent() -> bool:
    """Whether a combined ("合体") node must route quota through the agent.

    ``E2B_ENABLE_LOCAL_NODE`` (default true) makes the control plane provision
    sandboxes in its own process, and its *volume* quota is provisioned here
    (``control_plane/api/sandboxes.py``) while the workspace/GC half belongs
    to the envd service. One deployment must answer that switch the same way
    on both halves -- ``E2B_QUOTA_AGENT_URL`` present, else
    ``E2B_QUOTA_VIA_AGENT`` (default false) -- so this asks the envd service
    for the very function its own ``Settings.quota_via_agent`` is built from
    instead of re-deriving the rule. It matters in this shape: the merged
    image runs non-root (no ``xfs_quota``, no ``CAP_SYS_ADMIN``) and a volume
    may live on an NFS mount, where quota is server-side.

    A separated control plane (``E2B_ENABLE_LOCAL_NODE=false``) provisions no
    quota itself and has no envd service to ask; ``False`` is then the
    accurate answer for every caller.
    """
    try:
        from envd_service.config import _quota_via_agent_from_env
    except ImportError:  # pragma: no cover - separated control-plane image
        return False
    return _quota_via_agent_from_env()
