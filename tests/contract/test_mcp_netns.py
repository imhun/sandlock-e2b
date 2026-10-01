"""E7.1/E7.2 contract: MCP full path and CPython connect under
`net_isolation` (per-sandbox netns + fd-injected connect + inbound mapping).

The harness runs the control plane + one real envd worker (sandlock
executor) + the envd gateway with ``enable_net_isolation`` and
``fd_inject_connect`` enabled on the worker. Verifies:

* SDK -> gateway -> /mcp proxy -> host mapped port (50005+) -> sandbox's
  own accept() -> in-sandbox mcp-gateway -> stdio MCP server;
* in-sandbox CPython ``socket.connect()`` to an allowed host works on the
  fd-injection path (connect must return 0, not the injected fd number).

Run requirement: when ``E2B_BASE_IMAGE`` is set (image-rootfs/chroot shape)
the base image must be MCP-capable (``python-mcp:3.14`` built from
``deploy/docker/Dockerfile.mcp-base``); a plain python slim rootfs cannot exec
the envd gateway (``/usr/local/bin/mcp-gateway`` missing -> ENOENT, exit 2),
which is a base-image composition property, not a defect of this contract.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

_NETNS_TEST_ENV = "E2B_TEST_NET_ISOLATION"

pytestmark = pytest.mark.skipif(
    os.environ.get(_NETNS_TEST_ENV) != "1",
    reason=f"set {_NETNS_TEST_ENV}=1 to run the net-isolation shape "
    "(worker needs E2B_ENABLE_NET_ISOLATION=true E2B_FD_INJECT_CONNECT=true)",
)


def _linux_sandlock_ready() -> bool:
    if sys.platform != "linux":
        return False
    try:
        import sandlock

        return sandlock.landlock_abi_version() >= 6
    except Exception:
        return False


def _netns_servers(buildkit_addr):
    from tests.conftest import TMP_ROOT, _start_multinode

    # The harness root goes under ``TMP_ROOT`` (``E2B_TEST_TMP_ROOT``), never
    # ``PROJECT_ROOT/tmp``: the runner bind-mounts the repo into the container,
    # so a repo-relative root is the *same* directory for every concurrent
    # container of this suite and ``_start_multinode``'s ``_fresh_dir`` wipes
    # it. A second lane starting while this one runs therefore deleted live
    # sandboxes' workspace trees (the ``/home/user`` chdir of a command then
    # failed with ENOENT, and the in-sandbox MCP gateway's stdio server died --
    # the "exit 125/127 out of nowhere" family). ``TMP_ROOT`` is
    # container-native in the runner, which is also why every other harness
    # already lives there.
    harness = _start_multinode(
        TMP_ROOT / "multinode-netns",
        1,
        buildkit_addr=buildkit_addr,
        envd_settings_extra={
            "enable_net_isolation": True,
            "fd_inject_connect": True,
        },
    )
    return harness


@pytest.fixture(scope="session")
def netns_servers():
    if not _linux_sandlock_ready():
        pytest.skip(
            "net-isolation contract tests need Linux + sandlock "
            "(run inside the Docker test runner)"
        )
    # No buildkit dependency: this harness only creates sandlock sandboxes
    # without template images, so docker-in-docker is not required.
    harness = _netns_servers(None)
    yield harness
    harness["_stop"]()


def _opts(harness):
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


def _benchmark_loopback_address() -> str | None:
    """A free ``198.18.x.y/32`` on ``lo``, or ``None`` without NET_ADMIN.

    The benchmark range is the one the platform's private-range guard leaves
    alone (``test_network_enforcement`` uses the same range), so it is how a
    test reaches a hermetic local origin through an ``allowOut`` literal --
    loopback itself is now refused as an egress target (SEC-K0S-004).
    """
    for third in range(10, 20):
        for fourth in range(2, 254):
            candidate = f"198.18.{third}.{fourth}"
            add = subprocess.run(
                ["ip", "addr", "add", f"{candidate}/32", "dev", "lo"],
                check=False,
                capture_output=True,
            )
            if add.returncode == 0:
                return candidate
    return None


@pytest.fixture()
def origin_alias():
    """A hermetic local-origin address that ``allowOut`` may name."""
    addr = _benchmark_loopback_address()
    if addr is None:
        pytest.skip(
            "local-origin fixture needs a free 198.18.x.x loopback address "
            "(run with --cap-add NET_ADMIN)"
        )
    try:
        yield addr
    finally:
        subprocess.run(
            ["ip", "addr", "del", f"{addr}/32", "dev", "lo"],
            check=False,
            capture_output=True,
        )


@pytest.fixture()
def wildcard_alias():
    """198.18.0.x loopback address + /etc/hosts entry for a wildcard domain
    (the SSRF-guard-allowed benchmark range), removed on teardown."""
    hostname = "api.wild.test"
    addr = _benchmark_loopback_address()
    if addr is None:
        pytest.skip(
            "wildcard local-origin fixture needs a free 198.18.x.x loopback "
            "address (run with --cap-add NET_ADMIN)"
        )
    hosts_line = f"{addr} {hostname}\n"
    with open("/etc/hosts", "a", encoding="utf-8") as f:
        f.write(hosts_line)
    try:
        yield hostname, addr
    finally:
        try:
            with open("/etc/hosts", "r", encoding="utf-8") as f:
                lines = [line for line in f if line != hosts_line]
            with open("/etc/hosts", "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass
        subprocess.run(
            ["ip", "addr", "del", f"{addr}/32", "dev", "lo"],
            check=False,
            capture_output=True,
        )


ECHO_SERVER = r"""
import asyncio
from mcp.server.mcpserver import MCPServer
server = MCPServer("echo", version="1.0.0")

@server.tool()
async def echo(text: str) -> str:
    return f"echo:{text}"

server.run(transport="stdio")
"""


# FUP-E1 closed (2026-09-06): the image-rootfs (chroot) shape passes end to end
# when the base image carries the MCP runtime (mcp + uvicorn + mcp-gateway, see
# deploy/docker/Dockerfile.mcp-base -> python-mcp:3.14). A plain python slim
# rootfs cannot exec the worker's gateway (ENOENT), which is a base-image
# composition requirement, not an inbound-mapping defect: with an MCP-capable
# image the S2.5 mapping serves the chroot netns listener and this contract
# asserts the full path unconditionally.
def test_mcp_full_path_under_net_isolation(netns_servers):
    """SDK -> gateway -> envd /mcp proxy -> S2.5 inbound mapping -> sandbox
    mcp-gateway: list_tools and call_tool must work end to end."""
    from e2b import Sandbox

    sandbox = Sandbox.create(
        mcp={"name": "echo", "command": "python3", "args": ["-c", ECHO_SERVER]},
        **_opts(netns_servers),
    )
    try:
        token = sandbox.get_mcp_token()
        assert token
        url = f"{netns_servers['sandbox_url'].rstrip('/')}/mcp"
        sandbox_headers = {"E2b-Sandbox-Id": sandbox.sandbox_id}

        # The gateway is a background sandbox process; wait for the HTTP
        # endpoint to answer (4xx means reachable; only connection errors
        # retry).
        import httpx

        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                resp = httpx.get(
                    url,
                    headers={**sandbox_headers, "Authorization": f"Bearer {token}"},
                    timeout=2,
                )
                if resp.status_code < 500:
                    break
            except (httpx.HTTPError, OSError):
                time.sleep(0.3)
        else:
            raise AssertionError("mcp-gateway did not start listening")

        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from httpx2 import AsyncClient

        async def call():
            async with AsyncClient(
                headers={
                    **sandbox_headers,
                    "Authorization": f"Bearer {token}",
                }
            ) as http:
                async with streamable_http_client(url, http_client=http) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert [t.name for t in tools.tools] == ["echo"]
                        result = await session.call_tool("echo", {"text": "hello"})
                        return result.content[0].text

        assert asyncio.run(call()) == "echo:hello"
    finally:
        sandbox.kill()


def test_cpython_connect_under_net_isolation(netns_servers, origin_alias):
    """E7.2: CPython socket.connect() inside a netns sandbox works on the
    fd-injection path (the supervisor returns 0 and swaps the child's own
    socket fd)."""
    from e2b import Sandbox

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Bound on the benchmark-range alias rather than loopback: the platform
    # refuses loopback as an egress *target* (SEC-K0S-004), so a hermetic
    # origin has to live on an address an `allowOut` literal can still name.
    listener.bind((origin_alias, 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received: list[bytes] = []

    def _serve():
        conn, _ = listener.accept()
        data = conn.recv(4)
        received.append(data)
        conn.sendall(data)
        conn.close()
        listener.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    sandbox = Sandbox.create(
        network={"allow_out": [f"{origin_alias}:{port}"]},
        **_opts(netns_servers),
    )
    try:
        code = (
            "import socket;"
            f"s=socket.socket();s.settimeout(5);"
            f"s.connect((\"{origin_alias}\",{port}));"
            "s.sendall(b\"ping\");"
            "d=s.recv(4);"
            "print(\"RESULT:\"+d.decode());"
            "s.close()"
        )
        result = sandbox.commands.run(f"/usr/local/bin/python3 -c '{code}'")
        assert result.exit_code == 0, f"stderr: {result.stderr!r}"
        assert result.stdout == "RESULT:ping\n"
        assert received == [b"ping"], "host echo server must receive the ping"
    finally:
        sandbox.kill()
        thread.join(timeout=5)


def test_wildcard_connect_under_net_isolation(netns_servers, wildcard_alias):
    """E7.3: a wildcard allowOut rule resolves through the sandbox's in-netns
    DNS gateway (synthetic IP) and connects via outbound fd injection; the
    bare apex domain stays refused."""
    from e2b import Sandbox

    _, addr = wildcard_alias
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((addr, 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received: list[bytes] = []

    def _serve():
        conn, _ = listener.accept()
        data = conn.recv(4)
        received.append(data)
        conn.sendall(data)
        conn.close()
        listener.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    sandbox = Sandbox.create(
        network={"allow_out": [f"*.wild.test:{port}"]},
        **_opts(netns_servers),
    )
    try:
        code = (
            "import socket;"
            f"s=socket.create_connection((\"api.wild.test\",{port}),timeout=8);"
            "s.sendall(b\"ping\");"
            "d=s.recv(4);"
            "print(\"RESULT:\"+d.decode());"
            "s.close()"
        )
        result = sandbox.commands.run(f"/usr/local/bin/python3 -c '{code}'")
        assert result.exit_code == 0, f"stderr: {result.stderr!r}"
        assert result.stdout == "RESULT:ping\n"
        assert received == [b"ping"]

        from e2b.sandbox.commands.command_handle import CommandExitException

        bare = (
            "import socket;"
            "s=socket.socket();s.settimeout(5);"
            f"s.connect((\"wild.test\",{port}));"
            "print(\"BARE_ALLOWED\");s.close()"
        )
        with pytest.raises(CommandExitException):
            sandbox.commands.run(f"/usr/local/bin/python3 -c '{bare}'")
    finally:
        sandbox.kill()
        thread.join(timeout=5)
