"""FUP-E3/F11 contract: gateway + command share the boxed 1 GiB quota.

The Task-8 (FUP-E3) gateway+command pairing was not expressible on the
pre-F11 fork wheel: once the multithreaded MCP gateway ran, every later exec
was denied by the argv-safety freeze EPERM (exit 127), and on the old 512 MiB
box the gateway ledger left no headroom for a 450 MiB MCP server child.
Both blockers are closed: fork F11 normalizes ProcessIndex keys to unique
TGIDs before the exec freeze (``edd8c76``/``927d015``), and the E2B default
per-sandbox memory is 1 GiB (``E2B_DEFAULT_MEMORY_MB``). This contract proves
the original scenario on the F11 wheel:

* an MCP gateway whose stdio echo server allocates and touches 450 MiB at
  import is reachable (``list_tools`` returns exactly ``["echo"]``);
* a trivial command after the gateway runs with exit 0 (the F11 regression
  guard: pre-F11 every later exec died exit 127);
* a second concurrent 450 MiB command is denied with the exact SDK signature
  recorded by the F11 probe (``tmp/perf/f11-gateway-probe-450-450-50*.log`` and
  ``tmp/perf/f11-gateway-sdk-signature-variants.txt``): exit code 137, empty
  stdout, ``error is None``, stderr exactly one of the recorded set -- never
  a substring match and never an unobserved value;
* a 50 MiB control command succeeds exactly while the server still holds its
  450 MiB, and the gateway keeps serving ``["echo"]`` throughout;
* the public sandbox record reports the default ``memoryMB == 1024``.

Run requirements: pure-sandlock container shape with ``E2B_BASE_IMAGE=``
empty; the image-rootfs gate A shape with ``E2B_BASE_IMAGE=python-mcp:3.14``
also covers this file. No extra concurrency is needed: this test issues SDK
commands sequentially (``E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2`` is only
required by the sibling-exec contract in ``test_memory_quota_boxed.py``).
Skipped outside the Linux sandlock runner (macOS host runs cover the executor
mapping unit-level instead; run inside the Docker test runner).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from e2b import Sandbox
from e2b.sandbox.commands.command_handle import CommandExitException
from tests.security.conftest import sandlock_ready

pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "sandlock boxed memory quota contract tests need Linux + sandlock "
        "(run inside the Docker test runner)"
    ),
)

#: Default per-sandbox memory ceiling asserted through the public record.
DEFAULT_MEMORY_MB = 1024

#: The MCP stdio echo server holds this much memory (touched pages) inside
#: the box for the whole test, so a sibling 450 MiB command overcommits
#: (gateway ledger + 450 + 450 > 1024) while a 50 MiB one still fits.
SERVER_HOLD_MB = 450
DENIED_MB = 450
CONTROL_MB = 50

TRIVIAL_MARKER = "post-gateway-ok"

#: Exact SDK-visible denial signature recorded by the F11 probe
#: (tmp/perf/f11-gateway-sdk-signature-variants.txt): the sandlock supervisor
#: SIGKILLs the over-budget python task and its bash wrapper reports 128+9.
#: stderr was observed as ``""`` in all four probe runs
#: (tmp/perf/f11-gateway-probe-450-450-50*.log) and as ``"Killed\n"`` in the
#: container pytest gate (tmp/f11-e2b-contract-gw1.log); the exact recorded
#: two-element set is asserted -- never a substring, never an unobserved
#: value.
DENIED_EXIT_CODE = 137
DENIED_STDERR_SET = ("", "Killed\n")


# The MCP stdio server: allocate SERVER_HOLD_MB + touch every page at import
# time, then serve the echo tool (same pattern as test_mcp_netns.py and the
# task-8/F11 probes).
MCP_HELD_SERVER = r"""
import asyncio
_BUF = bytearray(SERVER_HOLD_MB_ * 1024 * 1024)
for _i in range(0, len(_BUF), 4096):
    _BUF[_i] = 1
from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return f"echo:{text}"

server.run(transport="stdio")
""".replace("SERVER_HOLD_MB_", str(SERVER_HOLD_MB))


def alloc_script(mb: int, hold: float) -> str:
    """Python script: allocate ``mb`` MiB, touch every page, print, and hold."""
    parts = [
        "import time",
        f"buf=bytearray({mb}*1024*1024)",
        "for i in range(0,len(buf),4096): buf[i]=1",
        f"print('got {mb}',flush=True)",
    ]
    parts.append(f"time.sleep({hold})")
    return "\n".join(parts)


def py_cmd(script: str) -> str:
    """SDK command line running ``script`` under python3."""
    return f"/usr/local/bin/python3 -c \"{script}\""


def sandbox_opts(harness) -> dict[str, str]:
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


async def _mcp_tool_names(url: str, headers: dict[str, str]) -> list[str]:
    """Initialize one MCP session and return the exact tool-name list."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers=headers, timeout=20.0) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]


def _list_tools(sandbox, harness) -> list[str]:
    token = sandbox.get_mcp_token()
    assert token, "no mcp token"
    url = f"{harness['sandbox_url'].rstrip('/')}/mcp"
    headers = {
        "E2b-Sandbox-Id": sandbox.sandbox_id,
        "Authorization": f"Bearer {token}",
    }
    return asyncio.run(_mcp_tool_names(url, headers))


def _wait_gateway_serving_echo(sandbox, harness, deadline_s: float = 60.0) -> None:
    """Wait until list_tools returns exactly ``["echo"]`` (the 450 MiB server
    child is alive and initialized); loud on timeout so a broken gateway or a
    killed server cannot make later assertions vacuously pass."""
    deadline = time.time() + deadline_s
    last: object | None = None
    while time.time() < deadline:
        try:
            tools = _list_tools(sandbox, harness)
        except Exception as exc:  # noqa: BLE001 - gateway still starting
            last = exc
            time.sleep(0.5)
            continue
        if tools == ["echo"]:
            return
        last = tools
        time.sleep(0.5)
    raise AssertionError(
        "MCP gateway/450 MiB server never served ['echo'] "
        f"(last observation: {last!r})"
    )


@pytest.fixture(scope="session")
def gateway_quota_servers():
    """One real control plane + worker + envd gateway (pure sandlock sandboxes
    only, so no buildkitd dependency -- same shape as the probe harness)."""
    if not sandlock_ready():
        pytest.skip(
            "gateway boxed-memory contract needs Linux + sandlock "
            "(run inside the Docker test runner)"
        )
    from tests.conftest import PROJECT_ROOT, _start_multinode

    harness = _start_multinode(
        PROJECT_ROOT / "tmp" / "multinode-gw-quota",
        1,
        buildkit_addr=None,
    )
    yield harness
    harness["_stop"]()


def test_gateway_and_command_share_boxed_quota(gateway_quota_servers) -> None:
    """MCP gateway (450 MiB server holder) + post-gateway exec + over-budget
    sibling denial + in-budget control on the shared 1 GiB box."""
    harness = gateway_quota_servers
    sandbox = Sandbox.create(
        mcp={
            "name": "echo",
            "command": "python3",
            "args": ["-c", MCP_HELD_SERVER],
        },
        **sandbox_opts(harness),
    )
    try:
        # The sandbox is created with the default per-sandbox memory: the
        # public record must say exactly 1024 MiB.
        detail = httpx.get(
            f"{harness['api_url'].rstrip('/')}/sandboxes/{sandbox.sandbox_id}",
            headers={"X-API-Key": "local-key"},
            timeout=10,
        )
        assert detail.status_code == 200
        assert detail.json()["memoryMB"] == DEFAULT_MEMORY_MB

        # Outcome 1: the gateway plus its 450 MiB stdio server is reachable
        # (list_tools works and reports exactly the echo tool).
        _wait_gateway_serving_echo(sandbox, harness)

        # Outcome 2 (F11 regression guard): a trivial command after the
        # gateway runs cleanly -- pre-F11 every later exec died exit 127.
        trivial = sandbox.commands.run(
            py_cmd(f"print('{TRIVIAL_MARKER}', flush=True)"),
            timeout=60,
        )
        assert trivial.exit_code == 0
        assert trivial.stdout == f"{TRIVIAL_MARKER}\n"
        assert trivial.stderr == ""

        # Outcome 3: a second concurrent 450 MiB command against the same box
        # (gateway ledger + 450 MiB server holder + 450 MiB sibling) is denied
        # with the exact recorded SDK signature.
        with pytest.raises(CommandExitException) as excinfo:
            sandbox.commands.run(
                py_cmd(alloc_script(DENIED_MB, hold=2.0)),
                timeout=60,
            )
        assert excinfo.value.exit_code == DENIED_EXIT_CODE
        assert excinfo.value.stdout == ""
        assert excinfo.value.stderr in DENIED_STDERR_SET
        assert excinfo.value.error is None

        # The denial killed the over-budget task only: the gateway is still
        # alive and still serving the 450 MiB holder's echo tool.
        assert _list_tools(sandbox, harness) == ["echo"]

        # Outcome 4: with the server still holding its 450 MiB, a 50 MiB
        # control command stays under the 1 GiB box quota and succeeds
        # exactly; the gateway keeps serving afterwards.
        control = sandbox.commands.run(
            py_cmd(alloc_script(CONTROL_MB, hold=2.0)),
            timeout=60,
        )
        assert control.exit_code == 0
        assert control.stdout == f"got {CONTROL_MB}\n"
        assert control.stderr == ""
        assert _list_tools(sandbox, harness) == ["echo"]
    finally:
        try:
            sandbox.kill()
        except Exception:
            pass
