"""F11 E2B rerun probe: gateway+command boxed quota on the F11 tip wheel.

Runs inside the Docker test runner against a real multinode harness with
pure-sandlock workers (``E2B_BASE_IMAGE=``). A sandbox with an MCP gateway
whose stdio echo server allocates HELD_MB (touched, default 450) must:

* be reachable (``list_tools`` returns exactly ``["echo"]``);
* still run a trivial command after the gateway started (exit 0) -- the F11
  regression guard (pre-F11 every later exec died exit 127 with the
  argv-safety freeze EPERM);
* deny a second concurrent DENIED_MB (default 450) command against the
  1 GiB box, recording the exact SDK signature (expected exit 137 / empty
  stdout / stderr in {"", "Killed\\n"} -- observed values are pinned, never
  guessed);
* accept a CONTROL_MB (default 50) command while the server still holds its
  HELD_MB allocation;
* keep the gateway serving ``["echo"]`` after both command outcomes;
* record ``memoryMB == 1024`` in the public sandbox record.

Evidence (exact exit code / stdout / stderr) is written to the path given by
``F11_EVIDENCE`` (default /workspace/tmp/perf/f11-gateway-evidence.txt) for
pinning in the contract test.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/workspace")

from e2b import Sandbox  # noqa: E402
from e2b.exceptions import SandboxException  # noqa: E402
from tests.conftest import PROJECT_ROOT, _start_multinode  # noqa: E402
from tests.security.conftest import sandlock_ready  # noqa: E402

MB = 1024 * 1024
HELD_MB = int(os.environ.get("F11_HELD_MB", "450"))
DENIED_MB = int(os.environ.get("F11_DENIED_MB", "450"))
CONTROL_MB = int(os.environ.get("F11_CONTROL_MB", "50"))
EXPECTED_MEMORY_MB = int(os.environ.get("F11_EXPECTED_MEMORY_MB", "1024"))
TRIVIAL_MARKER = os.environ.get("F11_TRIVIAL_MARKER", "post-gateway-ok")


# The MCP stdio server: allocate HELD_MB + touch every page at import time,
# then serve the echo tool (same pattern as tests/contract/test_mcp_netns.py
# and the task8 FUP3 probe).
MCP_HELD_SERVER = r"""
import asyncio
_BUF = bytearray(HELD_MB_ * 1024 * 1024)
for _i in range(0, len(_BUF), 4096):
    _BUF[_i] = 1
from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return f"echo:{text}"

server.run(transport="stdio")
""".replace("HELD_MB_", str(HELD_MB))


def alloc_script(mb: int, hold: float = 2.0) -> str:
    return (
        "import time\n"
        f"buf=bytearray({mb}*1024*1024)\n"
        "for i in range(0,len(buf),4096): buf[i]=1\n"
        f"print('got {mb}',flush=True)\n"
        f"time.sleep({hold})\n"
    )


def py_cmd(script: str) -> str:
    """SDK command line running ``script`` under python3."""
    return f"/usr/local/bin/python3 -c \"{script}\""


def run_cmd(sandbox, code: str, timeout: float = 120) -> dict:
    """Run a foreground command; a non-zero exit surfaces as an exception."""
    try:
        result = sandbox.commands.run(code, timeout=timeout)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": getattr(result, "error", None),
            "raised": False,
        }
    except SandboxException as exc:
        return {
            "exit_code": getattr(exc, "exit_code", None),
            "stdout": getattr(exc, "stdout", ""),
            "stderr": getattr(exc, "stderr", ""),
            "error": getattr(exc, "error", None),
            "raised": True,
            "exc_type": type(exc).__name__,
        }


def record(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


async def mcp_tools(url: str, headers: dict[str, str], timeout: float = 20.0):
    """Initialize one MCP session and return the exact tool-name list."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]


def env_header() -> str:
    try:
        main_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        main_head = "unknown"
    try:
        fork_head = subprocess.run(
            ["git", "-C", "/workspace/third_party/sandlock", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        fork_head = "unknown"
    try:
        image_id = subprocess.run(
            ["bash", "-lc", "docker images --format '{{.ID}}' e2b-sandlock-test:latest"],
            capture_output=True,
            text=True,
        ).stdout.strip().splitlines()
        image = image_id[0] if image_id else "unknown"
    except Exception:
        image = "unknown"
    return (
        "ENV-HEADER date=%s main=%s fork=%s image=%s held_mb=%d denied_mb=%d "
        "control_mb=%d expected_memory_mb=%d trivial_marker=%s\n"
        % (
            time.strftime("%Y-%m-%d %H:%M:%S %z"),
            main_head,
            fork_head,
            image,
            HELD_MB,
            DENIED_MB,
            CONTROL_MB,
            EXPECTED_MEMORY_MB,
            TRIVIAL_MARKER,
        )
    )


def main() -> int:
    if not sandlock_ready():
        print("SKIP: sandlock not ready on this host")
        return 2
    evidence = Path(
        os.environ.get(
            "F11_EVIDENCE",
            "/workspace/tmp/perf/f11-gateway-evidence.txt",
        )
    )
    if evidence.exists():
        evidence.unlink()
    harness = _start_multinode(
        PROJECT_ROOT / "tmp" / "f11-gateway-probe", 2, buildkit_addr=None
    )
    sandbox = None
    failures = []
    try:
        opts = {
            "api_url": harness["api_url"],
            "sandbox_url": harness["sandbox_url"],
            "api_key": "local-key",
        }
        t0 = time.monotonic()
        sandbox = Sandbox.create(
            mcp={"name": "echo", "command": "python3", "args": ["-c", MCP_HELD_SERVER]},
            **opts,
        )
        print(f"create+gateway-start took {time.monotonic() - t0:.1f}s", flush=True)
        token = sandbox.get_mcp_token()
        assert token, "no mcp token"
        url = f"{harness['sandbox_url'].rstrip('/')}/mcp"
        sandbox_headers = {"E2b-Sandbox-Id": sandbox.sandbox_id}
        import httpx

        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                resp = httpx.get(
                    url,
                    headers={**sandbox_headers, "Authorization": f"Bearer {token}"},
                    timeout=3,
                )
                if resp.status_code < 500:
                    break
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(0.5)
        else:
            raise AssertionError("mcp-gateway did not start listening")

        tools = asyncio.run(
            asyncio.wait_for(
                mcp_tools(
                    url, {**sandbox_headers, "Authorization": f"Bearer {token}"}
                ),
                timeout=40,
            )
        )
        print("initial tools:", tools, flush=True)
        if tools != ["echo"]:
            failures.append(f"initial tools != ['echo']: {tools!r}")

        # F11 regression guard: a trivial command must run after the gateway
        # (pre-F11 every later exec died exit 127, empty stdout/stderr).
        t_trivial = time.monotonic()
        trivial = run_cmd(
            sandbox, py_cmd(f"print('{TRIVIAL_MARKER}', flush=True)")
        )
        trivial_dur = time.monotonic() - t_trivial
        print(
            "trivial run:",
            json.dumps(
                {
                    "exit_code": trivial["exit_code"],
                    "stdout": trivial["stdout"],
                    "stderr": trivial["stderr"],
                    "error": trivial["error"],
                    "duration_s": round(trivial_dur, 2),
                }
            ),
            flush=True,
        )
        expected_trivial = f"{TRIVIAL_MARKER}\n"
        if (
            trivial["exit_code"] != 0
            or trivial["stdout"] != expected_trivial
            or trivial["stderr"] != ""
        ):
            failures.append(
                "post-gateway trivial command failed: "
                f"exit={trivial['exit_code']} stdout={trivial['stdout']!r} "
                f"stderr={trivial['stderr']!r}"
            )

        # Overcommit probe: gateway ledger + 450 MiB server already inside
        # this box; a concurrent 450 MiB allocation must be denied.
        t1 = time.monotonic()
        denied = run_cmd(
            sandbox, py_cmd(alloc_script(DENIED_MB))
        )
        dur = time.monotonic() - t1
        print(
            "denied run:",
            json.dumps(
                {
                    "exit_code": denied["exit_code"],
                    "stdout": denied["stdout"],
                    "stderr": denied["stderr"],
                    "error": denied["error"],
                    "duration_s": round(dur, 2),
                }
            ),
            flush=True,
        )
        if denied["exit_code"] == 0:
            failures.append("overcommit command unexpectedly succeeded")

        # Gateway must still be alive and serving tools.
        tools_after = asyncio.run(
            asyncio.wait_for(
                mcp_tools(
                    url, {**sandbox_headers, "Authorization": f"Bearer {token}"}
                ),
                timeout=40,
            )
        )
        print("tools after denial:", tools_after, flush=True)
        if tools_after != ["echo"]:
            failures.append("gateway lost after denial")

        # Control: gateway ledger + 450 + 50 stays below the 1024 MiB box
        # quota, so the command must succeed while the server holds 450 MiB.
        t2 = time.monotonic()
        ok = run_cmd(
            sandbox, py_cmd(alloc_script(CONTROL_MB))
        )
        dur2 = time.monotonic() - t2
        print(
            "control run:",
            json.dumps(
                {
                    "exit_code": ok["exit_code"],
                    "stdout": ok["stdout"],
                    "stderr": ok["stderr"],
                    "duration_s": round(dur2, 2),
                }
            ),
            flush=True,
        )
        if ok["exit_code"] != 0 or ok["stdout"] != f"got {CONTROL_MB}\n":
            failures.append(
                f"control command failed: exit={ok['exit_code']} "
                f"stdout={ok['stdout']!r}"
            )

        tools_final = asyncio.run(
            asyncio.wait_for(
                mcp_tools(
                    url, {**sandbox_headers, "Authorization": f"Bearer {token}"}
                ),
                timeout=40,
            )
        )
        print("tools after control:", tools_final, flush=True)
        if tools_final != ["echo"]:
            failures.append("gateway lost after control command")

        # Public API record: the per-sandbox default must be exactly 1024.
        import httpx as hx

        detail = hx.get(
            f"{harness['api_url'].rstrip('/')}/sandboxes/{sandbox.sandbox_id}",
            headers={"X-API-Key": "local-key"},
            timeout=10,
        )
        print("sandbox detail:", detail.status_code, json.dumps(detail.json()))
        assert detail.status_code == 200
        if detail.json().get("memoryMB") != EXPECTED_MEMORY_MB:
            failures.append(
                f"record memoryMB != {EXPECTED_MEMORY_MB}: "
                f"{detail.json().get('memoryMB')}"
            )

        record(
            evidence,
            "F11-GATEWAY-EVIDENCE\n"
            f"held_mb={HELD_MB} denied_mb={DENIED_MB} control_mb={CONTROL_MB}\n"
            f"expected_memory_mb={EXPECTED_MEMORY_MB}\n"
            f"initial_tools={tools}\n"
            f"trivial_exit_code={trivial['exit_code']}\n"
            f"trivial_stdout={trivial['stdout']!r}\n"
            f"trivial_stderr={trivial['stderr']!r}\n"
            f"trivial_raised={trivial['raised']}\n"
            f"overcommit_exit_code={denied['exit_code']}\n"
            f"overcommit_stdout={denied['stdout']!r}\n"
            f"overcommit_stderr={denied['stderr']!r}\n"
            f"overcommit_error={denied['error']!r}\n"
            f"overcommit_raised={denied['raised']}\n"
            f"tools_after_denial={tools_after}\n"
            f"control_exit_code={ok['exit_code']}\n"
            f"control_stdout={ok['stdout']!r}\n"
            f"control_stderr={ok['stderr']!r}\n"
            f"control_raised={ok['raised']}\n"
            f"tools_after_control={tools_final}\n"
            f"record_memoryMB={detail.json().get('memoryMB')}\n"
            f"failures={failures}\n",
        )
        print("EVIDENCE FILE:", evidence)
        print("FAILURES:", failures)
    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
            except Exception:
                pass
        harness["_stop"]()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
