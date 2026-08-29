"""Process table and event fan-out for the envd service."""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass, field
from typing import Any

from gateway_common.errors import ConnectError, not_found
from envd_service.executors.base import (
    ExecConfig,
    Executor,
    FailedRunningProcess,
    RunningProcess,
)

logger = logging.getLogger(__name__)

SIGNAL_MAP: dict[str, int] = {
    "SIGNAL_SIGTERM": signal.SIGTERM,
    "SIGNAL_SIGKILL": signal.SIGKILL,
    "SIGTERM": signal.SIGTERM,
    "SIGKILL": signal.SIGKILL,
    "15": signal.SIGTERM,
    "9": signal.SIGKILL,
}


def parse_signal(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    key = str(value or "")
    if key in SIGNAL_MAP:
        return SIGNAL_MAP[key]
    raise ConnectError(
        "invalid_argument", f"unsupported signal: {value}", 400
    )


@dataclass
class ManagedProcess:
    pid: int
    config: ExecConfig
    tag: str | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    captured: dict[str, bytearray] = field(
        default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray(), "pty": bytearray()}
    )
    ended: bool = False
    exit_code: int | None = None
    killed: bool = False
    _running: RunningProcess | None = field(default=None, repr=False)

    def subscribe(self, *, replay: bool) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        if replay:
            for kind, buf in self.captured.items():
                data = bytes(buf)
                if data:
                    queue.put_nowait(("data", kind, data))
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self.subscribers.remove(queue)
        except ValueError:
            pass


class ProcessManager:
    """Owns all running commands/PTYs of one sandbox runtime."""

    def __init__(
        self,
        executor: Executor,
        max_command_timeout: int = 3600,
        on_command_log=None,
    ) -> None:
        self._executor = executor
        self._max_command_timeout = max_command_timeout
        self._processes: dict[int, ManagedProcess] = {}
        # Optional ``callback(proc, event, payload)`` for command output
        # logging; events are start/stdout/stderr/pty/end.
        self._on_command_log = on_command_log

    async def start(
        self,
        *,
        cmd: list[str],
        env: dict[str, str],
        cwd: str,
        stdin_enabled: bool,
        pty_size: tuple[int, int] | None = None,
        tag: str | None = None,
    ) -> ManagedProcess:
        pty = pty_size is not None
        rows, cols = pty_size or (24, 80)
        config = ExecConfig(
            cmd=list(cmd),
            env=dict(env),
            cwd=cwd,
            stdin_enabled=stdin_enabled,
            pty=pty,
            rows=rows,
            cols=cols,
        )
        try:
            running = await self._executor.start(config)
        except FileNotFoundError:
            running = FailedRunningProcess(
                f"envd: {cmd[0] if cmd else ''}: command not found\n"
            )
        except Exception as e:
            running = FailedRunningProcess(str(e) or type(e).__name__)
        proc = ManagedProcess(
            pid=running.pid,
            config=config,
            tag=tag,
            _running=running,
        )
        self._processes[proc.pid] = proc
        if self._on_command_log is not None:
            self._on_command_log(proc, "start", None)
        asyncio.create_task(self._drive(proc, running))
        return proc

    async def _drive(self, proc: ManagedProcess, running: RunningProcess) -> None:
        timed_out = False

        async def _watchdog() -> None:
            nonlocal timed_out
            try:
                await asyncio.sleep(self._max_command_timeout)
                timed_out = True
                running.kill(signal.SIGKILL)
            except asyncio.CancelledError:
                pass

        watchdog = asyncio.create_task(_watchdog())
        try:
            async for kind, chunk in running.output():
                if kind not in ("stdout", "stderr", "pty"):
                    continue
                buf = proc.captured.get(kind)
                if buf is not None:
                    buf.extend(chunk)
                if self._on_command_log is not None:
                    self._on_command_log(proc, kind, chunk)
                self._broadcast(proc, ("data", kind, chunk))
            exit_code = await running.exit_code()
        except asyncio.CancelledError:
            running.kill(signal.SIGKILL)
            watchdog.cancel()
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("process %s output loop failed", proc.pid)
            exit_code = -1
        finally:
            watchdog.cancel()

        proc.exit_code = exit_code
        proc.ended = True
        status = "killed" if (timed_out or exit_code < 0) else "exited"
        if self._on_command_log is not None:
            self._on_command_log(proc, "end", exit_code)
        self._broadcast(proc, ("end", exit_code, status))
        self._processes.pop(proc.pid, None)

    @staticmethod
    def _broadcast(proc: ManagedProcess, item: tuple) -> None:
        for queue in list(proc.subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover - unbounded queues
                pass

    def get(self, pid: int) -> ManagedProcess:
        proc = self._processes.get(pid)
        if proc is None:
            raise not_found(f"Process {pid} not found")
        return proc

    def get_or_none(self, pid: int) -> ManagedProcess | None:
        return self._processes.get(pid)

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for proc in self._processes.values():
            cfg = proc.config
            item: dict[str, Any] = {
                "config": {
                    "cmd": cfg.cmd[0] if cfg.cmd else "",
                    "args": cfg.cmd[1:] if len(cfg.cmd) > 1 else [],
                    "envs": cfg.env,
                    "cwd": cfg.cwd,
                },
                "pid": proc.pid,
            }
            if proc.tag:
                item["tag"] = proc.tag
            out.append(item)
        return out

    def send_input(self, pid: int, data: bytes) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.send_stdin(data)

    def close_stdin(self, pid: int) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.close_stdin()

    def send_signal(self, pid: int, sig: int) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.kill(sig)
        if sig == signal.SIGKILL:
            # Deterministic second-kill semantics: mark dead immediately so a
            # subsequent kill returns NOT_FOUND while the end event still
            # reaches live streams through the driver.
            proc.killed = True
            self._processes.pop(pid, None)

    def update(self, pid: int, rows: int, cols: int) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.resize(rows, cols)

    def kill_all(self) -> None:
        for pid, proc in list(self._processes.items()):
            if proc._running is not None:
                proc._running.kill(signal.SIGKILL)
        self._processes.clear()

    def pause_all(self) -> None:
        """Freeze every running process tree (SIGSTOP)."""
        for proc in list(self._processes.values()):
            if proc._running is None:
                continue
            try:
                import os

                os.killpg(os.getpgid(proc.pid), signal.SIGSTOP)
            except (ProcessLookupError, PermissionError):
                try:
                    proc._running.kill(signal.SIGSTOP)
                except Exception:
                    pass

    def resume_all(self) -> None:
        """Unfreeze every running process tree (SIGCONT)."""
        for proc in list(self._processes.values()):
            if proc._running is None:
                continue
            try:
                import os

                os.killpg(os.getpgid(proc.pid), signal.SIGCONT)
            except (ProcessLookupError, PermissionError):
                try:
                    proc._running.kill(signal.SIGCONT)
                except Exception:
                    pass

    async def wait_ended(self, proc: ManagedProcess, queue: asyncio.Queue):
        """Consume a subscriber queue until the end event."""
        while True:
            item = await queue.get()
            if item[0] == "end":
                return item
