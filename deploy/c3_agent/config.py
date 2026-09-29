"""Configuration for the C3 per-node agent (``deploy/c3_agent``).

Same shape as ``deploy/quota_agent/config.py``: a tiny dataclass reading env,
refusing to run without its token (``app``/``__main__`` enforce that), and with
no state beyond what the process was configured with -- the agent is
**stateless by construction** (C3 §1: no authorization table, no TTL, no
"push before send" ordering; every parameter, the uid included, arrives in the
control plane's instruction).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from gateway_common.env import _env_bool, _env_float, _env_int


@dataclass
class Settings:
    #: The CP→agent credential (``X-Internal-Key`` on this service). The
    #: agent→CP direction keeps the existing internal-key pattern; this is the
    #: other half of "双向认证" -- both directions authenticated, no new PKI.
    token: str = field(default_factory=lambda: os.getenv("E2B_C3_AGENT_TOKEN", ""))
    #: The node this agent is (the only local decision it makes: "addressed to
    #: me?"). Under D12 this is the **host's** name, not a worker pod name: the
    #: DaemonSet sets the C3-specific variable from `spec.nodeName`, and a
    #: Compose shape sets it to the service name the control plane dials.
    #: `E2B_NODE_ID` is only a fallback for an embedder that has nothing else
    #: (in the shipped manifests the worker's own `E2B_NODE_ID` is its pod
    #: name, which is a *different* fact from this one).
    node_id: str = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_NODE_ID")
        or os.getenv("E2B_NODE_ID", "")
    )
    #: Listen address. ``0.0.0.0`` by default because the control plane dials
    #: this service from another pod; what must bound the reach is a
    #: NetworkPolicy allowing only CP→agent (Task 3's DaemonSet), not a loopback
    #: bind that would just break the channel. Kept configurable so a shape with
    #: a local sidecar can bind deliberately.
    host: str = field(
        default_factory=lambda: os.getenv("E2B_C3_AGENT_HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: _env_int("E2B_C3_AGENT_PORT", 49985)
    )
    #: The Task 1 primitive. Face A is the only shipped payload of the agent
    #: image besides ``e2b-maint`` (face B, wired by a later task).
    as_uid_path: str = field(
        default_factory=lambda: os.getenv(
            "E2B_C3_AGENT_AS_UID", "/var/lib/e2b-priv/as_uid"
        )
    )
    #: How long one ``as_uid`` invocation may take. The primitive is a single
    #: ``write(2)`` into ``/proc/<pid>/uid_map``, so this only bounds a hung
    #: target; it is a timeout, not a retry budget (a retry is safe -- the
    #: primitive refuses an already-written map -- but Task 3 owns delivery).
    as_uid_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("E2B_C3_AGENT_AS_UID_TIMEOUT_S", "5"))
    )
    #: Face B's payload, ``e2b-maint`` (C3 Task 4). The agent image installs it
    #: beside ``as_uid`` (``/var/lib/e2b-priv``), and the DaemonSet gives this
    #: container the same four roots the worker's broker had -- the path
    #: discipline itself stays in ``priv_common.c``, so nothing here resolves a
    #: path; these values are only ``maint.c``'s own inputs.
    maint_path: str = field(
        default_factory=lambda: os.getenv(
            "E2B_C3_AGENT_MAINT", "/var/lib/e2b-priv/e2b-maint"
        )
    )
    #: How long one ``e2b-maint`` invocation may take. ``rm``/``chown`` of a
    #: sandbox tree is bounded by the tree, not by this number; it is the
    #: agent's own ceiling so a wedged walk cannot pin a handler forever.
    maint_timeout_s: float = field(
        default_factory=lambda: float(
            os.getenv("E2B_C3_AGENT_MAINT_TIMEOUT_S", "300")
        )
    )
    #: Ruling **D25** removed the identity resolver's uid switch (and with it
    #: ``E2B_C3_AGENT_RESOLVER_UID/_GID/_TIMEOUT_S``): the file-operation
    #: anchor is the worker's container id, matched against the
    #: world-readable ``/proc/<pid>/cgroup``, so face B reads it as itself --
    #: root, three capabilities, no ``CAP_SYS_PTRACE``, no uid change. The
    #: knobs existed only because ``ns/pid`` needs the same uid; nothing on
    #: this path does any more.
    #: The whitelist roots and the uid pool, exactly as ``priv_common.c`` reads
    #: them. Defaults mirror ``envd_service/config.py`` / the C defaults, so an
    #: agent started without the DaemonSet's env is still the same shape.
    workspace_base: str = field(
        default_factory=lambda: os.getenv(
            "E2B_WORKSPACE_BASE", "/var/lib/e2b-sandboxes"
        )
    )
    #: Empty means "the workspace base itself" (``priv_state_base``'s rule); it
    #: is written into the child's environment *resolved*, never empty.
    state_base: str = field(default_factory=lambda: os.getenv("E2B_STATE_BASE", ""))
    shared_volume_root: str = field(
        default_factory=lambda: os.getenv("E2B_SHARED_VOLUME_ROOT", "")
    )
    image_cache_dir: str = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_CACHE_DIR", "")
    )
    uid_pool_start: int = field(
        default_factory=lambda: _env_int("E2B_UID_POOL_START", 10000)
    )
    uid_pool_size: int = field(
        default_factory=lambda: _env_int("E2B_UID_POOL_SIZE", 1000)
    )
    #: C3 Task 6: where the control plane answers agent reports. The same
    #: variable the workers and the gateway use, on purpose -- one name for
    #: "the control plane of this deployment", so the two directions cannot
    #: drift apart. Empty means "this container does not report" (face A never
    #: does: the scan needs the shared workspace mount, which face B carries),
    #: and a container that asks for the scan without it says so by name.
    control_plane_url: str = field(
        default_factory=lambda: os.getenv("E2B_CONTROL_PLANE_URL", "").rstrip("/")
    )
    #: The inventory scan ("the agent is the eyes", C3 §11.1 item 5 / Task 6).
    #: Off unless the deployment names it, because only the face that mounts
    #: the workspaces can scan them; the shipped manifests turn it on there
    #: (``deploy/k8s/c3-agent.yaml`` face B, and the compose/stack agent
    #: services) rather than leaving the shape to degrade quietly.
    scan_enabled: bool = field(
        default_factory=lambda: _env_bool("E2B_C3_AGENT_SCAN", False)
    )
    #: First scan this long after start, then one every interval. The brief's
    #: "worker crashed and never restarts ⇒ the disk converges within N
    #: minutes" is these two numbers: 30 s + 120 s ⇒ **2–3 minutes**.
    scan_initial_delay_s: float = field(
        default_factory=lambda: _env_float("E2B_C3_AGENT_SCAN_INITIAL_DELAY_S", 30.0)
    )
    scan_interval_s: float = field(
        default_factory=lambda: _env_float("E2B_C3_AGENT_SCAN_INTERVAL_S", 120.0)
    )
    #: A round the control plane *deferred* (its records could not certify the
    #: fleet) is retried on a doubling schedule, capped here: never a
    #: per-interval poll of the whole fleet, never silent.
    scan_backoff_max_s: float = field(
        default_factory=lambda: _env_float("E2B_C3_AGENT_SCAN_BACKOFF_MAX_S", 600.0)
    )
    #: One report's own deadline. The scan itself is a directory read; this
    #: bounds the hop so a wedged control plane cannot pin the agent's loop.
    report_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_C3_AGENT_REPORT_TIMEOUT_S", 10.0)
    )
