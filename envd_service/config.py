"""Envd service configuration."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from functools import lru_cache
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

logger = logging.getLogger(__name__)


def _image_cache_dir() -> Path:
    """``E2B_IMAGE_CACHE_DIR`` (or the legacy relative default).

    When the operator *configures* the shared cache, prepare it here -- before
    any process writes into it, and with the worker's ownership -- because the
    control plane (root) exports template layout tars into ``_images/_oci/``
    without going through the resolver. A root-owned ``_images`` left behind by
    that write is exactly what locks the 65534 workers out of the whole cache
    (no lock file, no staging tree, every resolve fails), and it is also what
    heals a directory an older deployment left ``root:root``. Left unset,
    nothing is created or changed: local development stays all-one-uid.
    See docs/production-deployment-requirements.md §2.7.1.
    """
    raw = os.getenv("E2B_IMAGE_CACHE_DIR")
    path = Path(raw if raw else "tmp/sandboxes/_images")
    if raw:
        try:
            from envd_service.runtime.image_resolver import ensure_shared_cache_dir

            ensure_shared_cache_dir(path)
        except Exception as e:  # noqa: BLE001 - never block startup on this
            logger.warning(
                "could not prepare the shared image cache %s: %s", path, e
            )
    return path.resolve()

# Default private-egress denylist applied to the implicit full-egress branch
# (no explicit allowOut/denyOut + internet allowed). Covers RFC1918, loopback,
# link-local / cloud metadata, CGNAT, multicast/reserved, and ULA. Override
# with E2B_NETWORK_DENY_CIDRS; an explicit empty value disables the protection.
#
# This list is the *only* egress control there is: the fork performs outbound
# connects on the sandbox's behalf **in the worker's network namespace**
# (``fd_inject_connect``), so a per-sandbox netns does not constrain a
# destination -- the rules do. Two families of spelling were missing and each
# reached worker-local services (SEC-001, 2026-09-16):
#
# * ``0.0.0.0/8`` -- ``connect(0.0.0.0)`` is INADDR_ANY, which Linux routes to
#   the local host, so ``tcp://0.0.0.0:49983`` reached the worker's own envd
#   even though ``127.0.0.0/8`` was denied. ``0.0.0.1`` behaves the same way.
# * ``::1/128`` (and ``::/128``) -- IPv6 loopback had no rule at all; the
#   v4-mapped form is already folded to IPv4 by the fork's
#   ``to_canonical()``, but the plain v6 spelling is not.
#
# ``fe80::/10`` (IPv6 link-local) and the multicast/reserved ranges are listed
# for the same reason the v4 ones are: metadata services and neighbours live
# there, and ``IpCidr`` refuses cross-family matches, so each family needs its
# own entry. ``::ffff:0:0/96`` is belt-and-braces for any future path that
# skips ``to_canonical()``.
DEFAULT_NETWORK_DENY_CIDRS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "::ffff:0:0/96",
    "fe80::/10",
    "fd00::/8",
    "ff00::/8",
)


def _network_deny_cidrs() -> tuple[str, ...]:
    value = os.getenv("E2B_NETWORK_DENY_CIDRS")
    if value is None:
        return DEFAULT_NETWORK_DENY_CIDRS
    if value.strip() == "":
        return ()
    return tuple(p.strip() for p in value.split(",") if p.strip())


def _quota_via_agent_from_env() -> bool:
    """Whether the worker must route quota operations through quota-agent.

    ``E2B_QUOTA_AGENT_URL`` is the switch (E2.6 + A6): a deployment that
    configures an agent address runs the agent form, which is what lets the
    worker drop ``CAP_SYS_ADMIN`` (``xfs_quota -x`` runs on the agent side).
    ``E2B_QUOTA_VIA_AGENT=true`` without a URL stays a supported
    misconfiguration: it wires no hooks and degrades with a warning.
    """
    if os.getenv("E2B_QUOTA_AGENT_URL", "").strip():
        return True
    return _env_bool("E2B_QUOTA_VIA_AGENT", False)


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
    # S2.5 bind injection (fork `net_bind_inject`): the mapped inbound port is
    # answered by replacing the sandbox's socket with a supervisor-created
    # host-loopback one at `bind()` time, so the sandbox `listen()`s/`accept()`s
    # on a real host-netns socket and the supervisor never traps
    # `poll`/`ppoll`/`epoll_wait` for readiness synthesis. Measured on the
    # deployment (2026-09-16): an MCP request cost ~390 ms per request with the
    # host-listener mapping and ~8 ms with injection. Requires
    # `port_mappings` + `net_isolation`; setting it without the mapping is
    # refused by the fork's own validation (fail closed).
    net_bind_inject: bool = field(
        default_factory=lambda: _env_bool("E2B_NET_BIND_INJECT", True)
    )
    # A7 follow-up (2026-09-16): the worker must actually run under the shipped
    # seccomp profile. `Seccomp: 0` in /proc/self/status means the container is
    # unfiltered (the profile was dropped or overridden with `unconfined`), and
    # the sandboxes then inherit the worker's whole syscall surface -- the exact
    # regression A7 removed, and one that otherwise shows up nowhere. Test
    # runners and the autoscaler's local backend run unfiltered on purpose and
    # set this to 0.
    require_seccomp_filter: bool = field(
        default_factory=lambda: _env_bool("E2B_REQUIRE_SECCOMP_FILTER", True)
    )
    # Per-sandbox PID namespace (fork `pid_ns`, S2.2's sibling): the sandbox is
    # the first process of its own PID namespace, so host/other-sandbox pids are
    # invisible from inside and the sandbox gets a real pid-1 reaper. Off by
    # default: the fork's self-map shape (route-B: guest uid 0 while the host uid
    # stays the slot uid) is not implemented on the pid-ns path yet, so turning
    # this on today costs the guest its root identity -- see
    # docs/production-deployment-requirements.md §2.4.10.
    pid_ns: bool = field(default_factory=lambda: _env_bool("E2B_PID_NS", False))
    # E7.2: the pairing guard's escape hatch. `net_isolation` without
    # `fd_inject_connect` yields a loopback-only sandbox: every external
    # connect fails at the kernel (no route), which in production looks like
    # "the network is down" with no error anywhere. That combination is
    # refused at startup unless the operator says here that they mean it.
    allow_loopback_only: bool = field(
        default_factory=lambda: _env_bool(
            "E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY", False
        )
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
    # Agent form (E2.6 + A6): the deployment's quota capability comes from the
    # server-side quota-agent, so *setting E2B_QUOTA_AGENT_URL is the switch*
    # (it outranks E2B_QUOTA_VIA_AGENT, whose default is false). That is what
    # frees the worker from CAP_SYS_ADMIN: ``xfs_quota -x`` runs agent-side.
    quota_via_agent: bool = field(default_factory=_quota_via_agent_from_env)
    # Server-side quota-agent URL/token. Wired by
    # envd_service.quota_agent.configure_quota_agent_client whenever the agent
    # form is on; a missing URL or an unreachable/rejecting agent degrades
    # quota with warnings (sandboxes and volume mounts keep working).
    quota_agent_url: str | None = field(
        default_factory=lambda: (os.getenv("E2B_QUOTA_AGENT_URL") or "").strip()
        or None
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
    # Per-sandbox host uid isolation (E3.2) -- **on by default**: every
    # sandbox gets a distinct host uid from the pool and its workspace is
    # `0770 <uid>:<worker gid>` (fix round 1 / c1). That identity is what makes the rest of
    # the isolation story work: shared volumes protect each other with real
    # 1777+sticky DAC, and (route B) the sandbox's `sandlock-supervise` slot
    # runs as *that* uid, so path mediation lands writes on the sandbox
    # instead of the worker (T5).
    #
    # It is a *preference*, not a promise: a non-root worker cannot map
    # arbitrary host uids (S1.2 fail-closed) and cannot chown, so it
    # auto-disables the pool and keeps the fixed-identity + Landlock model
    # (E5.1) with one startup WARNING -- flipping this default therefore does
    # not change what the unprivileged deployment shapes actually do.
    # Set it false explicitly to get the legacy shared-uid shape back.
    per_sandbox_uid: bool = field(
        default_factory=lambda: _env_bool("E2B_PER_SANDBOX_UID", True)
    )
    # Track F / Task F1: the two file-capability brokers
    # (``/var/lib/e2b-priv/e2b-slot-spawn`` / ``e2b-maint``) let a worker that
    # is *not* root perform the two privileged steps route B and E3.2 need --
    # starting a slot at a pooled host uid and reaching a tenant's tree (the
    # worker is a member of its group: `0770 owner=<sandbox uid> group=<worker gid>`,
    # fix round 1 / c1).
    # ``auto`` (default): use them when the worker is non-root and both are
    # installed; a *half-installed* broker pair fails the worker's startup by
    # name (a privileged broker must never be guessed at), while a worker with
    # no brokers keeps the in-process E5.1 shape and says so once. ``off``:
    # never use them (today's behaviour). A root worker ignores both.
    priv_helpers: str = field(
        default_factory=lambda: os.getenv("E2B_PRIV_HELPERS", "auto").lower()
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
        default_factory=_image_cache_dir
    )

    @property
    def all_internal_api_keys(self) -> tuple[str, ...]:
        """Active X-Internal-Key credentials (list first, single fallback)."""
        keys = list(self.internal_api_keys)
        if self.internal_api_key:
            keys.append(self.internal_api_key)
        return tuple(dict.fromkeys(keys))


#: Raised (and never swallowed) when the net-isolation switches contradict each
#: other. Named so a crash-looping worker says exactly which pair is wrong.
NET_ISOLATION_PAIRING_ERROR = (
    "E2B_ENABLE_NET_ISOLATION=true without E2B_FD_INJECT_CONNECT=true: every "
    "sandbox would get a loopback-only network namespace, so each outbound "
    "connect fails inside the kernel (no route) and user code only sees "
    "timeouts -- nothing in the worker logs an error. Set "
    "E2B_FD_INJECT_CONNECT=true to mediate egress through the supervisor, or "
    "set E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY=1 if a sandbox that cannot "
    "egress at all is what you want."
)


#: Raised when the worker is not running under the shipped seccomp profile.
#: Named so a crash-looping pod says which knob to turn.
SECCOMP_FILTER_MISSING_ERROR = (
    "SECCOMP_FILTER_MISSING: this worker is not running under a seccomp filter "
    "(/proc/self/status reports Seccomp: {mode}, {meaning}). The worker is the "
    "process that runs untrusted workloads, so an unfiltered worker hands every "
    "sandbox the worker's whole syscall surface -- and nothing logs it. The "
    "deployment profile is deploy/seccomp/sandlock-worker.json (Docker's default "
    "plus `pidfd_getfd` and `unshare`); install it per deploy/seccomp/README.md "
    "(compose `seccomp=` line, or deploy/k8s/seccomp-installer.yaml on k8s) and "
    "restart. To run unfiltered on purpose (test runner, autoscaler local "
    "backend), set E2B_REQUIRE_SECCOMP_FILTER=0."
)

SECCOMP_PROFILE_NOT_APPLIED_ERROR = (
    "SECCOMP_PROFILE_NOT_APPLIED: a seccomp filter is loaded, but it is not the "
    "shipped one -- `unshare(CLONE_NEWUSER)` is still gated (the child died with "
    "{detail}) while the worker carries no capability that would satisfy the "
    "gate. That is what the container runtime's default profile looks like, and "
    "it is how a k8s node with a missing Localhost profile can present itself: "
    "the kubelet silently skips the missing file and the pod comes up anyway "
    "(kubernetes#124944, 1.28/1.29), so every sandbox create would fail later "
    "instead of here. Install the profile (deploy/k8s/seccomp-installer.yaml, or "
    "the compose `seccomp=` line) and restart; set "
    "E2B_REQUIRE_SECCOMP_FILTER=0 to accept an unprofiled worker on purpose."
)

#: `/proc/self/status` `Seccomp:` values (linux/seccomp.h).
_SECCOMP_MODE_MEANING = {
    0: "filtering disabled",
    1: "strict mode, not a filter",
    2: "filter mode",
}


def parse_seccomp_mode(status_text: str) -> int | None:
    """The `Seccomp:` field of a /proc/<pid>/status dump, or None when absent."""
    for line in status_text.splitlines():
        if line.startswith("Seccomp:"):
            value = line.split(":", 1)[1].strip()
            try:
                return int(value)
            except ValueError:
                return None
    return None


def read_seccomp_mode(path: str = "/proc/self/status") -> int | None:
    """Read the worker's own seccomp mode; None when /proc is unavailable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_seccomp_mode(handle.read())
    except OSError:
        return None


@lru_cache(maxsize=1)
def _userns_probe() -> tuple[bool, str]:
    """Can this process still create a user namespace without capabilities?

    The shipped profile allows `unshare` unconditionally; the runtime default
    gates it on CAP_SYS_ADMIN, which the non-root worker does not have. So the
    probe separates "our profile" from "some other filter" -- the case the
    `Seccomp:` field alone cannot see. A subprocess rather than a fork: envd is
    multi-threaded and the child only has to run one syscall.
    """
    import subprocess
    import sys

    code = "import os; os.unshare(os.CLONE_NEWUSER)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or "").strip().splitlines()
    return False, (detail[-1] if detail else f"exit {result.returncode}")


def _userns_limited_by_host() -> str | None:
    """Why an EPERM probe may be the host's doing, not the profile's.

    Unprivileged user namespaces can be switched off globally, or restricted to
    AppArmor-profiled binaries (Ubuntu 24.04's default). Naming that here keeps
    the failure honest: the operator gets the real cause instead of "your
    seccomp profile is wrong".
    """
    import os

    for path, value, reason in (
        (
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
            "1",
            "the host restricts unprivileged user namespaces to AppArmor-profiled "
            "binaries (kernel.apparmor_restrict_unprivileged_userns=1)",
        ),
        (
            "/proc/sys/user/max_user_namespaces",
            "0",
            "the host has unprivileged user namespaces disabled "
            "(user.max_user_namespaces=0)",
        ),
    ):
        try:
            with open(path, encoding="utf-8") as handle:
                if handle.read().strip() == value:
                    return reason
        except OSError:
            continue
    return None


def check_seccomp_filter(
    settings: Settings,
    *,
    status_text: str | None = None,
    probe_runner: Callable[[], tuple[bool, str]] | None = None,
) -> int | None:
    """Refuse to serve sandboxes from a worker that is not filtered as shipped.

    Two layers, because they catch different mistakes (both silent otherwise):

    * `Seccomp:` in /proc/self/status -- ``0`` means no filter at all. This is
      what a dropped profile, a stray `seccomp=unconfined`, or a runtime that
      ignores an unknown profile looks like. Fatal by default: it is the exact
      regression A7 removed.
    * an active `unshare(CLONE_NEWUSER)` probe -- a filter is present, but the
      default one still gates that syscall, which is how a k8s node whose
      Localhost profile file is missing presents itself (kubelet skips it
      silently on 1.28/1.29, kubernetes#124944). Also fatal by default, unless
      the host itself disables/restricts user namespaces, which is reported as
      the cause instead.

    ``E2B_REQUIRE_SECCOMP_FILTER=0`` turns both into warnings, for the shapes
    that are unfiltered on purpose (test runners, the autoscaler's local
    backend). Returns the observed mode (None off-Linux).
    """
    mode = (
        parse_seccomp_mode(status_text)
        if status_text is not None
        else read_seccomp_mode()
    )
    if mode is None:
        # No /proc (macOS dev hosts): nothing to assert.
        logger.debug("seccomp self-check skipped: no /proc/self/status")
        return None

    required = bool(getattr(settings, "require_seccomp_filter", True))
    if mode != 2:
        message = SECCOMP_FILTER_MISSING_ERROR.format(
            mode=mode, meaning=_SECCOMP_MODE_MEANING.get(mode, "unknown mode")
        )
        if required:
            raise RuntimeError(message)
        logger.warning("%s (E2B_REQUIRE_SECCOMP_FILTER=0: continuing)", message)
        return mode

    probe = probe_runner or _userns_probe
    ok, detail = probe()
    if ok:
        logger.info("seccomp self-check: filter mode active, user namespaces allowed")
        return mode

    host_reason = _userns_limited_by_host()
    if host_reason is not None:
        logger.warning(
            "seccomp self-check: unshare(CLONE_NEWUSER) failed (%s) but %s -- "
            "sandbox creation will fail until that is lifted",
            detail,
            host_reason,
        )
        return mode

    message = SECCOMP_PROFILE_NOT_APPLIED_ERROR.format(detail=detail)
    if required:
        raise RuntimeError(message)
    logger.warning("%s (E2B_REQUIRE_SECCOMP_FILTER=0: continuing)", message)
    return mode


def check_net_isolation_pairing(settings: Settings) -> None:
    """Refuse the net-isolation shape that fails silently (measured 2026-09-16).

    ``net_isolation`` alone is a loopback-only sandbox: the supervisor still
    mediates, but a trapped ``connect()`` has no route to fall back on and the
    failure surfaces as a timeout in user code, not as an error in the worker.
    The shape stays reachable on purpose (a sandbox that must not egress, with
    inbound served through ``port_mappings``); an operator asks for it by
    setting ``E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY=1``.

    See docs/production-deployment-requirements.md §2.4.6 for the measurement.
    """
    # getattr with the conservative defaults: a settings double that predates
    # these fields keeps its old behaviour instead of crashing the startup path.
    if not getattr(settings, "enable_net_isolation", False) or getattr(
        settings, "fd_inject_connect", False
    ):
        return
    if getattr(settings, "allow_loopback_only", False):
        return
    raise RuntimeError(NET_ISOLATION_PAIRING_ERROR)
