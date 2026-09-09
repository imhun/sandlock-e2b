"""Envd service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from gateway_common.env import registry_host
from gateway_common.env import (
    _env_bool,
    _env_float,
    _env_int,
    _env_json_dict,
    _env_list,
)

# Default private-egress denylist applied to the implicit full-egress branch
# (no explicit allowOut/denyOut + internet allowed). Covers RFC1918, loopback,
# link-local / cloud metadata, and ULA. Override with E2B_NETWORK_DENY_CIDRS;
# an explicit empty value disables the protection.
DEFAULT_NETWORK_DENY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "fd00::/8",
)


def _network_deny_cidrs() -> tuple[str, ...]:
    value = os.getenv("E2B_NETWORK_DENY_CIDRS")
    if value is None:
        return DEFAULT_NETWORK_DENY_CIDRS
    if value.strip() == "":
        return ()
    return tuple(p.strip() for p in value.split(",") if p.strip())


@dataclass
class Settings:
    envd_port: int = field(default_factory=lambda: _env_int("E2B_ENVD_PORT", 49983))
    workspace_base: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_WORKSPACE_BASE", "tmp/sandboxes")
        ).resolve()
    )
    executor: str = field(
        default_factory=lambda: os.getenv("E2B_EXECUTOR", "auto").lower()
    )
    base_image: str | None = field(default_factory=lambda: os.getenv("E2B_BASE_IMAGE"))
    template_images: dict[str, str] = field(
        default_factory=lambda: _env_json_dict("E2B_TEMPLATE_IMAGES")
    )
    enable_network: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETWORK", False)
    )
    enable_netns: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETNS", False)
    )
    # E7.2: per-sandbox network isolation (S2.2 `net_isolation`): each
    # sandlock sandbox spawns in its own network namespace (loopback only)
    # and all egress is mediated by the supervisor. Default off keeps the
    # shared-netns path (zero regression); when enabled, outbound connects
    # need `fd_inject_connect` and inbound listeners need `port_mappings`.
    enable_net_isolation: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NET_ISOLATION", False)
    )
    # E7.2: sandlock connect fd-injection switch (S2.1). The supervisor
    # performs the connect on a host-side socket and injects the connected fd
    # at the child's own socket fd, so the trapped connect() returns 0 and
    # CPython's socket.connect() keeps working. Default off (legacy on-behalf
    # connect); `net_isolation` without it is a loopback-only sandbox.
    fd_inject_connect: bool = field(
        default_factory=lambda: _env_bool("E2B_FD_INJECT_CONNECT", False)
    )
    # E7.2: inbound port mappings {host_port: sandbox_port} (S2.5), JSON.
    # Host ports live in the reserved 50005+ range. The MCP gateway path adds
    # its per-sandbox port automatically when net_isolation is enabled.
    port_mappings: dict[str, str] = field(
        default_factory=lambda: _env_json_dict("E2B_PORT_MAPPINGS")
    )
    network_deny_cidrs: tuple[str, ...] = field(
        default_factory=_network_deny_cidrs
    )
    sandbox_notify_rate_limit: int = field(
        default_factory=lambda: _env_int("E2B_SANDBOX_NOTIFY_RATE_LIMIT", 5000)
    )
    iam_signing_key: str = field(
        default_factory=lambda: os.getenv(
            "E2B_IAM_SIGNING_KEY", "e2b-sandlock-local-iam-key"
        )
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
    quota_via_agent: bool = field(
        default_factory=lambda: _env_bool("E2B_QUOTA_VIA_AGENT", False)
    )
    # NFS form (E2.6): server-side quota-agent URL/token. Wired by
    # envd_service.quota_agent.configure_quota_agent_client when
    # quota_via_agent is enabled; missing values degrade quota with warnings.
    quota_agent_url: str | None = field(
        default_factory=lambda: os.getenv("E2B_QUOTA_AGENT_URL") or None
    )
    quota_agent_token: str | None = field(
        default_factory=lambda: os.getenv("E2B_QUOTA_AGENT_TOKEN") or None
    )
    quota_agent_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_QUOTA_AGENT_TIMEOUT_S", 5.0)
    )
    default_max_processes: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MAX_PROCESSES", 256)
    )
    default_max_open_files: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MAX_OPEN_FILES", 4096)
    )
    # Per-sandbox command serialization (E2.3): max commands that may run
    # concurrently for one sandbox (default 1 = strictly serial); commands
    # beyond the limit queue.
    max_concurrent_commands_per_sandbox: int = field(
        default_factory=lambda: _env_int(
            "E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX", 1
        )
    )
    # Max commands allowed to wait for a slot; beyond this a new command is
    # rejected with 429. None (default) = max_concurrent_commands_per_sandbox.
    max_queued_commands_per_sandbox: int | None = field(
        default_factory=lambda: (
            None
            if os.getenv("E2B_MAX_QUEUED_COMMANDS_PER_SANDBOX") in (None, "")
            else _env_int("E2B_MAX_QUEUED_COMMANDS_PER_SANDBOX", 0)
        )
    )
    # Max seconds a command may wait in the per-sandbox queue before it is
    # rejected with 429 (resource_exhausted).
    command_queue_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_COMMAND_QUEUE_TIMEOUT_S", 30)
    )
    # E4.1: per-stream command output capture cap (MiB). 0 disables the cap
    # (unlimited, matching repo convention); the default keeps replays and
    # command-log merging bounded against ``cat /dev/zero`` style output.
    command_capture_limit_mb: int = field(
        default_factory=lambda: _env_int("E2B_COMMAND_CAPTURE_LIMIT_MB", 10)
    )
    # E4.2: max bytes accepted by the worker ``/files`` write endpoint.
    # 0 disables the limit (repo convention).
    max_file_write_mb: int = field(
        default_factory=lambda: _env_int("E2B_MAX_FILE_WRITE_MB", 512)
    )
    # Per-sandbox host uid isolation (E3.2): when enabled (and the worker
    # runs as root / CAP_SETUID), every sandbox gets a distinct host uid
    # from the pool and its workspace is chowned to that uid with 0700.
    # Non-root workers cannot map arbitrary host uids (S1.2 fail-closed), so
    # they degrade to the fixed worker identity + Landlock.
    per_sandbox_uid: bool = field(
        default_factory=lambda: _env_bool("E2B_PER_SANDBOX_UID", False)
    )
    # Host uid pool range (10000+i by default, away from image uids like
    # 1000). Workers sharing one workspace must use disjoint ranges.
    uid_pool_start: int = field(
        default_factory=lambda: _env_int("E2B_UID_POOL_START", 10000)
    )
    uid_pool_size: int = field(
        default_factory=lambda: _env_int("E2B_UID_POOL_SIZE", 1000)
    )
    uid_reconcile_on_startup: bool = field(
        default_factory=lambda: _env_bool("E2B_UID_RECONCILE_ON_STARTUP", True)
    )
    # Route B (backlog #5 / T5): run each sandbox's path mediator as its own
    # ``sandlock-supervise`` process whose euid IS the sandbox host uid, so
    # mediated (``fs_denied`` carve-out) writes are owned by the sandbox and
    # 1777+sticky per-uid volume protection holds. ``auto`` starts a slot for
    # every sandbox that has its own host uid *and* the chroot (image-rootfs)
    # policy -- the only shape where mediation is active; ``on`` also covers
    # the pure shape; ``off`` keeps the in-process ``SandboxInstance``.
    # Starting a slot at another uid needs a privileged starter, so a non-root
    # worker stays on the in-process path in ``auto``/``off`` and fails loudly
    # in ``on`` (route A vs route B is a deployment decision, never a silent
    # downgrade -- docs/supervise-identity-handoff.md §8).
    route_b: str = field(
        default_factory=lambda: os.getenv("E2B_ROUTE_B", "auto").lower()
    )
    # Maximum live slots. W1 recycle semantics: one uid = one supervise
    # process = one sandbox generation, and reuse means restarting in place,
    # so the uid-reuse window is the number of concurrently live slots
    # (docs/superpowers/plans/2026-09-09-envd-route-b-wiring.md). ``0`` means
    # "no extra cap": the per-sandbox host uid pool bounds concurrency by
    # construction, and ``E2B_ROUTE_B_SLOTS>0`` is also an explicit opt-in for
    # shapes ``auto`` would leave on the in-process path.
    route_b_slots: int = field(
        default_factory=lambda: _env_int("E2B_ROUTE_B_SLOTS", 0)
    )
    # Scratch root for the per-slot policy/program documents (never the
    # channel path: the registered socket lives in the fork's per-uid
    # registry, /tmp/sandlock-ctl-<uid>-registry).
    # Slot control transport: ``fd`` (default) hands the slot a descriptor
    # from the worker's own socketpair, so neither a registry path nor a
    # channel token ever appears in the slot's argv (world-readable
    # /proc/<pid>/cmdline); ``path`` attaches to a registered slot started by
    # an external fleet.
    route_b_transport: str = field(
        default_factory=lambda: os.getenv("E2B_ROUTE_B_TRANSPORT", "fd").lower()
    )
    # Per-verb response deadline on the slot channel, in seconds. One verb
    # that outlives it retires the session and the executor restarts the slot
    # once, so this is the "a wedged generation must not hang the worker"
    # bound -- not a command timeout (that is E2B_MAX_COMMAND_TIMEOUT_S).
    route_b_verb_timeout_s: float = field(
        default_factory=lambda: _env_float("E2B_ROUTE_B_VERB_TIMEOUT_S", 15.0)
    )
    route_b_tmp_root: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_ROUTE_B_TMP_ROOT", "/tmp/sandlock-route-b")
        ).resolve()
    )
    # Quota maintenance (E2.4): periodic over-limit + disk watermark scans and
    # startup orphan project reconciliation.
    quota_monitor_interval_s: float = field(
        default_factory=lambda: _env_float("E2B_QUOTA_MONITOR_INTERVAL_S", 60)
    )
    quota_warn_ratio: float = field(
        default_factory=lambda: _env_float("E2B_QUOTA_WARN_RATIO", 0.9)
    )
    disk_warn_ratio: float = field(
        default_factory=lambda: _env_float("E2B_DISK_WARN_RATIO", 0.9)
    )
    disk_error_ratio: float = field(
        default_factory=lambda: _env_float("E2B_DISK_ERROR_RATIO", 0.98)
    )
    quota_reconcile_on_startup: bool = field(
        default_factory=lambda: _env_bool("E2B_QUOTA_RECONCILE_ON_STARTUP", True)
    )
    log_level: str = field(default_factory=lambda: os.getenv("E2B_LOG_LEVEL", "INFO"))
    internal_api_key: str = field(
        default_factory=lambda: os.getenv("E2B_INTERNAL_API_KEY", "internal-key")
    )
    # E3.6: rotation window (see control_plane/config.py). Workers accept
    # every key in E2B_INTERNAL_API_KEYS while the list is populated.
    internal_api_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_INTERNAL_API_KEYS", ())
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
    image_cache_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_IMAGE_CACHE_DIR", "tmp/sandboxes/_images")
        ).resolve()
    )

    @property
    def all_internal_api_keys(self) -> tuple[str, ...]:
        """Active X-Internal-Key credentials (list first, single fallback)."""
        keys = list(self.internal_api_keys)
        if self.internal_api_key:
            keys.append(self.internal_api_key)
        return tuple(dict.fromkeys(keys))
