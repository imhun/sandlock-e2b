"""Per-sandbox runtime context (executor + process manager + filesystem)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from envd_service.config import Settings
from envd_service.executors.factory import create_executor
from envd_service.filesystem.ops import FilesystemOps
from envd_service.filesystem.watch import WatcherRegistry, WatchDirStream
from envd_service.process.logs import CommandLogWriter
from envd_service.process.manager import ProcessManager
from envd_service.runtime.registry import RuntimeSandbox

from gateway_common.paths import sandbox_runtime_dir

logger = logging.getLogger(__name__)

#: First port the per-sandbox MCP gateway pool hands out, and the last one.
#:
#: The band is squeezed between two constraints, and only one window satisfies
#: both:
#:
#: * **at least 50005** -- under net_isolation the supervisor serves the
#:   sandbox's ``accept()`` from a host-loopback listener on the same number,
#:   and sandlock refuses a ``net_bind_map`` host port below 50005
#:   (``net_bind_map: host port ... is below the reserved inbound mapping range
#:   (50005+)``, ``sandlock-core/src/sandbox/builder.rs``);
#: * **outside the kernel's ephemeral range** -- ``ip_local_port_range`` is
#:   measured as ``32768-60999`` inside the containers, and every outgoing
#:   connection draws its *source* port from there without asking anyone.
#:
#: So the band has to start *above* the ephemeral top: ``61000-65535`` is the
#: whole window that is both >= 50005 and clear of the range the kernel hands
#: out on its own. The base was ``51000`` -- inside the ephemeral range, where
#: the suite's own loopback traffic is given the same numbers, and the gateway
#: only finds out when it binds, deep inside the sandbox (flake #2's class).
#: The band is bounded so the pool can never drift back down into that range: a
#: worker that runs out fails loudly instead of borrowing a shared port.
_MCP_PORT_BASE = 61000
_MCP_PORT_MAX = 65535

#: glibc hands every new thread its own malloc arena, and a fresh arena
#: reserves 64 MiB of anonymous address space up front. sandlock charges
#: anonymous *reservations*, not touched pages (it is cgroup-less and runs the
#: limit from seccomp notifications on mmap/brk:
#: ``sandlock-core/src/resource.rs::handle_memory``), so those reservations come
#: straight out of the sandbox's memory budget: measured on the target
#: 2026-09-16 in a 512MB box, one extra thread cost +72 MiB (64 arena + 8 stack)
#: and three cost +216 MiB, while the same three with this variable set cost
#: +24 MiB. It is what made an MCP stdio server top out near 110 MiB of payload;
#: with it the same server held 300 MiB (340 MiB was killed). The server is
#: spawned by the gateway, so it inherits this -- and the gateway itself, being
#: an I/O-bound proxy, is the one process the trade-off (all threads sharing one
#: arena) is safe for. Keep it here, not in the sandbox's general env: a
#: user workload that allocates from many threads would pay the contention.
_MCP_GATEWAY_MALLOC_ARENA_MAX = "1"

_GATEWAY_STDERR_TAIL_BYTES = 4096
#: Pinned prefix of the SDK-visible text for a gateway that never served
#: (FUP #4 / Task D1): a contract test asserts this string verbatim.
MCP_GATEWAY_FAILURE_PREFIX = "mcp gateway failed to start"


@dataclass(frozen=True)
class McpGatewayFailure:
    """A gateway process that died before the SDK could use it (FUP #4).

    ``text`` is the exact string the SDK sees as the whole stderr of the next
    command (the watcher's summary of the death: sandbox identity, exit code
    and the gateway's own stderr tail); ``exit_code`` is that process's own
    non-zero code and is replayed as the command's exit code, so a caller that
    only inspects the exit code still fails closed.
    """

    text: str
    exit_code: int


async def _watch_mcp_gateway_exit(
    proc,
    *,
    sandbox_id: str,
    port: int,
    on_failure: Callable[[McpGatewayFailure], None] | None = None,
) -> None:
    """Log a gateway process that terminates with its stderr/exit text.

    Task 10 found gateway start failures were silent: the SDK's gateway start
    command reports an immediate exit-0 while the real gateway process keeps
    running, and nothing consumes its streams, so an early non-zero exit (e.g.
    a missing interpreter or a bad config) vanished. This watcher drains the
    gateway output (bounded stderr tail) and logs the exit code plus stderr
    text when the process ends.

    FUP #4 (Task D1): a non-zero exit is also handed to ``on_failure`` as a
    typed :class:`McpGatewayFailure`, which is what makes the death visible to
    the SDK -- the sandbox's command path replays that record instead of
    reporting success. A clean exit-0 teardown is not a failure.
    The task is cancelled by ``shutdown()`` before the gateway is killed, so a
    normal teardown SIGKILL is not reported as a failure.
    """
    stderr_tail = bytearray()
    try:
        async for kind, chunk in proc.output():
            if kind == "stderr":
                stderr_tail.extend(chunk)
                if len(stderr_tail) > _GATEWAY_STDERR_TAIL_BYTES:
                    del stderr_tail[
                        : len(stderr_tail) - _GATEWAY_STDERR_TAIL_BYTES
                    ]
        code = await proc.exit_code()
    except asyncio.CancelledError:
        return
    except Exception as exc:  # noqa: BLE001 - never kill the worker watcher
        logger.error(
            "MCP gateway watch failed sandbox_id=%s port=%s error_type=%s "
            "error=%s",
            sandbox_id,
            port,
            type(exc).__name__,
            exc,
        )
        return
    if code == 0:
        logger.info(
            "MCP gateway exited sandbox_id=%s port=%s exit_code=0",
            sandbox_id,
            port,
        )
        return
    stderr_text = stderr_tail.decode("utf-8", "replace")
    if on_failure is not None:
        on_failure(
            McpGatewayFailure(
                text=(
                    f"{MCP_GATEWAY_FAILURE_PREFIX} sandbox_id={sandbox_id} "
                    f"port={port} exit_code={code} stderr={stderr_text!r}"
                ),
                exit_code=code,
            )
        )
    logger.error(
        "MCP gateway exited sandbox_id=%s port=%s exit_code=%d stderr=%r",
        sandbox_id,
        port,
        code,
        stderr_text,
    )


def _port_bindable(port: int) -> bool:
    """Whether ``port`` can be bound *right now* in this network namespace.

    The gateway binds ``0.0.0.0:<port>`` inside the sandbox (and under
    net_isolation the supervisor maps the same number on the worker's
    loopback, which is what the ``/mcp`` proxy dials), so the probe binds the
    same wildcard address -- a specific bind would miss a wildcard listener.
    ``SO_REUSEADDR`` mirrors what asyncio/uvicorn set, so a port a dead
    gateway left in ``TIME_WAIT`` still counts as free while a live listener
    never does.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


class McpPortPool:
    """Allocates per-sandbox MCP gateway ports and reuses freed ones (E6.3).

    Sandboxes share the worker network namespace, so each MCP gateway needs
    a distinct host port. The old allocator only ever incremented, so a
    long-lived worker's port numbers drifted upward forever. This pool keeps
    a monotonic counter plus a free set: ``release`` returns a port to the
    pool and the next ``allocate`` reuses it. Allocation and release are
    serialized under a lock, so concurrent sandbox create/delete cannot hand
    out the same port twice.

    Every candidate is *verified* bindable before it is handed out (see
    :func:`_port_bindable`): a number that another listener already holds --
    the pool only knows about ports it handed out itself -- must be skipped
    here, not inside the sandbox where the gateway's bind fails.
    """

    def __init__(
        self, base: int = _MCP_PORT_BASE, max_port: int = _MCP_PORT_MAX
    ) -> None:
        self._base = base
        self._max = max_port
        self._counter = 0
        self._free: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self) -> int:
        with self._lock:
            while True:
                candidate = self._next_candidate()
                if _port_bindable(candidate):
                    return candidate
                logger.warning(
                    "MCP gateway port %d is already held; skipping it", candidate
                )

    def _next_candidate(self) -> int:
        """The next port to try: a recycled one, else the next unused one.

        Caller holds the lock. A recycled port that turns out to be taken is
        dropped, not put back: the pool must not remember a number that is no
        longer its to give.
        """
        if self._free:
            return self._free.pop()
        if self._base + self._counter >= self._max:
            raise RuntimeError(
                f"the MCP gateway port band {self._base + 1}-{self._max} is "
                f"exhausted ({self._counter} ports held); refusing to allocate "
                "outside the band, where the kernel's ephemeral range hands out "
                "the same numbers to outgoing connections"
            )
        self._counter += 1
        return self._base + self._counter

    def release(self, port: int | None) -> None:
        """Return ``port`` to the free set, ignoring out-of-range values
        (never-allocated ports) and duplicate releases."""
        if port is None:
            return
        with self._lock:
            if port <= self._base or port > self._base + self._counter:
                return
            self._free.add(port)

    def stats(self) -> dict[str, int]:
        """Snapshot of the band, for the node's watermark (N8).

        ``in_use`` is the number an operator watches: every port ever handed
        out minus the ones released back, i.e. what live sandboxes hold
        against the band. ``highest`` is the monotonic counter (it never
        decreases), so "nearly exhausted" is distinguishable from "recycled a
        lot"; ``capacity`` is the band's size (the hard ceiling
        ``allocate`` refuses past).
        """
        with self._lock:
            return {
                "capacity": self._max - self._base,
                "in_use": self._counter - len(self._free),
                "highest": self._counter,
                "free": len(self._free),
            }


_mcp_port_pool = McpPortPool()


def _next_mcp_port() -> int:
    return _mcp_port_pool.allocate()


def mcp_port_stats() -> dict[str, int]:
    """The worker's MCP port-band snapshot (see :meth:`McpPortPool.stats`)."""
    return _mcp_port_pool.stats()


def _release_mcp_port(port: int | None) -> None:
    _mcp_port_pool.release(port)


class SandboxRuntimeContext:
    """Everything needed to serve RPCs for one sandbox."""

    def __init__(self, record: RuntimeSandbox, settings: Settings) -> None:
        self.record = record
        self.executor = create_executor(
            settings,
            workspace_dir=record.workspace_dir,
            sandbox_id=record.sandbox_id,
            base_image=record.base_image,
            host_uid=record.host_uid,
            per_sandbox_uid=settings.per_sandbox_uid,
            memory_mb=record.memory_mb,
            cpu_percent=record.cpu_percent,
            disk_mb=record.disk_mb,
            max_processes=record.max_processes,
            max_open_files=record.max_open_files,
            allow_internet_access=record.allow_internet_access,
            network=record.network,
            iam_tokens=record.iam_tokens,
            egress_lib_dir=settings.image_cache_dir / "egress",
            extra_fs_writable=[m["hostPath"] for m in record.volume_mounts],
            fs_mounts={
                # Every volume view must be registered under BOTH workspace
                # aliases (they are the same host directory). A cwd-derived
                # relative open resolves against whichever alias the sandbox
                # sits in, and only that alias's sub-mount can serve it --
                # evidence: tmp/vol_fs_mount_probe.py.
                **{
                    alias: m["hostPath"]
                    for m in record.volume_mounts
                    for alias in (
                        f"/workspace/{m['path']}",
                        f"/home/user/{m['path']}",
                    )
                },
            },
        )
        # Platform file: written beside the sandbox's tree, not inside it (the
        # sandbox owns its tree and could delete or rewrite anything there).
        self.command_logs = CommandLogWriter(
            sandbox_runtime_dir(Path(record.workspace_dir).parent, record.sandbox_id)
        )
        self.processes = ProcessManager(
            self.executor,
            max_command_timeout=record.max_command_timeout,
            on_command_log=self._on_command_log,
            sandbox_id=record.sandbox_id,
            max_concurrent_commands=settings.max_concurrent_commands_per_sandbox,
            max_queued_commands=settings.max_queued_commands_per_sandbox,
            queue_timeout_s=settings.command_queue_timeout_s,
            capture_limit_bytes=(
                None
                if settings.command_capture_limit_mb <= 0
                else settings.command_capture_limit_mb * 1024 * 1024
            ),
        )
        self.files = FilesystemOps(record.workspace_dir)
        self.watchers = WatcherRegistry(self.files)
        self.watch_stream = WatchDirStream(self.files)
        self._mcp_gateway = None
        self._mcp_gateway_watch: asyncio.Task | None = None
        self._mcp_gateway_failure: McpGatewayFailure | None = None
        self._mcp_port: int | None = None
        self._mcp_token: str | None = None
        self._network = dict(record.network) if record.network else None
        if record.mcp:
            # M4 D3: pre-allocate the MCP gateway port at context creation so
            # the executor's instance ceiling (``net_allow_bind``) is fixed
            # before the first exec; ``start_mcp_gateway`` consumes it.
            port = _next_mcp_port()
            self._mcp_port = port
            setter = getattr(self.executor, "set_mcp_bind_port", None)
            if setter is not None:
                setter(port)

    @property
    def mcp_port(self) -> int | None:
        return self._mcp_port

    @property
    def mcp_token(self) -> str | None:
        return self._mcp_token

    @property
    def mcp_gateway_failure(self) -> McpGatewayFailure | None:
        """FUP #4: the gateway death this sandbox must surface, if any.

        Steady state is ``None``. Once the gateway watcher sees a non-zero
        exit the record is kept for the sandbox's whole life: the command path
        reads it to fail closed with the recorded reason, and (like the port)
        it survives a failed start so a retry cannot clear the diagnosis.
        """
        return self._mcp_gateway_failure

    def _record_mcp_gateway_failure(self, failure: McpGatewayFailure) -> None:
        # The watcher is an asyncio task on the app's event loop, exactly like
        # every RPC handler that reads this, so the assignment needs no lock.
        # First failure wins: a later death (or a retry) must not rewrite the
        # reason the SDK was already told about.
        if self._mcp_gateway_failure is None:
            self._mcp_gateway_failure = failure

    def update_network(self, network: dict | None) -> None:
        """Apply an updated network config; the next command uses it.

        D4=A: the executor validates and applies first and raises
        ``NetworkUpdateConflictError`` when the update is not expressible on
        a launched instance. The record and this context's network copy are
        persisted only after a successful apply, so a rejection leaves both
        unchanged (HTTP 409 without record mutation).
        """
        merged = dict(network) if network else None
        updater = getattr(self.executor, "update_network", None)
        if updater is not None:
            updater(merged)
        if merged is not None and "allowInternetAccess" in merged:
            self.record.allow_internet_access = bool(merged["allowInternetAccess"])
        self.record.network = merged
        self._network = merged

    def _on_command_log(self, proc, event, payload) -> None:
        writer = self.command_logs
        if event == "start":
            writer.start(proc.pid, proc.config.cmd)
        elif event in ("stdout", "stderr", "pty"):
            writer.write(proc.pid, event, payload)
        elif event == "end":
            writer.end(proc.pid, payload)

    def shutdown(self) -> None:
        self.processes.kill_all()
        self.processes.remove_sandbox()
        if self._mcp_gateway_watch is not None:
            self._mcp_gateway_watch.cancel()
            self._mcp_gateway_watch = None
        if self._mcp_gateway is not None:
            try:
                self._mcp_gateway.kill(9)
            except Exception:
                pass
            self._mcp_gateway = None
        if self._mcp_port is not None:
            _release_mcp_port(self._mcp_port)
            self._mcp_port = None
        # Release the long-lived exec instance (idempotent). The getattr
        # keeps duck-typed fakes safe; the base Executor no-op covers the
        # stateless backends.
        closer = getattr(self.executor, "close", None)
        if closer is not None:
            closer()

    def pause(self) -> None:
        self.processes.pause_all()

    def resume(self) -> None:
        self.processes.resume_all()

    async def start_mcp_gateway(self, config: dict, token: str):
        """Start the MCP gateway as a long-running sandbox process.

        Returns the live ``RunningProcess``; the caller reports an immediate
        exit-0 to the SDK so ``Sandbox.create(mcp=...)`` does not block on the
        gateway's lifetime.
        """
        if self._mcp_gateway is not None:
            return self._mcp_gateway
        from envd_service.executors.base import ExecConfig

        gateway_bin = "/usr/bin/mcp-gateway"
        if not os.path.exists(gateway_bin):
            gateway_bin = "/usr/local/bin/mcp-gateway"
        # The stdio server needs the same pin, and it cannot inherit it: the
        # SDK's stdio client forwards only its DEFAULT_INHERITED_ENV_VARS
        # (HOME/LOGNAME/PATH/SHELL/TERM/USER) and merges the configured
        # ``envs`` on top, so an explicit entry is the only way through. A
        # value the caller set keeps precedence.
        mcp_envs = dict(config.get("envs") or {})
        mcp_envs.setdefault("MALLOC_ARENA_MAX", _MCP_GATEWAY_MALLOC_ARENA_MAX)
        config = {**config, "envs": mcp_envs}
        config_json = json.dumps(config, separators=(",", ":"))
        port = self._mcp_port
        if port is None:
            # Defensive fallback: a sandbox without ``record.mcp`` was still
            # asked to run the gateway. If the executor instance already
            # exists its bind ceiling is fixed and cannot widen (M4 D3), so
            # fail loudly instead of letting the exec die on a deep EPERM.
            # Before the instance exists, allocate on first start so the
            # executor ceiling gets the port before the exec.
            holder = getattr(self.executor, "instance_handle", None)
            if holder is not None:
                raise RuntimeError(
                    "MCP gateway requested after the executor instance was "
                    "created without a bind allowance; the instance ceiling "
                    "cannot widen (M4 D3)"
                )
            port = _next_mcp_port()
            self._mcp_port = port
            setter = getattr(self.executor, "set_mcp_bind_port", None)
            if setter is not None:
                setter(port)
        self._mcp_token = token
        # The SDK reads the gateway token from /etc/mcp-gateway/.token via the
        # files API (resolved under the sandbox workspace).
        token_dir = Path(self.record.workspace_dir) / "etc" / "mcp-gateway"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / ".token").write_text(token, encoding="utf-8")
        try:
            proc = await self.executor.start(
                ExecConfig(
                    # Run the gateway through the interpreter explicitly:
                    # the sandlock chroot exec handler supports ELF binaries
                    # only, so a shebang script cannot be exec'd directly in
                    # image rootfs mode (EACCES on the script path).
                    cmd=[
                        "/usr/local/bin/python3",
                        gateway_bin,
                        "--config",
                        config_json,
                        "--foreground",
                    ],
                    env={
                        "GATEWAY_ACCESS_TOKEN": token,
                        "MCP_PORT": str(port),
                        # clean_env strips PATH; the gateway spawns the
                        # configured MCP server by command name (e.g.
                        # python3) via stdio.
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        # See _MCP_GATEWAY_MALLOC_ARENA_MAX: this is what keeps
                        # the gateway's and the stdio server's thread counts out
                        # of the sandbox's (reservation-based) memory budget.
                        "MALLOC_ARENA_MAX": _MCP_GATEWAY_MALLOC_ARENA_MAX,
                    },
                    cwd=self.record.workspace_dir,
                    stdin_enabled=False,
                )
            )
        except BaseException as exc:
            # M4 D3: the allocated port is this sandbox's instance bind
            # ceiling (pushed before the first exec), so it stays allocated
            # for the sandbox even when this start fails -- a retry must
            # reuse the same port or the exec would exceed the fixed
            # ceiling. shutdown() returns it to the pool.
            logger.error(
                "MCP gateway start failed sandbox_id=%s port=%s "
                "error_type=%s error=%s",
                self.record.sandbox_id,
                port,
                type(exc).__name__,
                exc,
            )
            self._mcp_token = None
            raise
        self._mcp_gateway = proc
        # Task 10: gateway failures are silent to the SDK, so watch the real
        # process and log non-zero exits with their stderr text (logging only).
        if getattr(proc, "output", None) is not None:
            self._mcp_gateway_watch = asyncio.create_task(
                _watch_mcp_gateway_exit(
                    proc,
                    sandbox_id=self.record.sandbox_id,
                    port=port,
                    on_failure=self._record_mcp_gateway_failure,
                )
            )
        return proc
