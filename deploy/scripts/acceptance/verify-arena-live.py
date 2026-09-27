"""Post-deploy: the MCP stdio server's ceiling with the pinned arena."""

import time

import httpx

from e2b import Sandbox

TEMPLATE = '''BUF = bytearray(__N__ * 1024 * 1024)
for i in range(0, len(BUF), 4096):
    BUF[i] = 1
from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return "echo:" + text

server.run(transport="stdio")
'''

URL = "http://127.0.0.1:3000/mcp"


def list_tools(sb):
    import asyncio

    import httpx as hx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        headers = {"E2b-Sandbox-Id": sb.sandbox_id,
                   "Authorization": f"Bearer {sb.get_mcp_token()}"}
        async with hx.AsyncClient(headers=headers, timeout=30.0) as http:
            async with streamable_http_client(f"{URL}", http_client=http) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    res = await session.call_tool("echo", {"text": "hi"})
                    return [t.name for t in tools.tools], res.content[0].text

    return asyncio.run(go())


for n in (110, 200, 300, 340):
    sb = Sandbox.create(mcp={"name": "echo", "command": "python3",
                             "args": ["-c", TEMPLATE.replace("__N__", str(n))]})
    try:
        deadline = time.time() + 30
        out = None
        while time.time() < deadline:
            try:
                out = list_tools(sb)
                break
            except Exception as exc:  # noqa: BLE001
                out = f"{type(exc).__name__}"
                time.sleep(1.5)
        print(f"  server {n:>4d} MiB -> {out}")
    finally:
        sb.kill()

plain = Sandbox.create()
try:
    res = plain.commands.run(
        "python3 -c \"import time;b=bytearray(400*1024*1024);"
        "[b.__setitem__(i,1) for i in range(0,len(b),65536)];print('ok 400MiB')\"",
        timeout=60,
    )
    print("  plain box 400 MiB ->", (res.stdout or "").strip())
finally:
    plain.kill()
