"""Sandlock executor: Landlock + seccomp-bpf + seccomp user notification.

Requires Linux with Landlock ABI >= 6 and ``sandlock==0.9.0-beta``. Each
executor holds one lazily-created exec instance; every command execs onto it
(``start()`` -> ``instance.exec``) with per-exec cwd/env/clean_env and
optional pty stdio, while the policy ceiling (fs/chroot/network/limits) is
fixed at instance creation (M4 D1-D3). Non-Linux hosts import nothing and
``start()`` raises unimplemented.

The instance is either in-process (``sandlock.SandboxInstance``) or a
route-B ``sandlock-supervise`` slot running as the sandbox's own host uid,
where path mediation and DAC ownership are correct by construction
(``envd_service/route_b.py``, backlog #5 / T5).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from gateway_common.errors import ConnectError, unimplemented
from gateway_common.network import NetworkUpdateConflictError
from envd_service.executors.base import ExecConfig, Executor, RunningProcess
from envd_service.uid_pool import (
    CAP_SETGID,
    CAP_SETUID,
    LEGACY_SHARED_UID,
    has_effective_cap,
)
from envd_service.route_b import (
    RouteBConfig,
    RouteBInstance,
    SlotDeadError,
    supervise_policy_document,
    slot_pool_for,
)

logger = logging.getLogger(__name__)

try:  # sandlock is Linux-only; keep the import optional for macOS dev.
    import sandlock
    from sandlock import (
        ExecStdio,
        Sandbox as SandlockSandbox,
        SandboxInstance,
    )
except Exception:  # pragma: no cover - macOS / missing package
    sandlock = None  # type: ignore[assignment]
    ExecStdio = None  # type: ignore[assignment]
    SandboxInstance = None  # type: ignore[assignment]
    SandlockSandbox = None  # type: ignore[assignment]


# The six single-node /dev mounts of the fork ``sandlock.minimal_dev()``
# (third_party/sandlock/python/src/sandlock/sandbox.py, importable only on
# Linux). The mirror keeps the chroot policy shape unit-testable off-Linux,
# where the native module is absent and nothing is ever mounted.
_MINIMAL_DEV_MOUNTS = {
    "/dev/ptmx": "/dev/ptmx",
    "/dev/pts": "/dev/pts",
    "/dev/null": "/dev/null",
    "/dev/urandom": "/dev/urandom",
    "/dev/zero": "/dev/zero",
    "/dev/tty": "/dev/tty",
}


def _minimal_dev_mounts() -> dict[str, str]:
    """The chroot shape's ``fs_mount`` /dev set (minimal_dev, six nodes).

    With the native library present this delegates to the fork helper -- the
    host sources the mounts are taken from -- after a fail-closed pre-check
    that the host exposes a readable ``/dev/pts`` directory (the ``pts``
    mount binds the host devpts directory). There is deliberately no silent
    fallback to a whole-tree host ``/dev`` mount when devpts is missing.
    Off-Linux (policy-shape unit tests only) the module mirror is returned.
    """
    if sandlock is not None:
        pts = Path("/dev/pts")
        if not pts.is_dir() or not os.access(pts, os.R_OK):
            raise RuntimeError(
                "sandlock chroot shape requires a readable host /dev/pts "
                "directory (devpts) for the minimal_dev /dev/pts bind "
                "mount; refusing to fall back to a whole-tree /dev mount"
            )
        return sandlock.minimal_dev()
    return dict(_MINIMAL_DEV_MOUNTS)


class SandlockRunningProcess(RunningProcess):
    """Wraps a fork ``ExecProcess`` returned by ``SandboxInstance.exec``.

    PIPED mode streams ``proc.stdout``/``proc.stderr``; PTY mode streams the
    host-side pty master (``proc.pty``) and drives resizes straight through
    ``ExecProcess.resize`` -- the in-sandbox bridge and its in-band resize
    frames are gone (M4 D3). The child pid is cached at creation because
    ``ExecProcess.pid`` becomes ``None`` after ``wait()``.
    """

    def __init__(
        self,
        *,
        proc,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        stdin_queue: asyncio.Queue,
        pty_mode: bool = False,
        on_exit=None,
        signal_pause_supported: bool | None = None,
    ) -> None:
        self._proc = proc
        self._queue = queue
        self._loop = loop
        self._stdin_queue = stdin_queue
        self._pty_mode = pty_mode
        self._pid = proc.pid if proc.pid is not None else -1
        self._on_exit = on_exit
        self._reaped = False
        self._writer_thread: threading.Thread | None = None
        self._closed = False
        self._stdin_closed = False
        self._eof_count = 0
        if signal_pause_supported is not None:
            # Instance-level override of the class flag: a route-B child can
            # be signalled by number, an in-process one cannot.
            self.supports_signal_pause = signal_pause_supported

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def _input_stream(self):
        """Where stdin bytes go: PIPED -> proc.stdin, PTY -> proc.pty."""
        return self._proc.pty if self._pty_mode else self._proc.stdin

    def output(self) -> AsyncIterator[tuple[str, bytes]]:
        return self._consume()

    async def _consume(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            # Internal stream-termination markers are not output; the local
            # executor filters them the same way before yielding.
            if item[0] == "__eof__":
                continue
            yield item

    def _start_stdin_writer(self) -> None:
        stream = self._input_stream
        if self._writer_thread is not None or stream is None:
            return

        def _write_loop() -> None:
            try:
                while True:
                    data = asyncio.run_coroutine_threadsafe(
                        self._stdin_queue.get(), self._loop
                    ).result()
                    if data is None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                        return
                    try:
                        stream.write(data)
                        stream.flush()
                        logger.debug(
                            "sandlock stdin wrote %d bytes (fd=%s)",
                            len(data),
                            getattr(stream, "fileno", lambda: None)(),
                        )
                    except Exception as e:  # noqa: BLE001 - keep the loop alive
                        logger.warning("sandlock stdin write failed: %r", e)
                        return
            except Exception:  # pragma: no cover - defensive
                logger.exception("sandlock stdin writer failed")

        self._writer_thread = threading.Thread(target=_write_loop, daemon=True)
        self._writer_thread.start()

    def send_stdin(self, data: bytes) -> None:
        if self._closed or self._stdin_closed:
            return
        self._start_stdin_writer()
        try:
            self._stdin_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("sandlock stdin queue full; dropping %d bytes", len(data))

    def close_stdin(self) -> None:
        if self._pty_mode:
            # A pty has no independent EOF: closing the master write side
            # would hang up the terminal. Just stop accepting input; the
            # master is closed by wait() once the process exits.
            if self._stdin_closed:
                return
            self._stdin_closed = True
            return
        if self._closed:
            return
        self._closed = True
        # Flush any queued writes before closing the pipe (the writer thread
        # consumes the None sentinel and closes proc.stdin).
        self._start_stdin_writer()
        try:
            self._stdin_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def resize(self, rows: int, cols: int) -> None:
        if not self._pty_mode:
            return
        try:
            self._proc.resize(rows, cols)
        except (OSError, RuntimeError) as e:
            logger.warning("sandlock pty resize failed: %r", e)

    # M4 D5 / FUP #8: ``kill(sig)`` always SIGKILLs (see below); a
    # pause/resume fallback must never use it to deliver SIGSTOP/SIGCONT.
    supports_signal_pause = False

    def kill(self, sig: int) -> None:
        # In-process: the fork registry delivers SIGKILL to the child's whole
        # command subtree regardless of the requested signal, so
        # ``supports_signal_pause`` stays False and ProcessManager's
        # pause/resume fallback skips such a child with a WARNING instead of
        # turning a pause into a kill (FUP #8).
        #
        # Route B: ``kill_child`` carries the signal number through the slot's
        # registered pidfd, so the requested signal really arrives and
        # pause/resume may use it.
        try:
            if self.supports_signal_pause:
                self._proc.kill(sig)
            else:
                self._proc.kill()
        except Exception:
            pass

    def _mark_eof(self) -> None:
        """Signal the end of the output stream.

        PIPED mode needs both stdout and stderr EOF; PTY mode has a single
        stream (the master), so its EOF ends the output. By then the process
        has exited, so ProcessManager's ``exit_code()`` -> ``wait()`` can
        reap it without closing a still-open stdin first.
        """
        self._eof_count += 1
        target = 1 if self._pty_mode else 2
        if self._eof_count >= target:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def exit_code(self) -> int:
        result = await asyncio.to_thread(self._proc.wait)
        if self._on_exit is not None and not self._reaped:
            self._reaped = True
            self._on_exit(self._proc.child_id, self._pid)
        return result.exit_code


class SandlockExecutor(Executor):
    """Holds one lazily-created exec instance per sandbox.

    M4 D1-D3: the instance is created on first ``_ensure_instance()`` with a
    stable ``sandbox_id``-derived name and the command-independent policy
    ceiling from ``_policy_ceiling()`` (rebuilt exactly once after a
    closed/dead launch), and released by ``close()``. Every ``start()``
    execs onto that instance with per-exec cwd/env/clean_env/bind_ports and
    PIPED/PTY stdio. On non-Linux hosts sandlock is unavailable and the
    instance stays ``None`` (D11).

    The instance has two interchangeable backends, chosen once per sandbox by
    :meth:`_route_b_decline_reason`: the **in-process** ``sandlock.SandboxInstance``
    (the mediator is the worker process), or a **route-B** ``sandlock-supervise``
    slot leased from :mod:`envd_service.route_b` (the mediator is a process
    whose euid *is* this sandbox's host uid, so mediated path operations land
    with the sandbox's own ownership). Route B speaks the same
    ``exec``/``wait_child``/``kill_child``/``update_network``/``shutdown`` verb
    surface through :class:`~envd_service.route_b.RouteBInstance`, which
    mirrors ``SandboxInstance`` -- everything below is one code path.
    """

    _non_root_fallback_warned = False

    def __init__(
        self,
        *,
        workspace_dir: str,
        base_image: str | None,
        image_rootfs: Path | None,
        host_uid: int | None = None,
        per_sandbox_uid: bool = False,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        max_processes: int,
        max_open_files: int,
        allow_internet_access: bool,
        enable_network: bool,
        enable_netns: bool = False,
        enable_net_isolation: bool = False,
        fd_inject_connect: bool = False,
        port_mappings: dict | None = None,
        network: dict | None = None,
        network_deny_cidrs: tuple[str, ...] = (),
        notify_rate_limit: int = 0,
        iam_tokens: dict[str, dict[str, str]] | None = None,
        iam_signing_key: str | None = None,
        secrets_dir: str | Path | None = None,
        extra_fs_writable: list[str] | None = None,
        fs_mounts: dict[str, str] | None = None,
        sandbox_id: str | None = None,
        route_b: RouteBConfig | None = None,
    ) -> None:
        self._workspace_dir = workspace_dir
        self._base_image = base_image
        self._image_rootfs = image_rootfs
        self._host_uid = host_uid
        self._per_sandbox_uid = per_sandbox_uid
        self._memory_mb = memory_mb
        self._cpu_percent = cpu_percent
        self._disk_mb = disk_mb
        self._max_processes = max_processes
        self._max_open_files = max_open_files
        self._allow_internet_access = allow_internet_access
        self._enable_network = enable_network
        self._enable_net_isolation = enable_net_isolation
        self._fd_inject_connect = fd_inject_connect
        self._port_mappings = {
            int(host): int(sandbox) for host, sandbox in (port_mappings or {}).items()
        }
        if self._port_mappings and not enable_net_isolation:
            raise ValueError(
                "port_mappings require net isolation "
                "(E2B_ENABLE_NET_ISOLATION=true): host ports in the 50005+ "
                "range map onto the sandbox's own netns listeners"
            )
        self._network_deny_cidrs = tuple(network_deny_cidrs)
        self._notify_rate_limit = notify_rate_limit
        # Accepted for config compatibility (E2B_ENABLE_NETNS), but the fork
        # dropped per-sandbox netns/veth in favor of the unprivileged
        # loopback-netns mode: the real switch is `enable_net_isolation`
        # (E2B_ENABLE_NET_ISOLATION -> sandlock `net_isolation`), so this
        # legacy flag is a no-op.
        self._enable_netns = enable_netns
        self._network = dict(network) if network else None
        self._iam_tokens = dict(iam_tokens or {})
        self._iam_signing_key = iam_signing_key or "e2b-sandlock-local-iam-key"
        self._secrets_dir = Path(secrets_dir) if secrets_dir else None
        self._extra_fs_writable = list(extra_fs_writable or [])
        self._fs_mounts = dict(fs_mounts or {})
        self._sandbox_id = sandbox_id
        # Route B (one ``sandlock-supervise`` per sandbox, euid == the
        # sandbox's host uid). ``None`` / ``off`` keeps the in-process
        # instance; the decision itself is made once here because every input
        # (shape, uid, platform, starter privilege) is fixed at construction.
        self._route_b = route_b
        self._route_b_decline = self._route_b_decline_reason()
        self._route_b_active = self._route_b_decline is None
        self._disclose_mediation_shape()
        self._instance = None
        self._instance_name: str | None = None
        # Set by ``close()`` (the single shutdown point). Guards the
        # closed/dead rebuild-once paths: after an explicit close/shutdown a
        # fresh instance must never be leaked, so ``start()`` fails loudly
        # instead of rebuilding.
        self._closed = False
        # Serializes the instance lifecycle (creation/rebuild in
        # ``_ensure_instance``, teardown in ``close``) against
        # ``update_network``'s validate+apply section: a command exec racing
        # a network update must never interleave preflight and application
        # with instance creation (M4 D4 review Important-1).
        self._lifecycle_lock = threading.Lock()
        # F4.3/S2 staleness mapping: fork child id -> (host pid, resolved
        # argv). Registered in ``start()`` before the process is returned.
        self._child_registry: dict[int, tuple[int, list[str]]] = {}
        self._mcp_bind_port: int | None = None
        # SSL_CERT_FILE/CURL_CA_BUNDLE overrides merged into every per-exec
        # env when the chroot HTTPS-MITM CA branch is active (the policy
        # ceiling itself no longer carries env).
        self._http_inject_env: dict[str, str] = {}

    def _merged_state(self, network: dict | None) -> dict:
        """Canonical D4=A state for ``network`` folded with this executor's
        record mirrors (``allowInternetAccess``; ``allowPublicTraffic`` is
        carried by the network dict itself)."""
        from gateway_common.network import merged_network_state

        allow_internet = (network or {}).get("allowInternetAccess")
        if allow_internet is None:
            allow_internet = self._allow_internet_access
        return merged_network_state(
            network, allow_internet_access=bool(allow_internet)
        )

    def _applied_state(self) -> dict:
        """Canonical state currently applied to the instance (or, before the
        first launch, the static policy the future instance would build)."""
        from gateway_common.network import merged_network_state

        return merged_network_state(
            self._network, allow_internet_access=self._allow_internet_access
        )

    def validate_update(self, network: dict | None) -> None:
        """Raise :class:`NetworkUpdateConflictError` when ``network`` cannot
        be applied to an already-launched instance.

        D4=A: with no instance yet every normalized update is applicable (it
        becomes the static policy the future instance is built with). Once
        launched, only monotone narrowings the fork verb can represent are
        accepted; everything else must be rejected with HTTP 409 before any
        record is persisted. This method is pure -- it never mutates the
        executor or calls the instance.
        """
        if self._instance is None:
            return
        from gateway_common.network import network_update_conflict_reason

        reason = network_update_conflict_reason(
            self._applied_state(), self._merged_state(network)
        )
        if reason is not None:
            raise NetworkUpdateConflictError(reason)

    def update_network(self, network: dict | None) -> None:
        """Replace the network policy for new execs (M4 D4, S2 semantics).

        With no live instance the update simply becomes the static policy of
        the future instance. On a launched instance the update is validated
        first (raising :class:`NetworkUpdateConflictError` without mutating
        anything when it is not expressible); expressible narrowings call
        ``instance.update_network(ip_set)`` with the IP-literal allow set of
        the proposed ``allowOut`` and log the fork's stale-children report.
        The executor's own policy copy is only replaced after a successful
        apply, so a rejection never leaves the record and runtime diverging.
        """
        merged = dict(network) if network else None
        with self._lifecycle_lock:
            if self._instance is not None:
                self.validate_update(merged)
                if self._merged_state(merged) != self._applied_state():
                    self._apply_instance_update(merged)
            self._network = merged
            allow_internet = (merged or {}).get("allowInternetAccess")
            if allow_internet is not None:
                self._allow_internet_access = bool(allow_internet)

    def _apply_instance_update(self, network: dict | None) -> None:
        """Bind an accepted narrowing to the live instance's new execs.

        A closed/dead ``RuntimeError`` from ``instance.update_network``
        (idle/24h expiry surfaced at apply time) rebuilds the instance
        exactly once under the already-held lifecycle lock and retries the
        apply; after an explicit ``close()``/shutdown the failure propagates
        instead of leaking a fresh instance.
        """
        allow_out = (network or {}).get("allowOut")
        ip_set = list(allow_out) if allow_out is not None else []
        try:
            stale_child_ids = self._instance.update_network(ip_set)
        except RuntimeError as exc:
            message = str(exc)
            if "closed" not in message and "dead" not in message:
                raise
            if self._closed:
                logger.warning(
                    "not rebuilding %s instance after executor shutdown "
                    "during network update sandbox_id=%s instance_name=%s",
                    "closed" if "closed" in message else "dead",
                    self._sandbox_id or "-",
                    self.instance_name,
                )
                raise
            reason = "closed" if "closed" in message else "dead"
            if self._route_b_active:
                # Rebuilding here would spawn a supervise process on the event
                # loop (this method is synchronous). Refuse instead: the
                # executor's own policy copy stays untouched, so the record and
                # the runtime never diverge, and the next exec rebuilds the
                # slot off the loop with whatever network state is current.
                logger.warning(
                    "route-B instance %s during network update; refusing the "
                    "update instead of restarting the slot from the request "
                    "path sandbox_id=%s instance_name=%s",
                    reason,
                    self._sandbox_id or "-",
                    self.instance_name,
                )
                raise
            logger.info(
                "sandlock instance %s during network update; rebuilding once "
                "sandbox_id=%s instance_name=%s",
                reason,
                self._sandbox_id or "-",
                self.instance_name,
            )
            inst = self._instance
            try:
                inst.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            if self._instance is inst:
                self._instance = None
            inst = self._ensure_instance_locked()
            if inst is None:
                raise RuntimeError(
                    "no sandlock instance available after closed/dead rebuild"
                )
            try:
                # Exactly one retry; a second closed/dead failure propagates.
                stale_child_ids = inst.update_network(ip_set)
            except PermissionError as exc2:
                raise NetworkUpdateConflictError(
                    str(exc2) or "instance refused the network update (EPERM)"
                ) from exc2
        except PermissionError as exc:
            # Defense in depth: a fork EPERM (widening past the static
            # ceiling) maps to the same 409 without persisting.
            raise NetworkUpdateConflictError(
                str(exc) or "instance refused the network update (EPERM)"
            ) from exc
        self._log_stale_children(stale_child_ids)

    def _log_stale_children(self, stale_child_ids: list[int]) -> None:
        """INFO log one line per stale fork child plus a count summary."""
        sandbox_id = self._sandbox_id or "-"
        for child_id in stale_child_ids:
            entry = self._child_registry.get(child_id)
            if entry is None:
                logger.info(
                    "sandbox_id=%s instance_name=%s stale_child_id=%s "
                    "pid=%s cmd=%s",
                    sandbox_id,
                    self.instance_name,
                    child_id,
                    "-",
                    "-",
                )
                continue
            pid, cmd = entry
            logger.info(
                "sandbox_id=%s instance_name=%s stale_child_id=%s pid=%s cmd=%s",
                sandbox_id,
                self.instance_name,
                child_id,
                pid,
                " ".join(cmd),
            )
        logger.info(
            "sandbox_id=%s instance_name=%s network_update stale_child_count=%d",
            sandbox_id,
            self.instance_name,
            len(stale_child_ids),
        )

    def set_mcp_bind_port(self, port: int | None) -> None:
        """Set the per-sandbox MCP gateway host port for the bind ceiling.

        The runtime context pre-allocates the port before the first exec when
        the sandbox has MCP enabled (``record.mcp``), so the instance policy
        can carry ``net_allow_bind=[port]`` from creation. The ceiling is
        fixed at instance creation; ``None`` clears the allowance.
        """
        self._mcp_bind_port = int(port) if port is not None else None

    @property
    def instance_name(self) -> str:
        """Stable instance identity: ``sandbox_id`` (or its hash) when given,
        otherwise the workspace directory basename."""
        return self._instance_name or self._instance_name_for()

    @property
    def instance_handle(self):
        """Live ``SandboxInstance``, or ``None`` until ``_ensure_instance()``."""
        return self._instance

    def _instance_name_for(self) -> str:
        sid = self._sandbox_id or Path(self._workspace_dir).name
        if len(sid.encode()) <= 64:
            return sid
        import hashlib

        return "sbx_" + hashlib.sha256(sid.encode()).hexdigest()[:16]

    # Warn-once switches for the shapes that decline a slot. The reason itself
    # comes from `_route_b_decline_reason` -- one decision, quoted verbatim by
    # the disclosure below, so the message can never disagree with the rule.
    _route_b_no_starter_warned = False
    _route_b_no_fd_client_warned = False
    _mediation_shape_disclosed = False

    def _route_b_decline_reason(self) -> str | None:
        """Why this sandbox does not run on a supervise slot, or None if it does.

        Route B needs a *per-sandbox host uid*: the slot process **is** that uid
        (``docs/supervise-identity-handoff.md`` §5), and W1 forbids two live
        generations on one uid, so a shared-uid sandbox cannot have a slot. It
        also needs the native library, the ``sandlock-supervise`` binary the
        wheel ships, and a starter that can drop privileges.

        ``auto`` engages where it matters: the chroot (image-rootfs) shape is
        the only one where ``fs_denied``/chroot path mediation runs, and
        mediating as the sandbox's own uid is what makes mediated writes belong
        to the sandbox (T5). ``E2B_ROUTE_B_SLOTS>0`` or ``E2B_ROUTE_B=on`` asks
        for a slot in every shape instead. An operator who explicitly asked for
        route B and cannot get it fails loudly -- route A vs route B is a
        deployment decision, never a silent downgrade (§8).
        """
        cfg = self._route_b
        if cfg is None:
            return "the worker passed no route-B config (E2B_ROUTE_B_* unset)"
        if cfg.mode == "off":
            return "E2B_ROUTE_B=off"
        if sandlock is None:
            return "the native sandlock module is unavailable"
        forced = cfg.mode == "on" or cfg.slots > 0
        mediation_shape = bool(self._base_image and self._image_rootfs is not None)
        if not (forced or mediation_shape):
            return "auto keeps the pure (no-chroot) shape in-process: it mediates nothing"
        if not self._per_sandbox_uid or self._host_uid is None:
            reason = (
                "no per-sandbox host uid (E2B_PER_SANDBOX_UID off, or the uid "
                "pool allocated nothing): a slot runs as the sandbox's own uid"
            )
            if forced:
                raise RuntimeError(
                    "route B was requested (E2B_ROUTE_B=on / E2B_ROUTE_B_SLOTS>0) "
                    "but " + reason
                )
            return reason
        if not cfg.privileged_starter:
            reason = (
                f"this worker cannot start a slot as uid {self._host_uid} "
                "(needs root / CAP_SETUID or an injected launcher spawner)"
            )
            if forced:
                raise RuntimeError("route B was requested but " + reason)
            if not type(self)._route_b_no_starter_warned:
                type(self)._route_b_no_starter_warned = True
                logger.warning(
                    "route B unavailable for sandbox_id=%s: %s; mediation stays "
                    "in-process, which for the chroot shape now fails closed "
                    "(T5 is not traded back)",
                    self._sandbox_id or "-",
                    reason,
                )
            return reason
        from envd_service.route_b import default_supervise_bin, fd_client_available

        if cfg.transport == "fd" and not fd_client_available():
            # The wheel predates fork F17: it can serve an fd handoff but the
            # worker cannot drive one. Falling back to `path` would put a
            # channel token into the slot's argv, so that is an operator
            # decision, not something to do quietly.
            reason = (
                "the installed sandlock wheel has no sandlock_supervise_connect_fd "
                "(needs fork F17 or newer)"
            )
            if forced:
                raise RuntimeError(
                    "route B was requested with transport=fd, but " + reason
                    + ": rebuild wheels/fork/ or set E2B_ROUTE_B_TRANSPORT=path"
                )
            if not type(self)._route_b_no_fd_client_warned:
                type(self)._route_b_no_fd_client_warned = True
                logger.warning(
                    "route B unavailable for sandbox_id=%s: %s; not falling back to "
                    "the registered transport, whose token would sit in the slot's "
                    "world-readable argv (rebuild wheels/fork/ or set "
                    "E2B_ROUTE_B_TRANSPORT=path deliberately)",
                    self._sandbox_id or "-",
                    reason,
                )
            return reason
        if not default_supervise_bin().exists():
            reason = (
                f"{default_supervise_bin()} is missing (the sandlock wheel ships "
                "the supervise binary)"
            )
            if forced:
                raise RuntimeError("route B was requested but " + reason)
            logger.warning(
                "route B unavailable for sandbox_id=%s: %s; running the in-process "
                "instance",
                self._sandbox_id or "-",
                reason,
            )
            return reason
        return None

    def _disclose_mediation_shape(self) -> None:
        """Say up front what an in-process chroot sandbox now means.

        E2B no longer sets the fork's ``mediation_run_as=supervisor`` tier, so
        the combination the tier used to paper over -- privileged in-process
        mediator + path mediation + a non-zero sandbox host uid -- is refused by
        the fork instead of silently producing supervisor-owned files (SL-1/T5).
        None of that reaches the operator through the library, though: the FFI
        create/launch entry points return a null handle and the SDK turns it into
        ``sandlock_instance_launch failed`` (SL-12), so this is the only place
        that says which rule fired and *why no slot was available* -- with the
        reason quoted from the very function that decided it.
        """
        if self._route_b_active or type(self)._mediation_shape_disclosed:
            return
        if sandlock is None or not self._in_process_mediation_is_refused():
            return
        type(self)._mediation_shape_disclosed = True
        logger.error(
            "chroot (image-rootfs) sandbox_id=%s runs in-process, not on a "
            "supervise slot (%s): path mediation would then execute as the "
            "mediator, so the fork refuses the create instead of leaving "
            "supervisor-owned files behind (T5, no downgrade tier is set any "
            "more). Fix: keep E2B_PER_SANDBOX_UID on and let route B lease a "
            "slot (E2B_ROUTE_B=auto/on), or run a privileged launcher or an "
            "external slot fleet.",
            self._sandbox_id or "-",
            self._route_b_decline or "reason unavailable",
        )

    def _in_process_mediation_is_refused(self) -> bool:
        """Would the fork refuse *this* sandbox's in-process path mediation?

        Mirrors the fork's C-tier check (``mediation_remap_is_refused``: F6.1
        fail-closed, F14 privilege rule). Mediated path operations run in the
        mediator, so a mediator that can remap the sandbox to a *different*
        non-zero host uid attributes the sandbox's own files to itself (T5) and
        the create is refused now that nothing asks for the ``supervisor`` tier.

        The distinction matters because this predicate gates a loud ERROR: a
        non-root worker (E5.1) mediates as its own euid, which *is* the
        sandbox's host uid, so nothing is refused there and disclosing it on
        every unprivileged run would be a false alarm.
        """
        if not (self._base_image and self._image_rootfs is not None):
            return False
        if self._per_sandbox_uid:
            if self._host_uid is None:
                # A root worker fails the create on the missing allocation
                # itself (`_run_as_identity` raises); this is not that error.
                return False
            host_uid = self._host_uid
        else:
            host_uid = LEGACY_SHARED_UID if os.geteuid() == 0 else os.geteuid()
        mediator_euid = os.geteuid()
        if mediator_euid == 0:
            return host_uid != 0
        return (
            host_uid != 0
            and host_uid != mediator_euid
            and has_effective_cap(CAP_SETUID)
            and has_effective_cap(CAP_SETGID)
        )

    def _slot_key(self) -> str:
        """The pool key for this sandbox (slots are leased per sandbox)."""
        return self._sandbox_id or self.instance_name

    def _open_route_b_instance(self):
        """Lease this sandbox's slot and wrap it in the instance shim.

        The ceiling travels as a full-field ``--policy`` document (the same
        field set the in-process builder gets, in wire spellings), and the
        generation's main program is the parking shell: an envd instance has
        no main process, but launch-first is what brings the slot's session up
        and the generation ends when that process ends.
        """
        cfg = self._route_b
        document = supervise_policy_document(self._policy_ceiling())
        pool = slot_pool_for(cfg)
        uid = self._host_uid

        def _start():
            return pool.acquire_sync(
                self._slot_key(),
                document,
                uid=uid,
                name=self.instance_name,
            )

        try:
            handle = _start()
        except SlotDeadError as exc:
            if self._closed:
                raise
            # W1 recycles a uid by restarting its process, so a slot that died
            # on the way up is restarted exactly once here; a second failure
            # surfaces unchanged.
            logger.info(
                "route-B slot for sandbox_id=%s failed to start (%s); "
                "restarting once",
                self._sandbox_id or "-",
                exc,
            )
            handle = _start()
        logger.info(
            "route-B instance ready sandbox_id=%s instance_name=%s uid=%s "
            "slot=%s channel=%s guest-uid=%s",
            self._sandbox_id or "-",
            self.instance_name,
            uid,
            handle.name,
            # transport 1 has no path at all; naming the handoff keeps the log
            # honest about why there is nothing to look at in /tmp.
            (
                f"fd-handoff(pid {handle.process.pid})"
                if handle.sock_path is None
                else handle.sock_path
            ),
            # The slot's own answer, not an assumption: a self-mapped namespace
            # makes the workload uid 0 inside (parity with the in-process
            # mediator), and an unavailable unprivileged userns leaves it at the
            # host uid. `unknown` means the wheel predates fork F18.
            handle.guest_uid or "unknown",
        )
        return RouteBInstance(pool=pool, handle=handle, name=self.instance_name)

    async def _ensure_instance_async(self):
        """:meth:`_ensure_instance` without ever blocking the event loop.

        Creating an in-process instance is a quick native call, so it runs
        inline under the lifecycle lock as before. Creating a route-B instance
        spawns a process and waits for its registered channel to answer, which
        takes the same lock on a worker thread.
        """
        if self._route_b_active:
            return await asyncio.to_thread(self._ensure_instance)
        with self._lifecycle_lock:
            return self._ensure_instance_locked()

    async def _reopen_instance_after(self, reason: str, previous) -> object:
        """Release a closed/dead instance and build its replacement once.

        Shared by both backends: the executor must never leak a fresh
        instance after an explicit ``close()``/shutdown, and the replacement
        may block (route B restarts a slot process), so it runs off the loop.
        """
        retire = self._retire_previous_instance

        if self._route_b_active:
            # Closing a route-B instance ends a *process* (shutdown verb +
            # reap), so it goes off the loop like the creation next to it.
            await asyncio.to_thread(retire, reason, previous)
        else:
            retire(reason, previous)
        return await self._ensure_instance_async()

    def _retire_previous_instance(self, reason: str, previous) -> None:
        """Drop a closed/dead instance handle under the lifecycle lock.

        Shared by both backends; only the in-process one is cheap enough to
        run inline on the event loop.
        """
        with self._lifecycle_lock:
            if self._closed:
                logger.warning(
                    "not rebuilding %s exec instance after executor shutdown "
                    "sandbox_id=%s instance_name=%s",
                    reason,
                    self._sandbox_id or "-",
                    self.instance_name,
                )
                raise RuntimeError(
                    f"sandlock instance is {reason} and the executor is shut down"
                )
            if self._instance is previous:
                try:
                    previous.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
                self._instance = None

    def _ensure_instance(self):
        """Lazily create the one long-lived exec instance (M4 D1/D3).

        The creation segment (policy build + ``SandboxInstance`` construction
        + the rebuild-once retry after a closed/dead launch) runs under the
        lifecycle lock so an ``update_network`` cannot observe a half-created
        instance or interleave preflight/apply with it.
        """
        with self._lifecycle_lock:
            return self._ensure_instance_locked()

    def _ensure_instance_locked(self):
        """Creation core; caller must hold ``_lifecycle_lock``.

        A route-B instance needs the supervise binary and the native channel
        client (checked by :meth:`_route_b_decline_reason`), not the in-process
        ``SandboxInstance`` class, so the availability guard only applies to
        the in-process backend.
        """
        if self._instance is None and (
            self._route_b_active or SandboxInstance is not None
        ):
            if self._instance_name is None:
                self._instance_name = self._instance_name_for()
            # Route B: the "instance" is a supervise slot leased to this
            # sandbox, so the ceiling travels as a policy document and the
            # exec verbs cross the channel instead of the FFI.
            if self._route_b_active:
                self._instance = self._open_route_b_instance()
            else:
                policy = self._build_instance_policy()
                try:
                    self._instance = SandboxInstance(
                        policy, name=self._instance_name
                    )
                except RuntimeError as exc:
                    message = str(exc)
                    if "closed" not in message and "dead" not in message:
                        logger.warning(
                            "sandlock instance launch failed sandbox_id=%s "
                            "instance_name=%s error=%s",
                            self._sandbox_id or "-",
                            self._instance_name,
                            message,
                        )
                        raise
                    # The prior session was closed (shutdown/idle reclaim) or died
                    # (machinery failure): rebuild exactly once, and let a second
                    # failure bubble up unchanged.
                    reason = "closed" if "closed" in message else "dead"
                    logger.info(
                        "sandlock instance relaunching after %s sandbox_id=%s "
                        "instance_name=%s",
                        reason,
                        self._sandbox_id or "-",
                        self._instance_name,
                    )
                    try:
                        self._instance = SandboxInstance(
                            policy, name=self._instance_name
                        )
                    except RuntimeError as exc2:
                        logger.warning(
                            "sandlock instance relaunch failed sandbox_id=%s "
                            "instance_name=%s error=%s",
                            self._sandbox_id or "-",
                            self._instance_name,
                            str(exc2),
                        )
                        raise
            if self._instance is not None:
                logger.info(
                    "sandlock instance created sandbox_id=%s instance_name=%s "
                    "max_memory=%s max_processes=%d chroot=%s",
                    self._sandbox_id or "-",
                    self._instance_name,
                    f"{self._memory_mb}M",
                    self._max_processes,
                    "yes"
                    if self._base_image and self._image_rootfs is not None
                    else "no",
                )
        return self._instance

    def close(self) -> None:
        """Close the exec instance and release the handle (idempotent).

        Marks the executor shut down: later ``start()`` calls fail loudly
        instead of rebuilding a fresh instance (the closed/dead rebuild-once
        retry is for idle/24h expiry during a live sandbox, not for after an
        explicit teardown).
        """
        with self._lifecycle_lock:
            self._closed = True
            if self._instance is not None:
                logger.info(
                    "sandlock instance closed sandbox_id=%s instance_name=%s",
                    self._sandbox_id or "-",
                    self._instance_name,
                )
                self._instance.close()
                self._instance = None
            self._child_registry.clear()

    def _child_exited(self, child_id: int, pid: int) -> None:
        """Drop a reaped child from the staleness registry.

        ``pid`` guards against removing a newer entry if the fork recycles a
        child id after an instance rebuild: the entry is only removed while
        it still points at the process that just ended.
        """
        entry = self._child_registry.get(child_id)
        if entry is not None and entry[0] == pid:
            del self._child_registry[child_id]

    def _materialize_http_inject(
        self, entries: list[dict]
    ) -> list[dict]:
        """Turn ``http_inject`` entries carrying literal header values into
        sandlock ``secret`` sources.

        A literal value becomes a supervisor-only secret file (mode 0600,
        never granted to the sandbox); a ``${e2b.identity.tokens.<NAME>}``
        placeholder resolves either from the sandbox's registered ``iam``
        workload tokens (minting a JWT-SVID for the audience) or, as a
        fallback, from the ``E2B_IDENTITY_TOKEN_<NAME>`` env var of the
        worker (platform-injected literal secret). Raises when a placeholder
        has no backing source, so a misconfigured IAM secret fails at
        sandbox creation instead of silently sending the request
        unauthenticated.
        """
        if not entries:
            return []
        if self._secrets_dir is None:
            raise RuntimeError(
                "http_inject (rules[].transform.headers) requires a "
                "supervisor secrets dir"
            )
        out: list[dict] = []
        for entry in entries:
            value = str(entry["value"])
            import re

            placeholder = re.compile(
                r"\$\{e2b\.identity\.tokens\.([A-Za-z0-9_]+)\}"
            )
            m = placeholder.fullmatch(value)
            if m is not None and m.group(1) not in self._iam_tokens:
                # Pure env-backed placeholder: keep the supervisor env source
                # (sandlock reads it at build time) instead of a file.
                var = f"E2B_IDENTITY_TOKEN_{m.group(1)}"
                if var not in os.environ:
                    raise RuntimeError(
                        f"header transform for {entry['matcher']} references "
                        f"identity token {m.group(1)!r} but {var} is not set "
                        f"(and no iam token named {m.group(1)!r} was registered)"
                    )
                entry = dict(entry)
                entry.pop("value", None)
                entry["secret"] = f"env:{var}"
                out.append(entry)
                continue
            if "${e2b.identity.tokens." in value:

                def _resolve(mm: re.Match) -> str:
                    name = mm.group(1)
                    token_cfg = self._iam_tokens.get(name)
                    if token_cfg is not None:
                        # SDK workload identity (iam=...): mint a JWT-SVID for
                        # the requested audience (supports "Bearer ${...}").
                        return self._mint_iam_jwt(
                            audience=str(token_cfg.get("audience", ""))
                        )
                    var = f"E2B_IDENTITY_TOKEN_{name}"
                    if var in os.environ:
                        return os.environ[var]
                    raise RuntimeError(
                        f"header transform for {entry['matcher']} references "
                        f"identity token {name!r} but {var} is not set "
                        f"(and no iam token named {name!r} was registered)"
                    )

                value = placeholder.sub(_resolve, value)
            secret_dir = self._secrets_dir / os.path.basename(
                self._workspace_dir.rstrip("/")
            )
            secret_dir.mkdir(parents=True, exist_ok=True)
            path = secret_dir / f"{entry['name']}.secret"
            with open(path, "w", encoding="utf-8") as f:
                f.write(value)
            os.chmod(path, 0o600)
            entry = dict(entry)
            entry.pop("value", None)
            entry["secret"] = f"file:{path}"
            out.append(entry)
        return out

    def _run_as_identity(self) -> tuple[int, int]:
        """Host uid/gid passed to sandlock ``RunAs`` (S1.2 contract).

        With per-sandbox uid enabled the allocated ``host_uid`` becomes the
        sandbox's host identity (inside the namespace it is still uid 0).
        A non-root worker cannot map an arbitrary host uid (S1.2 fail-closed:
        single-entry userns maps only the caller's own euid), so it degrades
        to the worker identity — fixed uid + Landlock, the E5.1 model.
        A root worker with per-sandbox uid enabled but no allocated uid is a
        configuration error and fails loudly instead of silently downgrading
        to a shared uid.
        """
        if self._per_sandbox_uid:
            if self._host_uid is not None:
                return self._host_uid, self._host_uid
            if os.geteuid() != 0:
                if not type(self)._non_root_fallback_warned:
                    type(self)._non_root_fallback_warned = True
                    logger.warning(
                        "non-root worker: cannot map per-sandbox host uids "
                        "(single-entry userns); using fixed worker identity "
                        "+ Landlock"
                    )
                return os.geteuid(), os.getegid()
            raise RuntimeError(
                "per-sandbox uid enabled but sandbox has no allocated "
                "host_uid (worker uid pool did not provision it)"
            )
        # Legacy default: all sandboxes share host uid 1000. Only a root
        # worker can map that uid; a non-root worker would be rejected by
        # S1.2's fail-closed RunAs check (single-entry userns maps only the
        # caller's own identity), so it falls back to the worker identity —
        # fixed uid + Landlock, the E5.1 model — instead of hardcoding 1000.
        if os.geteuid() == 0:
            return 1000, 1000
        return os.geteuid(), os.getegid()


    def _mint_iam_jwt(self, audience: str) -> str:
        """Mint a JWT-SVID for a registered workload identity.

        Local compatible layer: HS256-signed with the worker's IAM signing key
        (``E2B_IAM_SIGNING_KEY``), carrying the requested audience. Upstreams
        that validate must be configured with the same key.
        """
        import base64
        import hashlib
        import hmac
        import json
        import time

        def _b64(data: bytes) -> bytes:
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        header = _b64(b'{"alg":"HS256","typ":"JWT"}')
        now = int(time.time())
        payload = _b64(
            json.dumps(
                {
                    "aud": audience,
                    "iss": "e2b-sandlock",
                    "iat": now,
                    "exp": now + 600,
                },
                separators=(",", ":"),
            ).encode()
        )
        signing_input = header + b"." + payload
        sig = hmac.new(
            self._iam_signing_key.encode(), signing_input, hashlib.sha256
        ).digest()
        return (signing_input + b"." + _b64(sig)).decode()
    @staticmethod
    def resolve_cmd(cmd: list[str]) -> list[str]:
        """Translate ``/bin/bash`` to ``/bin/sh`` when bash is unavailable
        (slim base images do not ship bash; the official SDK always sends
        ``cmd=/bin/bash``)."""
        if cmd and cmd[0] == "/bin/bash":
            return ["/bin/sh"] + cmd[1:]
        return cmd

    def _bind_ports_for(self, config: ExecConfig) -> list[int] | None:
        """Per-exec bind allowance for ``instance.exec(bind_ports=...)``.

        Only the MCP gateway command may bind the pre-allocated MCP host
        port (sandboxes share the worker network namespace); ordinary
        commands exec without a bind allowance.
        """
        if (
            self._mcp_bind_port is not None
            and "mcp-gateway" in " ".join(config.cmd)
        ):
            return [self._mcp_bind_port]
        return None

    def _view_cwd(self, config: ExecConfig) -> str | None:
        """Map a host-side command cwd into the sandbox's view for exec.

        In chroot mode the workspace is mounted at both /home/user (the
        canonical alias: the fork's ``host_to_virtual`` breaks host-source
        ties by declaration order and ``mount_map`` declares /home/user
        first) and /workspace, so a host workspace cwd (or an empty default)
        maps to /home/user; other paths pass through unchanged (S9: the
        chroot shape's fs_readable covers /). Without a chroot the host path
        is used as-is (the workspace sits in fs_writable).
        """
        cwd = (config.cwd or "").strip()
        if self._base_image and self._image_rootfs is not None:
            if not cwd or cwd.startswith(str(self._workspace_dir)):
                return "/home/user"
        return cwd or None

    def _exec_params(self, config: ExecConfig, *, bind_ports=None) -> dict:
        """Per-exec parameter dict for ``SandboxInstance.exec``.

        ``cwd`` maps through ``_view_cwd``; ``env`` starts from the command
        env plus the instance's HTTPS-MITM CA pinning when that branch is
        active; ``clean_env=True`` starts every child from an empty
        environment. None-valued params are dropped so the fork defaults
        apply.
        """
        env = dict(config.env)
        env.update(self._http_inject_env)
        params = {
            "cwd": self._view_cwd(config),
            "env": env,
            "clean_env": True,
            "bind_ports": bind_ports or None,
        }
        return {k: v for k, v in params.items() if v is not None}

    def _policy_ceiling(self) -> dict:
        """Command-independent policy ceiling for the long-lived instance, as kwargs.

        Everything the instance grants regardless of the individual command:
        fs writable/readable/denied, the network ceiling (net_allow/deny,
        http_*, host_mask, egress_proxy, http_inject), resource limits,
        uid/gid/mediation tier, the chroot + fs_mount shape and
        net_isolation/fd_inject/port_mappings. Per-command cwd/env/clean_env
        live in ``_exec_params``; the MCP bind allowance comes from
        ``set_mcp_bind_port`` (the context pre-allocates the port before the
        first exec). ``_build_sandbox`` below keeps the same field mapping
        for the one-shot probes/security tests that still use it.
        """
        fs_writable = [self._workspace_dir]
        fs_writable.extend(self._extra_fs_writable)
        fs_readable = ["/usr", "/lib", "/bin", "/opt"]
        # Denials are only issued where the sandbox can actually see the
        # path: without a chroot, Landlock is an allow-list and shared paths
        # like /dev/shm are already unreachable (not in fs_readable), so
        # rules would add nothing -- and they would cost something: a denial is
        # enforced by an on-behalf open the *mediator* performs, so mediated
        # writes belong to whoever mediates. On a route-B slot that is this
        # sandbox's host uid (T5's fix); on a privileged in-process mediator it
        # would be host uid 0, which the fork refuses outright now that E2B no
        # longer asks for the supervisor tier (SL-1).
        fs_denied: list[str] = []
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: "/" resolves inside the chroot (the image
            # rootfs), so the whole image is readable as its own filesystem.
            # The host filesystem stays unreachable: the chroot restricts the
            # path space, and shared volumes are only exposed via their exact
            # fs_writable directory. minimal_dev mounts only the six /dev
            # nodes, so /dev/shm and /dev/mqueue never exist in the sandbox
            # view and need no carve-out; /proc/kcore and /sys stay denied
            # as defensive entries.
            fs_readable = list(fs_readable) + ["/"]
            fs_denied = ["/proc/kcore", "/sys"]
        net_allow: list[str] = []
        net_deny: list[str] = []
        http_allow: list[str] = []
        http_inject: list[dict] = []
        host_mask: str | None = None
        egress_proxy: dict | None = None
        if self._network:
            from gateway_common.network import sandlock_network_policy

            policy = sandlock_network_policy(
                self._network,
                allow_internet_access=self._allow_internet_access,
                enable_network=self._enable_network,
                private_deny_cidrs=list(self._network_deny_cidrs),
            )
            net_allow = policy["net_allow"]
            net_deny = policy["net_deny"]
            http_allow = policy["http_allow"]
            http_inject = self._materialize_http_inject(policy["http_inject"])
            host_mask = policy["host_mask"]
            egress_proxy = policy["egress_proxy"]
        elif self._allow_internet_access and self._enable_network:
            net_allow = [
                "files.pythonhosted.org:443",
                "pypi.org:443",
                "registry.npmjs.org:443",
                "proxy.golang.org:443",
                "static.crates.io:443",
                "github.com:443",
                "raw.githubusercontent.com:443",
            ]

        sandbox_uid, sandbox_gid = self._run_as_identity()
        kwargs: dict = {
            "fs_writable": fs_writable,
            "fs_readable": fs_readable,
            "fs_denied": fs_denied,
            "net_allow": net_allow,
            "net_deny": net_deny,
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": host_mask,
            "egress_proxy": egress_proxy,
            "max_memory": f"{self._memory_mb}M",
            "max_processes": self._max_processes,
            "max_open_files": self._max_open_files,
            "max_cpu": min(100, max(1, self._cpu_percent)),
            "max_disk": f"{self._disk_mb}M",
            "notify_rate_limit": self._notify_rate_limit or None,
            "uid": sandbox_uid,
            "gid": sandbox_gid,
        }
        if self._mcp_bind_port is not None:
            # The SDK starts the MCP gateway inside the sandbox; the whole
            # instance may bind its HTTP port (per-sandbox allocated MCP
            # port, since sandboxes share the worker network namespace).
            kwargs["net_allow_bind"] = [self._mcp_bind_port]
            if self._enable_net_isolation:
                # E7.1: under net_isolation the gateway listens inside the
                # sandbox's own loopback-only netns, unreachable from the
                # worker; S2.5 inbound mapping serves the sandbox's accept()
                # from a supervisor host-loopback listener on the same port,
                # which is what the /mcp proxy dials (127.0.0.1:<port>).
                self._port_mappings.setdefault(
                    self._mcp_bind_port, self._mcp_bind_port
                )
        if self._enable_net_isolation:
            kwargs["net_isolation"] = True
            if self._port_mappings:
                kwargs["port_mappings"] = dict(self._port_mappings)
            if self._fd_inject_connect:
                kwargs["fd_inject_connect"] = True
            elif not getattr(type(self), "_netns_no_inject_warned", False):
                type(self)._netns_no_inject_warned = True
                logger.warning(
                    "net_isolation enabled without fd_inject_connect: "
                    "sandboxes are loopback-only (all external egress fails)"
                )
        elif self._fd_inject_connect:
            kwargs["fd_inject_connect"] = True
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: chroot into the extracted image and expose
            # the sandbox directory as /home/user (canonical alias: declared
            # first, and the fork breaks host-source ties by declaration
            # order) and /workspace (official SDK spelling) inside it.
            # fs_mount only takes effect at runtime, so the mount points must
            # already exist inside the rootfs for chdir() to work.
            # The rootfs must carry the standard /dev parent dir for
            # traversal and listings (minimal_dev provides the node names);
            # slim base images extract without one, so pre-create it like the
            # other mount points.
            for mount_point in ("workspace", "home/user", "dev"):
                Path(self._image_rootfs).joinpath(mount_point).mkdir(
                    parents=True, exist_ok=True
                )
            # Volume mount targets must exist inside the rootfs too.
            for virtual in self._fs_mounts:
                Path(self._image_rootfs).joinpath(
                    virtual.removeprefix("/")
                ).mkdir(parents=True, exist_ok=True)
            kwargs["chroot"] = str(self._image_rootfs)
            mount_map = {
                "/home/user": self._workspace_dir,
                "/workspace": self._workspace_dir,
            }
            mount_map.update(self._fs_mounts)
            # minimal_dev replaces the whole-tree host /dev mount: only the
            # six single-node mounts (ptmx, pts, null, urandom, zero, tty)
            # are visible under the chroot's /dev, so /dev/shm and
            # /dev/mqueue cannot leak in and no fs_deny carve-out is needed.
            # Native ExecStdio.PTY lives host-side, so no devpts node grants
            # are part of the ceiling either.
            mount_map.update(_minimal_dev_mounts())
            kwargs["fs_mount"] = mount_map
        elif self._fs_mounts:
            # Without a chroot (pure Sandlock), virtual mount paths cannot be
            # materialized; volume mounts live inside the sandbox directory as
            # symlinks created by the control plane.
            pass
        if http_allow and self._image_rootfs is not None:
            # HTTPS MITM for rule-registered domains: sandlock intercepts 443
            # with an ephemeral CA; splice that CA into a per-sandbox copy of
            # the image trust bundle (never mutate the shared rootfs) and pin
            # the copy via SSL_CERT_FILE so in-sandbox clients trust it. The
            # env pinning travels per exec (the ceiling carries no env).
            ca_src = self._image_rootfs / "etc/ssl/certs/ca-certificates.crt"
            if ca_src.is_file():
                ca_dir = Path(self._workspace_dir) / ".e2b-ca"
                ca_dir.mkdir(parents=True, exist_ok=True)
                ca_dst = ca_dir / "ca-certificates.crt"
                try:
                    shutil.copy2(ca_src, ca_dst)
                except OSError:
                    ca_dst = None
                if ca_dst is not None:
                    # sandlock resolves http_inject_ca in the sandbox's view:
                    # the chroot-visible path, not the host path (the host
                    # path would be resolved under the rootfs and "not found").
                    ca_inside = (
                        Path("/workspace/.e2b-ca/ca-certificates.crt")
                        if kwargs.get("chroot")
                        else ca_dst
                    )
                    kwargs["http_inject_ca"] = [str(ca_inside)]
                    self._http_inject_env = {
                        "SSL_CERT_FILE": str(ca_inside),
                        "CURL_CA_BUNDLE": str(ca_inside),
                    }
        return kwargs

    def _build_instance_policy(self):
        """The ceiling as a native ``Sandbox`` policy object (or a plain
        namespace off-Linux, so the mapping stays unit-testable)."""
        kwargs = self._policy_ceiling()
        if sandlock is None:
            from types import SimpleNamespace

            return SimpleNamespace(**kwargs)
        return SandlockSandbox(**kwargs)

    def _build_sandbox(self, config: ExecConfig):
        """One-shot per-command ``Sandbox`` policy builder.

        Kept for the security/probe tests that still run one-shot sandboxes;
        production commands exec onto ``_build_instance_policy``'s ceiling
        through ``start()`` and carry cwd/env/clean_env/bind_ports per exec.
        """
        fs_writable = [self._workspace_dir]
        fs_writable.extend(self._extra_fs_writable)
        fs_readable = ["/usr", "/lib", "/bin", "/opt"]
        # Denials are only issued where the sandbox can actually see the
        # path: without a chroot, Landlock is an allow-list and shared paths
        # like /dev/shm are already unreachable (not in fs_readable), so
        # rules would add nothing -- and issuing them would cost the sandbox
        # its own file ownership: sandlock enforces denials through its
        # on-behalf open path, so every file the sandbox creates is then
        # attributed to the supervisor (host uid 0) instead of the sandbox
        # host uid, which silently voids both ``chmod`` inside the sandbox
        # and the per-uid isolation of shared volumes.
        fs_denied: list[str] = []
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: "/" resolves inside the chroot (the image
            # rootfs), so the whole image is readable as its own filesystem.
            # The host filesystem stays unreachable: the chroot restricts the
            # path space, and shared volumes are only exposed via their exact
            # fs_writable directory. minimal_dev mounts only the six /dev
            # nodes, so /dev/shm and /dev/mqueue never exist in the sandbox
            # view and need no carve-out; /proc/kcore and /sys stay denied
            # as defensive entries.
            fs_readable = list(fs_readable) + ["/"]
            fs_denied = ["/proc/kcore", "/sys"]
        net_allow: list[str] = []
        net_deny: list[str] = []
        http_allow: list[str] = []
        http_inject: list[dict] = []
        host_mask: str | None = None
        egress_proxy: dict | None = None
        if self._network:
            from gateway_common.network import sandlock_network_policy

            policy = sandlock_network_policy(
                self._network,
                allow_internet_access=self._allow_internet_access,
                enable_network=self._enable_network,
                private_deny_cidrs=list(self._network_deny_cidrs),
            )
            net_allow = policy["net_allow"]
            net_deny = policy["net_deny"]
            http_allow = policy["http_allow"]
            http_inject = self._materialize_http_inject(policy["http_inject"])
            host_mask = policy["host_mask"]
            egress_proxy = policy["egress_proxy"]
        elif self._allow_internet_access and self._enable_network:
            net_allow = [
                "files.pythonhosted.org:443",
                "pypi.org:443",
                "registry.npmjs.org:443",
                "proxy.golang.org:443",
                "static.crates.io:443",
                "github.com:443",
                "raw.githubusercontent.com:443",
            ]

        sandbox_uid, sandbox_gid = self._run_as_identity()
        kwargs: dict = {
            "fs_writable": fs_writable,
            "fs_readable": fs_readable,
            "fs_denied": fs_denied,
            "net_allow": net_allow,
            "net_deny": net_deny,
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": host_mask,
            "egress_proxy": egress_proxy,
            "max_memory": f"{self._memory_mb}M",
            "max_processes": self._max_processes,
            "max_open_files": self._max_open_files,
            "max_cpu": min(100, max(1, self._cpu_percent)),
            "max_disk": f"{self._disk_mb}M",
            "notify_rate_limit": self._notify_rate_limit or None,
            "clean_env": True,
            "env": dict(config.env),
            "cwd": config.cwd,
            "uid": sandbox_uid,
            "gid": sandbox_gid,
        }
        if "mcp-gateway" in " ".join(config.cmd):
            # The SDK starts the MCP gateway inside the sandbox; it must be
            # allowed to bind its HTTP port (per-sandbox MCP_PORT, since
            # sandboxes share the worker network namespace).
            mcp_port = str((config.env or {}).get("MCP_PORT", "50005"))
            kwargs["net_allow_bind"] = [mcp_port]
            if self._enable_net_isolation:
                # E7.1: under net_isolation the gateway listens inside the
                # sandbox's own loopback-only netns, unreachable from the
                # worker; S2.5 inbound mapping serves the sandbox's accept()
                # from a supervisor host-loopback listener on the same port,
                # which is what the /mcp proxy dials (127.0.0.1:<port>).
                port = int(mcp_port)
                self._port_mappings.setdefault(port, port)
        if self._enable_net_isolation:
            kwargs["net_isolation"] = True
            if self._port_mappings:
                kwargs["port_mappings"] = dict(self._port_mappings)
            if self._fd_inject_connect:
                kwargs["fd_inject_connect"] = True
            elif not getattr(type(self), "_netns_no_inject_warned", False):
                type(self)._netns_no_inject_warned = True
                logger.warning(
                    "net_isolation enabled without fd_inject_connect: "
                    "sandboxes are loopback-only (all external egress fails)"
                )
        elif self._fd_inject_connect:
            kwargs["fd_inject_connect"] = True
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: chroot into the extracted image and expose
            # the sandbox directory as /home/user (canonical alias: declared
            # first, and the fork breaks host-source ties by declaration
            # order) and /workspace (official SDK spelling) inside it.
            # fs_mount only takes effect at runtime, so the mount points must
            # already exist inside the rootfs for chdir() to work.
            # The rootfs must carry the standard /dev parent dir for
            # traversal and listings (minimal_dev provides the node names);
            # slim base images extract without one, so pre-create it like the
            # other mount points.
            for mount_point in ("workspace", "home/user", "dev"):
                Path(self._image_rootfs).joinpath(mount_point).mkdir(
                    parents=True, exist_ok=True
                )
            # Volume mount targets must exist inside the rootfs too.
            for virtual in self._fs_mounts:
                Path(self._image_rootfs).joinpath(
                    virtual.removeprefix("/")
                ).mkdir(parents=True, exist_ok=True)
            kwargs["chroot"] = str(self._image_rootfs)
            mount_map = {
                "/home/user": self._workspace_dir,
                "/workspace": self._workspace_dir,
            }
            mount_map.update(self._fs_mounts)
            # minimal_dev replaces the whole-tree host /dev mount: only the
            # six single-node mounts (ptmx, pts, null, urandom, zero, tty)
            # are visible under the chroot's /dev, so /dev/shm and
            # /dev/mqueue cannot leak in and no fs_deny carve-out is needed.
            # Native ExecStdio.PTY lives host-side, so no devpts node grants
            # are part of this shape either.
            mount_map.update(_minimal_dev_mounts())
            kwargs["fs_mount"] = mount_map
            cwd = (config.cwd or "").strip()
            if not cwd or cwd.startswith(str(self._workspace_dir)):
                kwargs["cwd"] = "/home/user"
            else:
                kwargs["cwd"] = cwd
        elif self._fs_mounts:
            # Without a chroot (pure Sandlock), virtual mount paths cannot be
            # materialized; volume mounts live inside the sandbox directory as
            # symlinks created by the control plane.
            pass
        if http_allow and self._image_rootfs is not None:
            # HTTPS MITM for rule-registered domains: sandlock intercepts 443
            # with an ephemeral CA; splice that CA into a per-sandbox copy of
            # the image trust bundle (never mutate the shared rootfs) and pin
            # the copy via SSL_CERT_FILE so in-sandbox clients trust it.
            ca_src = self._image_rootfs / "etc/ssl/certs/ca-certificates.crt"
            if ca_src.is_file():
                ca_dir = Path(self._workspace_dir) / ".e2b-ca"
                ca_dir.mkdir(parents=True, exist_ok=True)
                ca_dst = ca_dir / "ca-certificates.crt"
                try:
                    shutil.copy2(ca_src, ca_dst)
                except OSError:
                    ca_dst = None
                if ca_dst is not None:
                    # sandlock resolves http_inject_ca in the sandbox's view:
                    # the chroot-visible path, not the host path (the host
                    # path would be resolved under the rootfs and "not found").
                    ca_inside = (
                        Path("/workspace/.e2b-ca/ca-certificates.crt")
                        if kwargs.get("chroot")
                        else ca_dst
                    )
                    kwargs["http_inject_ca"] = [str(ca_inside)]
                    env = dict(kwargs.get("env") or {})
                    env["SSL_CERT_FILE"] = str(ca_inside)
                    env["CURL_CA_BUNDLE"] = str(ca_inside)
                    kwargs["env"] = env
        if sandlock is None:
            # Non-Linux / missing native library: return a plain object so the
            # policy mapping stays unit-testable without executing anything.
            from types import SimpleNamespace

            return SimpleNamespace(**kwargs)
        return SandlockSandbox(**kwargs)

    async def start(self, config: ExecConfig) -> SandlockRunningProcess:
        """Exec ``config.cmd`` onto the long-lived instance (M4 D3).

        PTY commands use the fork-native ``ExecStdio.PTY`` (host-side master,
        resized through ``ExecProcess.resize``) instead of the removed
        in-sandbox bridge; everything else uses ``ExecStdio.PIPED``. Per-exec
        cwd/env/clean_env/bind_ports come from ``_exec_params``. A
        closed/dead ``RuntimeError`` from ``inst.exec`` (idle-15min/24h
        instance expiry surfaces at exec time, not construction) rebuilds the
        instance exactly once under the lifecycle lock and retries the exec;
        after an explicit ``close()``/shutdown the executor fails loudly
        instead of rebuilding.
        """
        if sandlock is None:
            raise unimplemented("Sandlock is not available on this platform")
        if self._closed:
            raise RuntimeError(
                "sandlock executor is shut down; refusing to start a command "
                "on a rebuilt instance"
            )
        # The normal exec path takes the lifecycle lock around instance
        # creation too, so a concurrent ``update_network`` serializes against
        # it exactly like any other ``_ensure_instance`` caller -- but a
        # route-B creation (spawn + readiness probe) runs off the loop.
        inst = await self._ensure_instance_async()
        if inst is None:
            raise unimplemented("Sandlock is not available on this platform")
        stdio = ExecStdio.PTY if config.pty else ExecStdio.PIPED
        resolved = self.resolve_cmd(config.cmd)

        async def _exec_once(target) -> object:
            return await asyncio.to_thread(
                target.exec,
                resolved,
                stdio,
                **self._exec_params(
                    config, bind_ports=self._bind_ports_for(config)
                ),
            )

        try:
            proc = await _exec_once(inst)
        except RuntimeError as exc:
            message = str(exc)
            if "closed" not in message and "dead" not in message:
                logger.warning(
                    "sandlock exec failed sandbox_id=%s instance_name=%s "
                    "argv=%s error_type=%s error=%s",
                    self._sandbox_id or "-",
                    self.instance_name,
                    resolved,
                    type(exc).__name__,
                    exc,
                )
                raise
            # Idle/24h expiry or machinery death surfaced at exec time:
            # rebuild exactly once and retry. Never after an explicit close()
            # (a concurrent shutdown must not leak a fresh instance).
            reason = "closed" if "closed" in message else "dead"
            logger.info(
                "sandlock instance %s during exec; rebuilding once "
                "sandbox_id=%s instance_name=%s argv=%s",
                reason,
                self._sandbox_id or "-",
                self.instance_name,
                resolved,
            )
            inst = await self._reopen_instance_after(reason, inst)
            if inst is None:
                raise unimplemented("Sandlock is not available on this platform")
            # Exactly one retry; a second closed/dead failure propagates.
            proc = await _exec_once(inst)
        except Exception as exc:
            logger.warning(
                "sandlock exec failed sandbox_id=%s instance_name=%s argv=%s "
                "error_type=%s error=%s",
                self._sandbox_id or "-",
                self.instance_name,
                resolved,
                type(exc).__name__,
                exc,
            )
            raise
        # F4.3/S2 staleness mapping: register the fork child (id -> pid +
        # resolved argv) before the running process is returned so a later
        # ``update_network`` can log which children keep their old policy.
        self._child_registry[proc.child_id] = (proc.pid, resolved)
        queue: asyncio.Queue = asyncio.Queue()
        stdin_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_running_loop()
        running = SandlockRunningProcess(
            proc=proc,
            queue=queue,
            loop=loop,
            stdin_queue=stdin_queue,
            pty_mode=config.pty,
            on_exit=self._child_exited,
            # ``kill(sig)`` on an in-process child is always SIGKILL (see
            # ``SandlockRunningProcess.kill``); a slot child takes the signal
            # number through ``kill_child``, so the pause/resume fallback may
            # use it (M4 D5 / FUP #8).
            signal_pause_supported=self._route_b_active,
        )
        if config.pty:
            # The removed in-sandbox bridge applied the requested window size
            # at spawn; a fresh pty starts with the kernel default (0x0), so
            # apply the create-time rows/cols once before any output flows.
            running.resize(config.rows, config.cols)

        def _pump(stream, kind: str) -> None:
            if stream is None:
                loop.call_soon_threadsafe(running._mark_eof)
                return
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, (kind, chunk))
            except Exception:  # pragma: no cover - defensive
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("__eof__", kind))
                loop.call_soon_threadsafe(running._mark_eof)

        if config.pty:
            streams = [(proc.pty, "pty")]
        else:
            streams = [
                (proc.stdout, "stdout"),
                (proc.stderr, "stderr"),
            ]
        for stream, kind in streams:
            threading.Thread(
                target=_pump, args=(stream, kind), daemon=True
            ).start()

        return running
