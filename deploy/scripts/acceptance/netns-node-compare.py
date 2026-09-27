"""Per-node comparison: SDK command RTT, MCP /mcp RTT, wildcard DNS.

One measurement type per round, a handful of boxes at a time (the scheduler
balances, so four creates land ~2 per node), then everything is killed so no
round inherits another's capacity.
"""

import os
import time

import httpx

from e2b import Sandbox

API = os.environ["E2B_API_URL"]
KEY = os.environ["E2B_INTERNAL_API_KEY"]
SANDBOX_URL = os.environ["E2B_SANDBOX_URL"].rstrip("/")

DNS_PROBE = """import socket
try:
    infos = socket.getaddrinfo("api.wild.test", 80, proto=socket.IPPROTO_TCP)
    print("DNS ok:", sorted({i[4][0] for i in infos}))
except Exception as exc:
    print("DNS FAILED:", type(exc).__name__, exc)
"""

SERVER = '''from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return "echo:" + text

server.run(transport="stdio")
'''


def node_of(sb) -> str:
    return httpx.get(f"{API}/internal/routes/{sb.sandbox_id}",
                     headers={"X-Internal-Key": KEY}).json()["nodeID"]


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def spread(count: int, **kwargs) -> dict[str, list]:
    """Create ``count`` boxes and return them grouped by node."""
    created = []
    try:
        for _ in range(count):
            created.append(Sandbox.create(**kwargs))
        grouped: dict[str, list] = {}
        for sb in created:
            grouped.setdefault(node_of(sb), []).append(sb)
        return grouped, created
    except BaseException:
        for sb in created:
            try:
                sb.kill()
            except Exception:  # noqa: BLE001
                pass
        raise


def kill_all(boxes):
    for sb in boxes:
        try:
            sb.kill()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(1.0)


def mcp_call(sb):
    token = sb.get_mcp_token()
    t0 = time.perf_counter()
    r = httpx.post(f"{SANDBOX_URL}/mcp",
                   headers={"E2b-Sandbox-Id": sb.sandbox_id,
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "p", "version": "0"}}},
                   timeout=15.0)
    return r.status_code, (time.perf_counter() - t0) * 1000.0


# --- round 1: command RTT -------------------------------------------------
grouped, boxes = spread(4)
try:
    for node, sbs in sorted(grouped.items()):
        rtts = []
        for _ in range(20):
            for sb in sbs:
                t0 = time.perf_counter()
                sb.commands.run("true")
                rtts.append((time.perf_counter() - t0) * 1000.0)
        print(f"{node}  command RTT  n={len(rtts):3d} p50={pct(rtts, 0.5):6.1f}ms "
              f"p95={pct(rtts, 0.95):6.1f}ms")
finally:
    kill_all(boxes)

# --- round 2: wildcard DNS ------------------------------------------------
grouped, boxes = spread(4, network={"allow_out": ["*.wild.test:80"]})
try:
    for node, sbs in sorted(grouped.items()):
        sb = sbs[0]
        sb.files.write("dns.py", DNS_PROBE)
        print(f"{node}  wildcard DNS {(sb.commands.run('python3 dns.py').stdout or '').strip()}")
finally:
    kill_all(boxes)

# --- round 3: MCP /mcp ----------------------------------------------------
grouped, boxes = spread(4, mcp={"name": "echo", "command": "python3", "args": ["-c", SERVER]})
try:
    for node, sbs in sorted(grouped.items()):
        for sb in sbs:
            codes, lat = [], []
            for _ in range(8):
                code, ms = mcp_call(sb)
                codes.append(code)
                if code < 500:
                    lat.append(ms)
                time.sleep(0.3)
            verdict = (f"serves p50={pct(lat, 0.5):6.1f}ms p95={pct(lat, 0.95):6.1f}ms"
                       if lat else "DEAD")
            print(f"{node}  mcp /mcp     {verdict} (codes {sorted(set(codes))}, {len(lat)}/8)")
finally:
    kill_all(boxes)
