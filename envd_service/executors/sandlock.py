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
import shutil
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
        network: dict | None = None,
        egress_lib_dir: str | Path | None = None,
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
        self._network = dict(network) if network else None
        self._egress_lib_dir = Path(egress_lib_dir) if egress_lib_dir else None
        self._egress_resolved: tuple[str, int] | None = None
        self._extra_fs_writable = list(extra_fs_writable or [])
        self._fs_mounts = dict(fs_mounts or {})

    def update_network(self, network: dict | None) -> None:
        """Replace the network policy; the next command uses it."""
        self._network = dict(network) if network else None
        self._egress_resolved = None

    def _egress_endpoint(self) -> tuple[str, int] | None:
        """Resolve the egress proxy address to (ip, port); hostnames are
        resolved here (the supervisor has DNS) so the LD_PRELOAD library only
        ever dials a literal IP."""
        proxy = (self._network or {}).get("egressProxy")
        if not proxy or not isinstance(proxy, dict):
            return None
        if self._egress_resolved is not None:
            return self._egress_resolved
        import socket as py_socket

        address = str(proxy.get("address", ""))
        host, _, port_s = address.rpartition(":")
        if not host or not port_s.isdigit():
            raise RuntimeError(f"invalid egress proxy address: {address!r}")
        port = int(port_s)
        if not 1 <= port <= 65535:
            raise RuntimeError(f"invalid egress proxy port: {port}")
        infos = py_socket.getaddrinfo(host, port, type=py_socket.SOCK_STREAM)
        ip = next(
            (info[4][0] for info in infos if info[0] == py_socket.AF_INET),
            None,
        )
        if ip is None:
            raise RuntimeError(
                f"egress proxy {address!r} does not resolve to an IPv4 address"
            )
        self._egress_resolved = (ip, port)
        return self._egress_resolved

    def _egress_library(self) -> Path:
        """Return the LD_PRELOAD egress proxy library.

        Prefers the platform-matched library baked into the worker image
        (``/opt/egress/libegress_proxy.so``); otherwise builds it once from
        source into ``egress_lib_dir`` (dev/test fallback, requires cc).
        """
        if self._egress_lib_dir is None:
            raise RuntimeError("egress proxy requires egress_lib_dir")
        lib = self._egress_lib_dir / "libegress_proxy.so"
        if lib.is_file():
            return lib
        baked = Path("/opt/egress/libegress_proxy.so")
        if baked.is_file():
            self._egress_lib_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(baked, lib)
            return lib
        import shutil as _shutil
        import subprocess as _subprocess

        src = (
            Path(__file__).resolve().parent.parent.parent
            / "envd_service"
            / "egress"
            / "libegress_proxy.c"
        )
        if not src.is_file():
            raise RuntimeError(f"egress proxy library source missing: {src}")
        if _shutil.which("cc") is None:
            raise RuntimeError(
                "egress proxy requires a C compiler (cc) to build "
                "libegress_proxy.so"
            )
        self._egress_lib_dir.mkdir(parents=True, exist_ok=True)
        result = _subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-O2",
                "-o",
                str(lib),
                str(src),
                "-ldl",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not lib.is_file():
            raise RuntimeError(
                f"failed to build libegress_proxy.so: {result.stderr.strip()}"
            )
        return lib

    @staticmethod
    def resolve_cmd(cmd: list[str]) -> list[str]:
        """Translate ``/bin/bash`` to ``/bin/sh`` when bash is unavailable
        (slim base images do not ship bash; the official SDK always sends
        ``cmd=/bin/bash``)."""
        if cmd and cmd[0] == "/bin/bash":
            return ["/bin/sh"] + cmd[1:]
        return cmd

    def _build_sandbox(self, config: ExecConfig):
        egress_endpoint = None
        egress_lib: Path | None = None
        if self._network and self._network.get("egressProxy"):
            egress_endpoint = self._egress_endpoint()
            egress_lib = self._egress_library()
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
        net_deny: list[str] = []
        http_allow: list[str] = []
        if egress_endpoint is not None:
            # Egress-proxy mode: the only reachable endpoint is the user's
            # SOCKS5 proxy; allowOut/denyOut filtering happens inside the
            # LD_PRELOAD library, and rules (header transforms) need the
            # proxy layer (rejected at the API).
            ip, port = egress_endpoint
            net_allow = [f"{ip}:{port}"]
            if egress_lib is not None:
                fs_readable = list(fs_readable) + [str(egress_lib.parent)]
        elif self._network:
            from gateway_common.network import sandlock_network_policy

            policy = sandlock_network_policy(
                self._network,
                allow_internet_access=self._allow_internet_access,
                enable_network=self._enable_network,
            )
            net_allow = policy["net_allow"]
            net_deny = policy["net_deny"]
            http_allow = policy["http_allow"]
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

        kwargs: dict = {
            "fs_writable": fs_writable,
            "fs_readable": fs_readable,
            "fs_denied": fs_denied,
            "net_allow": net_allow,
            "net_deny": net_deny,
            "http_allow": http_allow,
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
                    kwargs["http_inject_ca"] = [str(ca_dst)]
                    env = dict(kwargs.get("env") or {})
                    ca_inside = (
                        Path("/home/user/.e2b-ca/ca-certificates.crt")
                        if kwargs.get("chroot")
                        else ca_dst
                    )
                    env["SSL_CERT_FILE"] = str(ca_inside)
                    env["CURL_CA_BUNDLE"] = str(ca_inside)
                    kwargs["env"] = env
        if egress_endpoint is not None and egress_lib is not None:
            env = dict(kwargs.get("env") or {})
            ip, port = egress_endpoint
            if kwargs.get("chroot"):
                # Inside the chroot the workspace is /home/user; copy the
                # library there so the sandboxed loader can reach it.
                ws_lib = Path(self._workspace_dir) / ".egress"
                ws_lib.mkdir(parents=True, exist_ok=True)
                ws_lib = ws_lib / "libegress_proxy.so"
                shutil.copy2(egress_lib, ws_lib)
                env["LD_PRELOAD"] = "/home/user/.egress/libegress_proxy.so"
            else:
                env["LD_PRELOAD"] = str(egress_lib)
            env["EGRESS_PROXY"] = f"{ip}:{port}"
            proxy = self._network["egressProxy"]
            if proxy.get("username"):
                env["EGRESS_PROXY_USER"] = str(proxy["username"])
            if proxy.get("password"):
                env["EGRESS_PROXY_PASS"] = str(proxy["password"])
            if self._network.get("allowOut"):
                env["EGRESS_ALLOW"] = json.dumps(self._network["allowOut"])
            if self._network.get("denyOut"):
                env["EGRESS_DENY"] = json.dumps(self._network["denyOut"])
            kwargs["env"] = env
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
