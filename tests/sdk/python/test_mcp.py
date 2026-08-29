"""MCP gateway integration: SDK starts mcp-gateway, clients call its tools.

Runs where ``mcp-gateway`` is available (Linux test runner / envd image).
On macOS with the local executor the gateway binary is absent, so the test
is skipped with the reason recorded.
"""

from __future__ import annotations

import shutil
import time

import pytest


def _gateway_available() -> bool:
    return shutil.which("mcp-gateway") is not None


pytestmark = pytest.mark.skipif(
    not _gateway_available(),
    reason="mcp-gateway not on PATH; run inside the Linux test runner / envd image",
)

# A tiny stdio MCP server (echo tool) executed with python3 inside the sandbox.
ECHO_SERVER = r"""
import asyncio
from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return f"echo:{text}"

server.run(transport="stdio")
"""


def test_mcp_gateway_tools(live_servers):
    from e2b import Sandbox

    sandbox = Sandbox.create(
        mcp={"name": "echo", "command": "python3", "args": ["-c", ECHO_SERVER]}
    )
    try:
        # Non-debug mode builds a cloud domain URL; the local gateway always
        # listens on 127.0.0.1:50005/mcp.
        url = "http://127.0.0.1:50005/mcp"
        token = sandbox.get_mcp_token()
        assert sandbox.get_mcp_url().endswith("/mcp")
        assert token

        # The SDK started mcp-gateway as a background command; wait for the
        # HTTP port to accept connections.
        import socket

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", 50005), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            raise AssertionError("mcp-gateway did not start listening on 50005")

        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from httpx2 import AsyncClient

        async def call():
            async with AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                async with streamable_http_client(url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert [t.name for t in tools.tools] == ["echo"]
                        result = await session.call_tool("echo", {"text": "hello"})
                        return result.content[0].text

        assert asyncio.run(call()) == "echo:hello"
    finally:
        sandbox.kill()
