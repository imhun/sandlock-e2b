"""Sandlock executor: Landlock + seccomp-bpf + seccomp user notification.

Requires Linux with Landlock ABI >= 6 and ``sandlock==0.9.0-beta``. The Sandbox
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
                        # Keep only a tail that could be the start of a
                        # FRAME_START marker; plain data flushes in full
                        # (keeping len(FRAME_START)-1 unconditionally used to
                        # swallow short inputs byte by byte).
                        keep = 0
                        for k in range(len(FRAME_START) - 1, 0, -1):
                            if FRAME_START[:k] == pending[-k:]:
                                keep = k
                                break
                        # NB: pending[:-0] is empty in Python, so handle
                        # keep == 0 (no frame prefix) explicitly.
                        if keep:
                            flush = pending[:-keep] if len(pending) > keep else b""
                        else:
                            flush = pending
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
    from sandlock import (
        BranchAction,
        Sandbox as SandlockSandbox,
        SandboxInstance,
        StdioMode,
    )
except Exception:  # pragma: no cover - macOS / missing package
    sandlock = None  # type: ignore[assignment]
    BranchAction = None  # type: ignore[assignment]
    SandboxInstance = None  # type: ignore[assignment]
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
                        self._proc.stdin.flush()
                        logger.debug(
                            "sandlock stdin wrote %d bytes (fd=%s)",
                            len(data),
                            getattr(self._proc.stdin, "fileno", lambda: None)(),
                        )
                    except Exception as e:  # noqa: BLE001 - keep the loop alive
                        logger.warning("sandlock stdin write failed: %r", e)
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
            logger.warning("sandlock stdin queue full; dropping %d bytes", len(data))

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
    """Holds one lazily-created ``sandlock.SandboxInstance`` per executor.

    M4 D1/D2 lifecycle shell: the instance is created on first
    ``_ensure_instance()`` with a stable ``sandbox_id``-derived name, rebuilt
    exactly once after a closed/dead launch, and released by ``close()``.
    ``start()`` still builds a fresh one-shot ``Sandbox`` per command until a
    later step rewires it onto the instance's ``exec()``; on non-Linux hosts
    sandlock is unavailable and the instance stays ``None`` (D11).
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
        self._instance = None

    def update_network(self, network: dict | None) -> None:
        """Replace the network policy; the next command uses it."""
        self._network = dict(network) if network else None

    @property
    def instance_name(self) -> str:
        """Stable instance identity: ``sandbox_id`` (or its hash) when given,
        otherwise the workspace directory basename."""
        return self._instance_name_for()

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

    def _ensure_instance(self):
        """Lazily create the one long-lived exec instance (M4 D1).

        The instance policy is a placeholder: the current ``_build_sandbox``
        shape for an empty command config (``_build_instance_policy()`` lands
        with Task 2). A launch reporting a closed/dead session is retried
        exactly once; any second failure propagates unchanged. Without the
        native library (non-Linux) this is a silent no-op returning ``None``
        (D11).
        """
        if self._instance is None and SandboxInstance is not None:
            # Placeholder policy: current _build_sandbox shape for an empty
            # command config; Task 2 replaces it with _build_instance_policy().
            policy = self._build_sandbox(
                ExecConfig(
                    cmd=[],
                    env={},
                    cwd=self._workspace_dir,
                    stdin_enabled=False,
                )
            )
            try:
                self._instance = SandboxInstance(
                    policy, name=self._instance_name_for()
                )
            except RuntimeError as exc:
                message = str(exc)
                if "closed" not in message and "dead" not in message:
                    raise
                # The prior session was closed (shutdown/idle reclaim) or died
                # (machinery failure): rebuild exactly once, and let a second
                # failure bubble up unchanged.
                self._instance = None
                self._instance = SandboxInstance(
                    policy, name=self._instance_name_for()
                )
        return self._instance

    def close(self) -> None:
        """Close the exec instance and release the handle (idempotent)."""
        if self._instance is not None:
            self._instance.close()
            self._instance = None

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

    def _mediation_run_as(self) -> str:
        """F6.1 C 档 fail-closed 与现网形态的桥：
        root worker + RunAs(非 0) + chroot 路径中介 ⇒ 默认 caller 会在建箱前被拒。
        E2B 部署 route-B（supervise 进程 euid==沙箱 uid）前，显式降级档恢复 F9 前语义
        （fork 每次 launch WARN + stats.mediation_downgrades 计数）；非 root 无降级。"""
        if os.geteuid() == 0 and self._base_image and self._image_rootfs is not None:
            return "supervisor"
        return "caller"

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

    def _build_sandbox(self, config: ExecConfig):
        fs_writable = [self._workspace_dir]
        fs_writable.extend(self._extra_fs_writable)
        fs_readable = ["/usr", "/lib", "/bin", "/opt"]
        # Shared-path denials (/dev/shm, /dev/mqueue are common to every
        # sandbox on the worker) are only needed where the sandbox can actually
        # see them: with an image rootfs the whole tree is readable, so they
        # have to be carved out explicitly. Without a chroot, Landlock is an
        # allow-list and those paths are already unreachable (not in
        # fs_readable), so the rules add nothing -- and issuing them would cost
        # the sandbox its own file ownership: sandlock enforces denials through
        # its on-behalf open path, so every file the sandbox creates is then
        # attributed to the supervisor (host uid 0) instead of the sandbox host
        # uid, which silently voids both ``chmod`` inside the sandbox and the
        # per-uid isolation of shared volumes.
        fs_denied: list[str] = []
        if config.pty:
            # The in-sandbox PTY bridge needs the pty device nodes.
            fs_writable += ["/dev/ptmx", "/dev/pts"]
            fs_readable += ["/dev/ptmx", "/dev/pts"]
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: "/" resolves inside the chroot (the image
            # rootfs), so the whole image is readable as its own filesystem.
            # The host filesystem stays unreachable: the chroot restricts the
            # path space, and shared volumes are only exposed via their exact
            # fs_writable directory. The container /dev is mounted into the
            # chroot for the PTY bridge, so the shared tmpfs/queue paths need
            # the explicit denials here (accepting the supervisor-attributed
            # writes that come with that path -- see the note above, and
            # docs/HANDOFF.md T1 for the open fork question).
            fs_readable = list(fs_readable) + ["/"]
            fs_denied = ["/proc/kcore", "/sys", "/dev/shm", "/dev/mqueue"]
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
            "mediation_run_as": self._mediation_run_as(),
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
            # the sandbox directory as /workspace (official SDK default cwd)
            # and /home/user (legacy home) inside it. fs_mount only takes
            # effect at runtime, so the mount points must already exist
            # inside the rootfs for chdir() to work.
            for mount_point in ("workspace", "home/user"):
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
                "/workspace": self._workspace_dir,
                "/home/user": self._workspace_dir,
            }
            mount_map.update(self._fs_mounts)
            # The extracted image /dev is empty; expose the container's /dev
            # so the sandbox gets /dev/ptmx + devpts (PTY bridge), /dev/null,
            # /dev/urandom etc. fs_mount treats the host target as a
            # directory root, so the whole /dev tree must be mounted (a
            # single-file mount would resolve with ENOTDIR).
            mount_map["/dev"] = "/dev"
            kwargs["fs_mount"] = mount_map
            cwd = (config.cwd or "").strip()
            if not cwd or cwd.startswith(str(self._workspace_dir)):
                kwargs["cwd"] = "/workspace"
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
