"""Phase B1 egress proxy: the LD_PRELOAD SOCKS5 tunnel on Sandlock."""

from __future__ import annotations

import asyncio
import socket
import struct
import tempfile
from pathlib import Path

import pytest

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


def _executor(ws, lib_dir, network) -> SandlockExecutor:
    return SandlockExecutor(
        workspace_dir=ws,
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=True,
        network=network,
        egress_lib_dir=lib_dir,
    )


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
async def test_egress_proxy_tunnels_with_remote_dns():
    """Sandbox traffic exits through the user's SOCKS5 proxy with ATYP=domain
    (remote DNS), while net_allow only permits the proxy endpoint."""
    proxy = Socks5Server()
    await proxy.start()
    try:
        ws = tempfile.mkdtemp()
        lib_dir = Path(tempfile.mkdtemp())
        executor = _executor(
            ws,
            lib_dir,
            {
                "egressProxy": {"address": f"127.0.0.1:{proxy.port}"},
                "allowOut": ["example.com"],
            },
        )
        code = (
            "import urllib.request; "
            "print(urllib.request.urlopen('https://example.com', "
            "timeout=10).status)"
        )
        exit_code, out, err = await _run(executor, ws, code)
        assert exit_code == 0, err.decode()
        assert out.decode().strip() == "200"
        # Remote DNS: the proxy saw the hostname, not a synthetic IP.
        assert (3, "example.com", 443) in proxy.log, proxy.log
        # net_allow only permits the proxy endpoint.
        sandbox_kwargs = executor._build_sandbox(
            ExecConfig(
                cmd=["true"],
                env={},
                cwd=ws,
                stdin_enabled=False,
            )
        )
        assert sandbox_kwargs.net_allow == [f"127.0.0.1:{proxy.port}"]
        assert "EGRESS_PROXY" in sandbox_kwargs.env
        assert sandbox_kwargs.env["EGRESS_PROXY"] == (
            f"127.0.0.1:{proxy.port}"
        )
    finally:
        await proxy.stop()


@pytest.mark.usefixtures("require_sandlock")
async def test_egress_proxy_deny_out_blocks():
    """A destination outside allowOut never reaches the proxy (fail closed)."""
    proxy = Socks5Server()
    await proxy.start()
    try:
        ws = tempfile.mkdtemp()
        lib_dir = Path(tempfile.mkdtemp())
        executor = _executor(
            ws,
            lib_dir,
            {
                "egressProxy": {"address": f"127.0.0.1:{proxy.port}"},
                "allowOut": ["example.com"],
            },
        )
        code = (
            "import urllib.request; "
            "urllib.request.urlopen('https://example.org', timeout=10)"
        )
        exit_code, _out, _err = await _run(executor, ws, code)
        assert exit_code != 0
        assert all(addr != "example.org" for _a, addr, _p in proxy.log)
    finally:
        await proxy.stop()


@pytest.mark.usefixtures("require_sandlock")
async def test_egress_proxy_wildcard_domain():
    """``*.example.com`` matches subdomains (remote DNS via the proxy) but
    not the bare apex domain."""
    proxy = Socks5Server()
    await proxy.start()
    try:
        ws = tempfile.mkdtemp()
        lib_dir = Path(tempfile.mkdtemp())
        executor = _executor(
            ws,
            lib_dir,
            {
                "egressProxy": {"address": f"127.0.0.1:{proxy.port}"},
                "allowOut": ["*.example.com"],
            },
        )

        subdomain = (
            "import urllib.request; "
            "print(urllib.request.urlopen('https://www.example.com', "
            "timeout=10).status)"
        )
        exit_code, out, err = await _run(executor, ws, subdomain)
        assert exit_code == 0, err.decode()
        assert out.decode().strip() == "200"
        assert (3, "www.example.com", 443) in proxy.log, proxy.log

        apex = (
            "import urllib.request; "
            "urllib.request.urlopen('https://example.com', timeout=10)"
        )
        exit_code, _out, _err = await _run(executor, ws, apex)
        assert exit_code != 0
    finally:
        await proxy.stop()
