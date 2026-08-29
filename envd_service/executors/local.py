"""Local executor: plain subprocess execution (macOS/dev fallback).

No Sandlock isolation — used only where Landlock/seccomp are unavailable.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty as pty_module
import struct
import termios
from collections.abc import AsyncIterator

from envd_service.executors.base import ExecConfig, Executor, RunningProcess


class LocalRunningProcess(RunningProcess):
    def __init__(
        self,
        *,
        proc: asyncio.subprocess.Process,
        queue: asyncio.Queue,
        output_task: asyncio.Task,
        stdin_writer: asyncio.StreamWriter | None,
        pty_master: int | None,
    ) -> None:
        self._proc = proc
        self._queue = queue
        self._output_task = output_task
        self._stdin_writer = stdin_writer
        self._pty_master = pty_master
        self._exit_code: int | None = None

    @property
    def pid(self) -> int:
        return self._proc.pid

    def output(self) -> AsyncIterator[tuple[str, bytes]]:
        return self._consume()

    async def _consume(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            if item[0] == "__eof__":
                continue
            yield item

    def send_stdin(self, data: bytes) -> None:
        if self._pty_master is not None:
            try:
                os.write(self._pty_master, data)
            except OSError:
                pass
            return
        if self._stdin_writer is not None and not self._stdin_writer.is_closing():
            self._stdin_writer.write(data)

    def close_stdin(self) -> None:
        if self._pty_master is not None:
            return
        if self._stdin_writer is not None and not self._stdin_writer.is_closing():
            self._stdin_writer.close()

    def resize(self, rows: int, cols: int) -> None:
        if self._pty_master is None:
            return
        try:
            fcntl.ioctl(
                self._pty_master,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0),
            )
        except OSError:
            pass

    def kill(self, sig: int) -> None:
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

    async def exit_code(self) -> int:
        if self._exit_code is not None:
            return self._exit_code
        code = await self._proc.wait()
        self._exit_code = code
        return code


class LocalExecutor(Executor):
    """Spawns commands as plain subprocesses on the host."""

    async def start(self, config: ExecConfig) -> LocalRunningProcess:
        env = dict(os.environ)
        env.update(config.env)
        kwargs: dict = {
            "cwd": config.cwd,
            "env": env,
            "start_new_session": True,
        }
        pty_master: int | None = None
        stdin_writer: asyncio.StreamWriter | None = None

        if config.pty:
            master, slave = pty_module.openpty()
            try:
                fcntl.ioctl(
                    master,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", config.rows, config.cols, 0, 0),
                )
            except OSError:
                pass
            pty_master = master
            slave_w = os.fdopen(slave, "w+b", buffering=0)
            kwargs["stdin"] = slave_w
            kwargs["stdout"] = slave_w
            kwargs["stderr"] = slave_w
            kwargs.pop("start_new_session", None)
            proc = await asyncio.create_subprocess_exec(*config.cmd, **kwargs)
        else:
            kwargs["stdin"] = (
                asyncio.subprocess.PIPE
                if config.stdin_enabled
                else asyncio.subprocess.DEVNULL
            )
            kwargs["stdout"] = asyncio.subprocess.PIPE
            kwargs["stderr"] = asyncio.subprocess.PIPE
            proc = await asyncio.create_subprocess_exec(*config.cmd, **kwargs)
            if config.stdin_enabled:
                stdin_writer = proc.stdin

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        readers: list[asyncio.Task] = []

        async def _read(stream, kind: str) -> None:
            try:
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        break
                    await queue.put((kind, chunk))
            finally:
                await queue.put(("__eof__", kind))

        if pty_master is not None:

            def _on_pty_readable() -> None:
                try:
                    chunk = os.read(pty_master, 65536)
                except OSError:
                    loop.remove_reader(pty_master)
                    asyncio.ensure_future(queue.put(("__eof__", "pty")))
                    return
                if not chunk:
                    loop.remove_reader(pty_master)
                    asyncio.ensure_future(queue.put(("__eof__", "pty")))
                else:
                    asyncio.ensure_future(queue.put(("pty", chunk)))

            loop.add_reader(pty_master, _on_pty_readable)
        else:
            readers.append(asyncio.create_task(_read(proc.stdout, "stdout")))
            readers.append(asyncio.create_task(_read(proc.stderr, "stderr")))

        async def _drive() -> None:
            if pty_master is not None:
                await proc.wait()
                try:
                    loop.remove_reader(pty_master)
                except (ValueError, OSError):
                    pass
                await queue.put(("__eof__", "pty"))
            else:
                await asyncio.gather(*readers)
                await proc.wait()
            await queue.put(None)

        output_task = asyncio.create_task(_drive())
        return LocalRunningProcess(
            proc=proc,
            queue=queue,
            output_task=output_task,
            stdin_writer=stdin_writer,
            pty_master=pty_master,
        )
