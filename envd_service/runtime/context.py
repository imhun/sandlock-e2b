"""Per-sandbox runtime context (executor + process manager + filesystem)."""

from __future__ import annotations

import json
import logging

from envd_service.config import Settings
from envd_service.executors.factory import create_executor
from envd_service.filesystem.ops import FilesystemOps
from envd_service.filesystem.watch import WatcherRegistry, WatchDirStream
from envd_service.process.logs import CommandLogWriter
from envd_service.process.manager import ProcessManager
from envd_service.runtime.registry import RuntimeSandbox

logger = logging.getLogger(__name__)


class SandboxRuntimeContext:
    """Everything needed to serve RPCs for one sandbox."""

    def __init__(self, record: RuntimeSandbox, settings: Settings) -> None:
        self.record = record
        self.executor = create_executor(
            settings,
            workspace_dir=record.workspace_dir,
            base_image=record.base_image,
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
                # /home/user, so mount paths map under it.
                f"/home/user/{m['path']}": m["hostPath"]
                for m in record.volume_mounts
            },
        )
        self.command_logs = CommandLogWriter(record.workspace_dir)
        self.processes = ProcessManager(
            self.executor,
            max_command_timeout=record.max_command_timeout,
            on_command_log=self._on_command_log,
        )
        self.files = FilesystemOps(record.workspace_dir)
        self.watchers = WatcherRegistry(self.files)
        self.watch_stream = WatchDirStream(self.files)
        self._mcp_gateway = None
        self._network = dict(record.network) if record.network else None

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
        if self._mcp_gateway is not None:
            try:
                self._mcp_gateway.kill(9)
            except Exception:
                pass
            self._mcp_gateway = None

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
        import os

        if not os.path.exists(gateway_bin):
            gateway_bin = "/usr/local/bin/mcp-gateway"
        config_json = json.dumps(config, separators=(",", ":"))
        proc = await self.executor.start(
            ExecConfig(
                cmd=[gateway_bin, "--config", config_json, "--foreground"],
                env={"GATEWAY_ACCESS_TOKEN": token},
                cwd=self.record.workspace_dir,
                stdin_enabled=False,
            )
        )
        self._mcp_gateway = proc
        return proc
