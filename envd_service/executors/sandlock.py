"""Sandlock executor: Landlock + seccomp-bpf + seccomp user notification.

Requires Linux with Landlock ABI >= 6 and ``sandlock==0.8.6``. The Sandbox
instance policy maps directly from the E2B sandbox configuration (spec
section 6.4). A fresh Sandbox instance is created per command, matching
sandlock's one-running-process-per-instance contract; the E2B sandbox
directory is shared across instances via ``fs_writable`` (no COW).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from gateway_common.errors import ConnectError, unimplemented
from envd_service.executors.base import ExecConfig, Executor, RunningProcess

logger = logging.getLogger(__name__)

# Runs inside the sandbox (as the sandlock child). It creates a real PTY,
# attaches the actual command to the slave side, and forwards the master side
# over the sandlock PIPED stdio. Window resizes arrive as in-band control
# frames on stdin:
#
#     ESC [ E2BRESIZE:<rows>,<cols> BEL
#
# The child stays in the same process group as the bridge so sandlock's
# process-group kill tears down the whole tree.
PTY_BRIDGE_SCRIPT = r'''
import fcntl, json, os, select, signal, struct, sys, termios

FRAME_START = b"\x1b[E2BRESIZE:"
FRAME_END = b"\x07"


def main() -> int:
    cmd = json.loads(sys.argv[1])
    rows, cols = int(sys.argv[2]), int(sys.argv[3])
    master, slave = os.openpty()
    try:
        fcntl.ioctl(
            slave, termios.TIOCSWINSZ,
            struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0),
        )
    except OSError:
        pass
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        except OSError:
            pass
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(master)
        os.close(slave)
        try:
            os.execvp(cmd[0], cmd)
        except OSError:
            os._exit(127)
    os.close(slave)
    pending = b""
    try:
        while True:
            r, _, _ = select.select([master, sys.stdin.buffer], [], [])
            if master in r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            if sys.stdin.buffer in r:
                data = os.read(sys.stdin.buffer.fileno(), 65536)
                if not data:
                    continue
                pending += data
                while True:
                    start = pending.find(FRAME_START)
                    if start < 0:
                        keep = max(0, len(FRAME_START) - 1)
                        flush = pending[:-keep] if len(pending) > keep else b""
                        if flush:
                            os.write(master, flush)
                        pending = pending[-keep:] if keep else b""
                        break
                    if start > 0:
                        os.write(master, pending[:start])
                    end = pending.find(FRAME_END, start)
                    if end < 0:
                        pending = pending[start:]
                        break
                    spec = pending[start + len(FRAME_START):end]
                    pending = pending[end + 1:]
                    try:
                        r_, c_ = spec.split(b",")
                        fcntl.ioctl(
                            slave, termios.TIOCSWINSZ,
                            struct.pack("HHHH", int(r_), int(c_), 0, 0),
                        )
                        os.kill(pid, signal.SIGWINCH)
                    except (OSError, ValueError):
                        pass
    except KeyboardInterrupt:
        pass
    try:
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return 1
    except ChildProcessError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
'''

try:  # sandlock is Linux-only; keep the import optional for macOS dev.
    import sandlock
    from sandlock import BranchAction, Sandbox as SandlockSandbox, StdioMode
except Exception:  # pragma: no cover - macOS / missing package
    sandlock = None  # type: ignore[assignment]
    BranchAction = None  # type: ignore[assignment]
    StdioMode = None  # type: ignore[assignment]
    SandlockSandbox = None  # type: ignore[assignment]


class SandlockRunningProcess(RunningProcess):
    def __init__(
        self,
        *,
        proc,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        stdin_queue: asyncio.Queue,
        pty_mode: bool = False,
    ) -> None:
        self._proc = proc
        self._queue = queue
        self._loop = loop
        self._stdin_queue = stdin_queue
        self._pty_mode = pty_mode
        self._writer_thread: threading.Thread | None = None
        self._closed = False
        self._eof_count = 0

    @property
    def pid(self) -> int:
        pid = self._proc.pid
        return pid if pid is not None else -1

    def output(self) -> AsyncIterator[tuple[str, bytes]]:
        return self._consume()

    async def _consume(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    def _start_stdin_writer(self) -> None:
        if self._writer_thread is not None or self._proc.stdin is None:
            return

        def _write_loop() -> None:
            try:
                while True:
                    data = asyncio.run_coroutine_threadsafe(
                        self._stdin_queue.get(), self._loop
                    ).result()
                    if data is None:
                        try:
                            self._proc.stdin.close()
                        except OSError:
                            pass
                        return
                    try:
                        self._proc.stdin.write(data)
                    except (OSError, ValueError):
                        return
            except Exception:  # pragma: no cover - defensive
                logger.exception("sandlock stdin writer failed")

        self._writer_thread = threading.Thread(target=_write_loop, daemon=True)
        self._writer_thread.start()

    def send_stdin(self, data: bytes) -> None:
        if self._closed:
            return
        self._start_stdin_writer()
        try:
            self._stdin_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def close_stdin(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stdin_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def resize(self, rows: int, cols: int) -> None:
        if self._closed:
            return
        frame = f"\x1b[E2BRESIZE:{rows},{cols}\x07".encode("ascii")
        self._start_stdin_writer()
        try:
            self._stdin_queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    def kill(self, sig: int) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass

    def _mark_eof(self) -> None:
        """Signal the end of the output stream once both pipes hit EOF.

        The process has exited by then (it closed stdout/stderr), so the
        ProcessManager's ``exit_code()`` -> sandlock ``wait()`` can reap it
        without closing a still-open stdin first (which would send EOF to
        interactive children like ``cat``).
        """
        self._eof_count += 1
        if self._eof_count >= 2:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def exit_code(self) -> int:
        result = await asyncio.to_thread(self._proc.wait)
        return result.exit_code


class SandlockExecutor(Executor):
    """Runs each command inside a fresh ``sandlock.Sandbox`` instance."""

    def __init__(
        self,
        *,
        workspace_dir: str,
        base_image: str | None,
        image_rootfs: Path | None,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        max_processes: int,
        max_open_files: int,
        allow_internet_access: bool,
        enable_network: bool,
        extra_fs_writable: list[str] | None = None,
        fs_mounts: dict[str, str] | None = None,
    ) -> None:
        self._workspace_dir = workspace_dir
        self._base_image = base_image
        self._image_rootfs = image_rootfs
        self._memory_mb = memory_mb
        self._cpu_percent = cpu_percent
        self._disk_mb = disk_mb
        self._max_processes = max_processes
        self._max_open_files = max_open_files
        self._allow_internet_access = allow_internet_access
        self._enable_network = enable_network
        self._extra_fs_writable = list(extra_fs_writable or [])
        self._fs_mounts = dict(fs_mounts or {})

    @staticmethod
    def resolve_cmd(cmd: list[str]) -> list[str]:
        """Translate ``/bin/bash`` to ``/bin/sh`` when bash is unavailable
        (slim base images do not ship bash; the official SDK always sends
        ``cmd=/bin/bash``)."""
        if cmd and cmd[0] == "/bin/bash":
            return ["/bin/sh"] + cmd[1:]
        return cmd

    def _build_sandbox(self, config: ExecConfig):
        fs_writable = [self._workspace_dir]
        fs_writable.extend(self._extra_fs_writable)
        fs_readable = ["/usr", "/lib", "/bin", "/opt"]
        fs_denied = ["/proc/kcore", "/sys"]
        if config.pty:
            # The in-sandbox PTY bridge needs the pty device nodes.
            fs_writable += ["/dev/ptmx", "/dev/pts"]
            fs_readable += ["/dev/ptmx", "/dev/pts"]
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: "/" resolves inside the chroot (the image
            # rootfs), so the whole image is readable as its own filesystem.
            # The host filesystem stays unreachable: the chroot restricts the
            # path space, and shared volumes are only exposed via their exact
            # fs_writable directory.
            fs_readable = list(fs_readable) + ["/"]
        net_allow: list[str] = []
        if self._allow_internet_access and self._enable_network:
            net_allow = [
                "files.pythonhosted.org:443",
                "pypi.org:443",
                "registry.npmjs.org:443",
                "proxy.golang.org:443",
                "static.crates.io:443",
                "github.com:443",
                "raw.githubusercontent.com:443",
            ]

        kwargs: dict = {
            "fs_writable": fs_writable,
            "fs_readable": fs_readable,
            "fs_denied": fs_denied,
            "net_allow": net_allow,
            "max_memory": f"{self._memory_mb}M",
            "max_processes": self._max_processes,
            "max_open_files": self._max_open_files,
            "max_cpu": min(100, max(1, self._cpu_percent)),
            "clean_env": True,
            "env": dict(config.env),
            "cwd": config.cwd,
            "uid": 1000,
            "gid": 1000,
        }
        if "mcp-gateway" in " ".join(config.cmd):
            # The SDK starts the MCP gateway inside the sandbox; it must be
            # allowed to bind its HTTP port.
            kwargs["net_allow_bind"] = ["50005"]
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: chroot into the extracted image and expose
            # the sandbox directory as /home/user inside it.
            # fs_mount only takes effect at runtime, so the mount point must
            # already exist inside the rootfs for chdir(/home/user) to work.
            home = Path(self._image_rootfs) / "home" / "user"
            home.mkdir(parents=True, exist_ok=True)
            # Volume mount targets must exist inside the rootfs too.
            for virtual in self._fs_mounts:
                home.joinpath(virtual.removeprefix("/home/user/")).mkdir(
                    parents=True, exist_ok=True
                )
            kwargs["chroot"] = str(self._image_rootfs)
            mount_map = {"/home/user": self._workspace_dir}
            mount_map.update(self._fs_mounts)
            kwargs["fs_mount"] = mount_map
            kwargs["cwd"] = "/home/user"
        elif self._fs_mounts:
            # Without a chroot (pure Sandlock), virtual mount paths cannot be
            # materialized; volume mounts live inside the sandbox directory as
            # symlinks created by the control plane.
            pass
        if sandlock is None:
            # Non-Linux / missing native library: return a plain object so the
            # policy mapping stays unit-testable without executing anything.
            from types import SimpleNamespace

            return SimpleNamespace(**kwargs)
        return SandlockSandbox(**kwargs)

    async def start(self, config: ExecConfig) -> SandlockRunningProcess:
        if sandlock is None:
            raise unimplemented("Sandlock is not available on this platform")
        if config.pty:
            resolved = [
                "/usr/local/bin/python3",
                "-c",
                PTY_BRIDGE_SCRIPT,
                json.dumps(self.resolve_cmd(config.cmd)),
                str(config.rows),
                str(config.cols),
            ]
        else:
            resolved = self.resolve_cmd(config.cmd)

        def _spawn():
            sb = self._build_sandbox(config)
            return sb.popen(
                resolved,
                stdin=StdioMode.PIPED,
                stdout=StdioMode.PIPED,
                stderr=StdioMode.PIPED,
            )

        proc = await asyncio.to_thread(_spawn)
        queue: asyncio.Queue = asyncio.Queue()
        stdin_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_running_loop()
        running = SandlockRunningProcess(
            proc=proc,
            queue=queue,
            loop=loop,
            stdin_queue=stdin_queue,
            pty_mode=config.pty,
        )

        def _pump(stream, kind: str) -> None:
            if config.pty and kind == "stdout":
                kind = "pty"
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

        t1 = threading.Thread(
            target=_pump, args=(proc.stdout, "stdout"), daemon=True
        )
        t2 = threading.Thread(
            target=_pump, args=(proc.stderr, "stderr"), daemon=True
        )
        t1.start()
        t2.start()

        return running
