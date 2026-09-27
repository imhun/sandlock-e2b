"""In a 512MB box: wait for the gateway, then find the largest stdio server."""

import asyncio
import os
import time

import httpx

from e2b import Sandbox

TEMPLATE = '''BUF = bytearray(__N__ * 1024 * 1024)
for i in range(0, len(BUF), 4096): BUF[i] = 1
from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return "echo:" + text

server.run(transport="stdio")
'''

URL = os.environ["E2B_SANDBOX_URL"].rstrip("/")


async def mcp_call(url: str, headers: dict, text: str):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                res = await session.call_tool("echo", {"text": text})
                return names, res.content[0].text if res.content else None


def probe(n: int) -> None:
    sb = Sandbox.create(mcp={"name": "echo", "command": "python3", "args": ["-c", TEMPLATE.replace("__N__", str(n))]})
    try:
        headers = {"E2b-Sandbox-Id": sb.sandbox_id, "Authorization": f"Bearer {sb.get_mcp_token()}"}
        deadline = time.time() + 25
        last = None
        while time.time() < deadline:
            try:
                names, echo = asyncio.run(mcp_call(f"{URL}/mcp", headers, f"n={n}"))
                print(f"server {n:>3} MiB -> tools={names} echo={echo!r}")
                return
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {str(exc)[:70]}"
                time.sleep(1.5)
        print(f"server {n:>3} MiB -> NEVER READY ({last})")
    finally:
        try:
            sb.kill()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    for n in (110, 120):
        probe(n)
