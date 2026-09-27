"""Split an MCP call into forward path / server work / return path.

The stdio server (our own test code) stamps wall-clock time when the tool
handler is entered and left; the client stamps its send/recv on the worker
side. Same host clock, so the deltas are directly comparable.
"""

import asyncio
import os
import subprocess
import time

import httpx

from e2b import Sandbox

API = os.environ["E2B_API_URL"]
KEY = os.environ["E2B_INTERNAL_API_KEY"]

SERVER = '''import time

from mcp.server.mcpserver import MCPServer

LOG = "t.log"


def log(msg):
    with open(LOG, "a") as fh:
        fh.write(f"{time.time():.6f} {msg}\\n")


server = MCPServer("echo", version="1.0.0")


@server.tool()
async def echo(text: str) -> str:
    log("enter")
    time.sleep(0.0)
    log("exit")
    return "echo:" + text


log("boot")
server.run(transport="stdio")
'''

FIND_PORT = r'''
import sys
import httpx
box, token = sys.argv[1], sys.argv[2]
cands = set()
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    with open(path) as fh:
        next(fh)
        for line in fh:
            c = line.split()
            if c[3] == "0A":
                p = int(c[1].rsplit(":", 1)[1], 16)
                if 50000 <= p <= 65535:
                    cands.add(p)
body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "p", "version": "0"}}}
for p in sorted(cands):
    try:
        r = httpx.post(f"http://127.0.0.1:{p}/mcp",
                       headers={"Authorization": f"Bearer {token}"}, json=body, timeout=10)
    except Exception:
        continue
    if r.status_code == 200:
        print(p)
        break
'''

CLIENT = r'''
import asyncio, json, sys, time

import httpx


async def main():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    port, token = int(sys.argv[1]), sys.argv[2]
    marks = []
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                 timeout=30.0) as http:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp",
                                          http_client=http) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                for _ in range(4):
                    send = time.time()
                    res = await session.call_tool("echo", {"text": "x"})
                    recv = time.time()
                    marks.append((send, recv, res.content[0].text))
                    await asyncio.sleep(0.5)
    for send, recv, text in marks:
        print(f"  client_send={send:.6f} client_recv={recv:.6f} total={(recv - send) * 1000:.1f}ms {text}")


asyncio.run(main())
'''


def place_both():
    """Create a small batch and return {node: sandbox} (extras killed)."""
    for attempt in range(20):
        batch = []
        try:
            for _ in range(4):
                batch.append(Sandbox.create(
                    mcp={"name": "echo", "command": "python3", "args": ["-c", SERVER]}))
        except Exception as exc:  # noqa: BLE001
            print(f"  create retry {attempt}: {type(exc).__name__}")
            for sb in batch:
                try:
                    sb.kill()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(10)
            continue
        by_node = {}
        for sb in batch:
            node = httpx.get(f"{API}/internal/routes/{sb.sandbox_id}",
                             headers={"X-Internal-Key": KEY}).json()["nodeID"]
            by_node.setdefault(node, []).append(sb)
        if {"worker-1", "worker-2"} <= set(by_node):
            picked = {"worker-1": by_node["worker-1"][0], "worker-2": by_node["worker-2"][0]}
            for node, boxes in by_node.items():
                for sb in boxes:
                    if sb not in picked.values():
                        try:
                            sb.kill()
                        except Exception:  # noqa: BLE001
                            pass
            return picked
        for sb in batch:
            try:
                sb.kill()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(4)
    return {}


def find_port(want, sb):
    token = sb.get_mcp_token()
    for _ in range(20):
        found = subprocess.run(
            ["docker", "exec", "-i", f"sandlock-{want}-1", "python3", "-", sb.sandbox_id, token],
            input=FIND_PORT, capture_output=True, text=True, timeout=120)
        out = (found.stdout or "").strip().splitlines()
        if out and out[-1].isdigit():
            return int(out[-1])
        time.sleep(2)
    return None


placed = place_both()
for want in ("worker-1", "worker-2"):
    sb = placed.get(want)
    print(f"=== {want} {sb.sandbox_id if sb else 'NOT PLACED'} ===")
    if sb is None:
        continue
    try:
        port = find_port(want, sb)
        if port is None:
            print("  port not found")
            continue
        res = subprocess.run(
            ["docker", "exec", "-i", f"sandlock-{want}-1", "python3", "-", str(port),
             sb.get_mcp_token()], input=CLIENT, capture_output=True, text=True, timeout=300)
        print((res.stdout or "").strip() or res.stderr.strip()[-200:])
        log = str(sb.files.read("t.log"))
        for line in log.splitlines()[-10:]:
            print("   server:", line)
    finally:
        try:
            sb.kill()
        except Exception:  # noqa: BLE001
            pass
