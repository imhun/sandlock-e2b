"""Phase B1/C egress proxy: SOCKS5 tunneling on the sandlock on-behalf path.

The sandlock fork tunnels every outbound TCP connect through the user's
SOCKS5 proxy *after* allow/deny filtering — the LD_PRELOAD library is retired
(R14). The proxy endpoint is dialed by the supervisor and is never added to
the sandbox's allowlist, so the sandbox cannot reach it directly.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import tempfile
from pathlib import Path

import pytest

from tests.security.conftest import route_b_sandbox, sandbox_tmpdir

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor


class Socks5Server:
    """Minimal SOCKS5 proxy recording (atyp, addr, port) per CONNECT."""

    def __init__(self) -> None:
        self.log: list[tuple[int, str, int]] = []
        self.port: int | None = None
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        try:
            head = await reader.readexactly(2)
            if head[0] != 0x05:
                return
            await reader.readexactly(head[1])
            writer.write(bytes([0x05, 0x00]))
            await writer.drain()
            req = await reader.readexactly(4)
            if req[0] != 0x05 or req[1] != 0x01:
                return
            atyp = req[3]
            if atyp == 0x01:
                addr = socket.inet_ntoa(await reader.readexactly(4))
            elif atyp == 0x03:
                n = (await reader.readexactly(1))[0]
                addr = (await reader.readexactly(n)).decode()
            else:
                return
            port = struct.unpack(">H", await reader.readexactly(2))[0]
            self.log.append((atyp, addr, port))
            upstream = await asyncio.open_connection(addr, port)
            writer.write(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
            await asyncio.gather(
                self._pump(reader, upstream[1]),
                self._pump(upstream[0], writer),
            )
        except Exception:
            pass
        finally:
            writer.close()

    @staticmethod
    async def _pump(src, dst) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except Exception:
            pass


class OriginServer:
    """Local HTTP-ish origin that records the first request and replies 200."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.port: int | None = None
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        data = await reader.read(65536)
        self.requests.append(data)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()


def _executor(ws, network, secrets_dir: Path | None = None) -> SandlockExecutor:
    """A pure sandbox with this network policy, in the deployment's shape.

    Built through `route_b_sandbox` rather than hand-built (N15): the pure
    shape is mediated now, and a hand-built one on a root worker is the shape
    the fork refuses (SL-1 -- the mediation would run as the host root while
    the sandbox has its own uid).
    """
    executor, _ = route_b_sandbox(
        None,
        None,
        workspace=ws,
        enable_network=True,
        network=network,
        secrets_dir=secrets_dir,
    )
    return executor


async def _run(executor, ws, code) -> tuple[int, bytes, bytes]:
    probe = ExecConfig(
        cmd=["/usr/local/bin/python3", "-c", code],
        env={},
        cwd=ws,
        stdin_enabled=False,
    )
    proc = await executor.start(probe)
    out = b""
    err = b""
    async for kind, data in proc.output():
        if kind in ("stdout", "pty"):
            out += data
        elif kind == "stderr":
            err += data
    return await proc.exit_code(), out, err


@pytest.mark.usefixtures("require_sandlock")
async def test_egress_proxy_tunnels_tcp_after_filter():
    """Allowed TCP exits through the SOCKS5 proxy (ATYP=IPv4 for a literal
    destination); the proxy endpoint is not in net_allow."""
    proxy = Socks5Server()
    await proxy.start()
    origin = OriginServer()
    await origin.start()
    try:
        ws = str(sandbox_tmpdir())
        executor = _executor(
            ws,
            {
                "egressProxy": {"address": f"127.0.0.1:{proxy.port}"},
                "allowOut": [f"127.0.0.1:{origin.port}"],
            },
        )
        code = (
            "import urllib.request; "
            f"print(urllib.request.urlopen('http://127.0.0.1:{origin.port}/', "
            "timeout=10).status)"
        )
        exit_code, out, err = await _run(executor, ws, code)
        assert exit_code == 0, err.decode()
        assert out.decode().strip() == "200"
        # The proxy saw a literal-IP CONNECT and the origin got the request.
        assert (1, "127.0.0.1", origin.port) in proxy.log, proxy.log
        assert origin.requests, "origin must receive the tunneled request"

        kwargs = executor._build_sandbox(
            ExecConfig(cmd=["true"], env={}, cwd=ws, stdin_enabled=False)
        )
        # The proxy endpoint is NOT in net_allow (it is dialed by the
        # supervisor); filtering stays on the real destination.
        assert kwargs.net_allow == [f"127.0.0.1:{origin.port}"]
        assert kwargs.egress_proxy == {
            "address": f"127.0.0.1:{proxy.port}"
        }
        assert "EGRESS_PROXY" not in kwargs.env
    finally:
        await proxy.stop()
        await origin.stop()


@pytest.mark.usefixtures("require_sandlock")
async def test_egress_proxy_deny_out_blocks():
    """A destination outside allowOut never reaches the proxy (fail closed)."""
    proxy = Socks5Server()
    await proxy.start()
    origin = OriginServer()
    await origin.start()
    try:
        ws = str(sandbox_tmpdir())
        executor = _executor(
            ws,
            {
                "egressProxy": {"address": f"127.0.0.1:{proxy.port}"},
                # Only the origin is allowed; the child dials the proxy port.
                "allowOut": [f"127.0.0.1:{origin.port}"],
            },
        )
        code = (
            "import urllib.request; "
            f"urllib.request.urlopen('http://127.0.0.1:{proxy.port}/', timeout=10)"
        )
        exit_code, _out, _err = await _run(executor, ws, code)
        # Denied: the child cannot even reach the proxy endpoint directly.
        assert exit_code != 0
        assert not origin.requests
    finally:
        await proxy.stop()
        await origin.stop()


@pytest.mark.usefixtures("require_sandlock")
async def test_egress_proxy_wildcard_uses_atyp_domain():
    """``*.example.com`` matches subdomains and the proxy resolves them
    remotely (ATYP=domain); the bare apex is not matched."""
    proxy = Socks5Server()
    await proxy.start()
    origin = OriginServer()
    await origin.start()
    hosts_line = "127.0.0.1 api.egress.test\n"
    try:
        with open("/etc/hosts", "a", encoding="utf-8") as f:
            f.write(hosts_line)
        ws = str(sandbox_tmpdir())
        executor = _executor(
            ws,
            {
                "egressProxy": {"address": f"127.0.0.1:{proxy.port}"},
                "allowOut": ["*.egress.test"],
            },
        )
        subdomain = (
            "import urllib.request; "
            f"print(urllib.request.urlopen('http://api.egress.test:{origin.port}/', "
            "timeout=10).status)"
        )
        exit_code, out, err = await _run(executor, ws, subdomain)
        assert exit_code == 0, err.decode()
        assert out.decode().strip() == "200"
        assert (3, "api.egress.test", origin.port) in proxy.log, proxy.log

        apex = (
            "import urllib.request; "
            f"urllib.request.urlopen('http://egress.test:{origin.port}/', timeout=10)"
        )
        exit_code, _out, _err = await _run(executor, ws, apex)
        assert exit_code != 0, "bare apex must not match *.egress.test"
    finally:
        try:
            with open("/etc/hosts", "r", encoding="utf-8") as f:
                lines = [l for l in f if l != hosts_line]
            with open("/etc/hosts", "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass
        await proxy.stop()
        await origin.stop()
