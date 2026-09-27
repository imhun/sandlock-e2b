"""FUP-23 paired send/recv probe: several marker execs on one pure instance.

Each command's argv carries a distinct marker that still matches the fork-side
token filter, so one init report line can be attributed to exactly one exec.
Because the fork-side diagnostics accumulate in a process-local string that the
*next* matching child ships back through its own stdout end, running several
execs lets one report carry the pre-fork / post-fork / in-child snapshots of an
earlier one.  N = spare descriptors this client process holds before starting,
the variable that decides red vs green.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, "/workspace")

N = int(sys.argv[1]) if len(sys.argv) > 1 else 0
EXECS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
held = [os.open("/dev/null", os.O_RDONLY) for _ in range(N)]

from e2b import Sandbox  # noqa: E402
from tests.conftest import PROJECT_ROOT, _start_multinode  # noqa: E402
from tests.security.conftest import sandlock_ready  # noqa: E402

MCP_SERVER = r"""
import asyncio
_BUF = bytearray(8 * 1024 * 1024)
for _i in range(0, len(_BUF), 4096):
    _BUF[_i] = 1
from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return f"echo:{text}"

server.run(transport="stdio")
"""


def run_one(sandbox, marker: str) -> dict:
    code = f"/usr/local/bin/python3 -c \"print('{marker}', flush=True)\""
    t0 = time.monotonic()
    try:
        r = sandbox.commands.run(code, timeout=60)
        return {"exit": r.exit_code, "out": r.stdout, "err": r.stderr,
                "dur": round(time.monotonic() - t0, 2)}
    except Exception as exc:  # noqa: BLE001
        return {
            "exit": getattr(exc, "exit_code", None),
            "out": getattr(exc, "stdout", ""),
            "err": getattr(exc, "stderr", ""),
            "exc": type(exc).__name__,
            "dur": round(time.monotonic() - t0, 2),
        }


def main() -> int:
    if not sandlock_ready():
        print("SKIP: sandlock not ready")
        return 2
    print(f"holding {N} extra fds; highest={max(held) if held else -1}", flush=True)
    harness = _start_multinode(PROJECT_ROOT / "tmp" / "f23-multi-probe", 2,
                               buildkit_addr=None)
    sandbox = None
    try:
        opts = {
            "api_url": harness["api_url"],
            "sandbox_url": harness["sandbox_url"],
            "api_key": "local-key",
        }
        sandbox = Sandbox.create(
            mcp={"name": "echo", "command": "python3", "args": ["-c", MCP_SERVER]},
            **opts,
        )
        for i in range(EXECS):
            res = run_one(sandbox, f"post-gateway-ok{i}")
            print("MARKER", i, json.dumps(res, ensure_ascii=False), flush=True)
    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
            except Exception:  # noqa: BLE001
                pass
        harness["_stop"]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
