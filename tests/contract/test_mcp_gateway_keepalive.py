"""The gateway inside a *sandbox* pins the same idle keep-alive rule.

``envd_service/mcp/gateway.py`` is copied into the MCP base image as
``/usr/bin/mcp-gateway`` (``deploy/docker/Dockerfile.mcp-base``) and runs there
standalone -- it cannot import ``gateway_common.keepalive``, so the value is
inlined in the script. These tests pin both ends of that inlining:

* the source pins ``timeout_keep_alive`` to the shared constant (measured, not
  inherited from uvicorn, whose 5s default is *below* the client's pool window
  -- the ordering that costs a non-replayable bidi ``process.Process/Start``
  its whole RPC as ``Connection reset by peer``);
* the *image* ships exactly those bytes, and the gateway served from it does
  not close an idle pooled connection inside the old 5s window -- the
  sandbox-side half of "the client is the side that closes".
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid

import httpx
import pytest

from gateway_common.keepalive import CLIENT_POOL_IDLE_TIMEOUT_S, SERVER_KEEP_ALIVE_S
from tests.conftest import PROJECT_ROOT, _docker_container, _published_port

_GATEWAY_SOURCE = PROJECT_ROOT / "envd_service" / "mcp" / "gateway.py"

#: uvicorn's own default: the window the sandbox gateway used to close at, and
#: the ordering this file exists to keep out.
_UVICORN_DEFAULT_KEEP_ALIVE_S = 5.0

#: Idle time before the reuse attempt: longer than the old default (so the
#: unfixed image fails), far below both the fixed window and the client pool's.
_IDLE_WINDOW_S = 12.0

#: The port the gateway listens on *inside* the container. Docker publishes it
#: on a host port it picks itself (``-p 127.0.0.1::50005``) and the harness
#: reads it back, so nothing probes a host port and rebinds it later.
_GATEWAY_PORT = 50005

#: A minimal stdio MCP server for the gateway to proxy (it initializes a
#: session with it before serving HTTP).
_ECHO_SERVER = """
from mcp.server.mcpserver import MCPServer

server = MCPServer("echo", version="1.0.0")


@server.tool()
async def echo(text: str) -> str:
    return f"echo:{text}"


server.run(transport="stdio")
"""

_UNAUTHORIZED_BODY = (
    b'{"jsonrpc":"2.0","error":{"code":-32001,"message":"unauthorized"}}'
)


def _base_image() -> str:
    """The MCP-capable base image the sandboxes run on.

    An *explicitly empty* ``E2B_BASE_IMAGE`` is how a lane asks for the
    pure (no-image-rootfs) shape -- `deploy/scripts/test-prod-shaped.sh` cannot
    express it (its ``${VAR:-default}`` turns the empty value back into the
    default), but ``tmp/k0s/gateB-full.sh`` does, and the deployment-shape
    selectors are deliberately outside the strict-skip list. There is no image
    to inspect in that shape: `docker run ""` would report a docker usage
    error, which reads as a broken contract rather than a narrower matrix.
    """
    image = os.environ.get("E2B_BASE_IMAGE")
    if image == "":
        pytest.skip("no base image configured (pure shape): nothing to inspect")
    return image or "python-mcp:3.14"


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.fail("docker is required to exercise the sandbox image's mcp-gateway")


def _uvicorn_config_keywords(source: str) -> dict[str, ast.expr]:
    """The keyword arguments of the single ``uvicorn.Config(...)`` call."""
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Config"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    ]
    assert len(calls) == 1, "the gateway should build exactly one uvicorn.Config"
    return {keyword.arg: keyword.value for keyword in calls[0].keywords}


def _module_constant(source: str, name: str) -> object:
    """The literal assigned to ``name`` at module level (exactly once)."""
    values = [
        ast.literal_eval(node.value)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        )
    ]
    assert len(values) == 1, f"{name} must be assigned exactly once at module level"
    return values[0]


def _read_response(conn: socket.socket) -> tuple[bytes, bytes]:
    """One ``GET /mcp``; ``(status line, body)`` read off the same connection."""
    conn.sendall(b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(4096)
        if not chunk:
            raise AssertionError(
                f"the server closed before answering (read {buffer!r})"
            )
        buffer += chunk
    head, _, body = buffer.partition(b"\r\n\r\n")
    length = int(re.search(rb"\r\ncontent-length: *(\d+)", b"\r\n" + head.lower())[1])
    while len(body) < length:
        chunk = conn.recv(4096)
        if not chunk:
            raise AssertionError(f"the server closed mid-body (read {body!r})")
        body += chunk
    return head.split(b"\r\n", 1)[0], body[:length]


def test_the_sandbox_gateway_pins_the_shared_keep_alive() -> None:
    """The in-sandbox server follows the same rule as the host-side entries.

    uvicorn's default (5s) is shorter than the client pool's idle window, i.e.
    the server closes an idle pooled connection first. The script cannot import
    ``gateway_common`` where it runs, so this asserts the inlined copy is the
    shared value -- a drift makes the two sides disagree silently.
    """
    source = _GATEWAY_SOURCE.read_text(encoding="utf-8")
    keep_alive = _uvicorn_config_keywords(source)["timeout_keep_alive"]
    assert isinstance(keep_alive, ast.Name)
    assert keep_alive.id == "SERVER_KEEP_ALIVE_S"

    assert _module_constant(source, "SERVER_KEEP_ALIVE_S") == SERVER_KEEP_ALIVE_S
    assert SERVER_KEEP_ALIVE_S == 120.0
    assert SERVER_KEEP_ALIVE_S != _UVICORN_DEFAULT_KEEP_ALIVE_S
    assert SERVER_KEEP_ALIVE_S > CLIENT_POOL_IDLE_TIMEOUT_S


def test_the_mcp_base_image_ships_the_gateway_this_tree_builds() -> None:
    """Byte-for-byte: the image is the artifact the sandbox actually runs.

    ``Dockerfile.mcp-base`` COPYs the script in at build time, so an edit that
    is not followed by a rebuild leaves every sandbox on the old parameters.
    Equality (and the same inlined constant read back out of the shipped
    bytes) turns that into a failure that names the rebuild instead of a silent
    drift.
    """
    _require_docker()
    image = _base_image()
    shipped = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat", image, "/usr/bin/mcp-gateway"],
        capture_output=True,
    )
    assert shipped.returncode == 0, (
        f"cannot read /usr/bin/mcp-gateway out of {image}: "
        f"{shipped.stderr.decode(errors='replace').strip()}\n"
        "Rebuild the MCP base image from this tree: docker build -f "
        "deploy/docker/Dockerfile.mcp-base -t python-mcp:3.14 ."
    )
    assert shipped.stdout == _GATEWAY_SOURCE.read_bytes(), (
        f"{image} ships a different /usr/bin/mcp-gateway than "
        f"{_GATEWAY_SOURCE.relative_to(PROJECT_ROOT)}; rebuild it with "
        "docker build -f deploy/docker/Dockerfile.mcp-base -t python-mcp:3.14 ."
    )
    shipped_source = shipped.stdout.decode("utf-8")
    assert (
        _module_constant(shipped_source, "SERVER_KEEP_ALIVE_S") == SERVER_KEEP_ALIVE_S
    )


def test_the_sandbox_gateway_does_not_close_an_idle_connection_first() -> None:
    """Idle past the old 5s window, then reuse the *same* TCP connection.

    One request (the auth middleware's 401 keeps the connection alive), silence
    for longer than uvicorn's default keep-alive -- which used to close it at
    ~4.6s -- and then a second request on that same socket. Both status lines
    and bodies are asserted exactly: a server that closed first shows up as an
    empty read here, which is what the SDK's pooled client would experience as
    ``Connection reset by peer`` on its next ``process.Process/Start``.
    """
    _require_docker()
    image = _base_image()
    config = json.dumps(
        {"name": "echo", "command": "python3", "args": ["-c", _ECHO_SERVER]}
    )
    container = ""
    try:
        container = _docker_container(
            f"mcp-gateway-keepalive-{uuid.uuid4().hex[:8]}",
            [
                "-p",
                f"127.0.0.1::{_GATEWAY_PORT}",
                "-e",
                "GATEWAY_ACCESS_TOKEN=keepalive-probe-token",
                "-e",
                f"MCP_PORT={_GATEWAY_PORT}",
                image,
                "python3",
                "/usr/bin/mcp-gateway",
                "--config",
                config,
                "--foreground",
            ],
        )
        port = _published_port(container, _GATEWAY_PORT)
        # Readiness is "the gateway answers HTTP", not "the published port
        # accepts": ``-p`` is served by docker's own proxy, which completes the
        # TCP handshake even while the process behind it is still starting (and
        # then resets the connection on the first byte).
        deadline = time.time() + 60
        ready = False
        while not ready:
            try:
                # The auth middleware answers before the MCP app sees the
                # request, so a 401 is the gateway being up and serving.
                ready = (
                    httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=2).status_code
                    == 401
                )
            except httpx.HTTPError:
                pass
            if not ready and time.time() > deadline:
                logs = subprocess.run(
                    ["docker", "logs", container], capture_output=True, text=True
                )
                pytest.fail(
                    f"the gateway in {image} never answered 401 on "
                    f"127.0.0.1:{port}/mcp within 60s\n"
                    f"--- gateway log tail ---\n{(logs.stdout + logs.stderr)[-2000:]}",
                    pytrace=False,
                )
            if not ready:
                time.sleep(0.5)

        with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
            conn.settimeout(30)
            status, body = _read_response(conn)
            assert status == b"HTTP/1.1 401 Unauthorized"
            assert body == _UNAUTHORIZED_BODY

            time.sleep(_IDLE_WINDOW_S)
            try:
                status, body = _read_response(conn)
            except (OSError, AssertionError) as exc:
                raise AssertionError(
                    f"the mcp-gateway in {image} closed a connection that was "
                    f"idle {_IDLE_WINDOW_S:.0f}s (uvicorn default "
                    f"{_UVICORN_DEFAULT_KEEP_ALIVE_S:.0f}s); a pooled client parks "
                    f"one for {CLIENT_POOL_IDLE_TIMEOUT_S:.0f}s and then reuses it "
                    "for a non-replayable bidi request, which then dies as "
                    f"pyqwest.WriteError: Connection reset by peer ({exc}). "
                    "Rebuild the base image after editing "
                    "envd_service/mcp/gateway.py."
                ) from exc
            assert status == b"HTTP/1.1 401 Unauthorized"
            assert body == _UNAUTHORIZED_BODY
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
