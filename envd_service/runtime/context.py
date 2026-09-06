"""Per-sandbox runtime context (executor + process manager + filesystem)."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from envd_service.config import Settings
from envd_service.executors.factory import create_executor
from envd_service.filesystem.ops import FilesystemOps
from envd_service.filesystem.watch import WatcherRegistry, WatchDirStream
from envd_service.process.logs import CommandLogWriter
from envd_service.process.manager import ProcessManager
from envd_service.runtime.registry import RuntimeSandbox

logger = logging.getLogger(__name__)

_MCP_PORT_BASE = 51000


class McpPortPool:
    """Allocates per-sandbox MCP gateway ports and reuses freed ones (E6.3).

    Sandboxes share the worker network namespace, so each MCP gateway needs
    a distinct host port. The old allocator only ever incremented, so a
    long-lived worker's port numbers drifted upward forever. This pool keeps
    a monotonic counter plus a free set: ``release`` returns a port to the
    pool and the next ``allocate`` reuses it. Allocation and release are
    serialized under a lock, so concurrent sandbox create/delete cannot hand
    out the same port twice.
    """

    def __init__(self, base: int = _MCP_PORT_BASE) -> None:
        self._base = base
        self._counter = 0
        self._free: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self) -> int:
        with self._lock:
            if self._free:
                return self._free.pop()
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


_mcp_port_pool = McpPortPool()


def _next_mcp_port() -> int:
    return _mcp_port_pool.allocate()


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
                # Inside the image-rootfs chroot the sandbox directory is
                # /workspace (official SDK default cwd), so mount paths map
                # under it.
                f"/workspace/{m['path']}": m["hostPath"]
                for m in record.volume_mounts
            },
        )
        self.command_logs = CommandLogWriter(record.workspace_dir)
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

    def update_network(self, network: dict | None) -> None:
        """Apply an updated network config; the next command uses it."""
        self.record.network = dict(network) if network else None
        self._network = self.record.network
        updater = getattr(self.executor, "update_network", None)
        if updater is not None:
            updater(self.record.network)

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
                    },
                    cwd=self.record.workspace_dir,
                    stdin_enabled=False,
                )
            )
        except BaseException:
            # M4 D3: the allocated port is this sandbox's instance bind
            # ceiling (pushed before the first exec), so it stays allocated
            # for the sandbox even when this start fails -- a retry must
            # reuse the same port or the exec would exceed the fixed
            # ceiling. shutdown() returns it to the pool.
            self._mcp_token = None
            raise
        self._mcp_gateway = proc
        return proc
