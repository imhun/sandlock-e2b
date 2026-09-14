"""Flake #2 (_free_port): the port a server is given must already be bound.

The harness used to probe a free port with ``bind(("127.0.0.1", 0))``, close
it, and let uvicorn bind that number later. Between the two binds the port is
free -- and it comes from the kernel's ephemeral range, the same range every
loopback connection in the suite draws its *source* port from (thousands per
lane), which is also where the worker's MCP-gateway port pool starts
(51000). Observed as ``_ServerThread.start`` -> ``RuntimeError: server failed to
start`` / ``[Errno 98] address already in use`` on a loaded whole-lane round.

The fix hands the *bound listening socket* to uvicorn (``sock=``), which
removes the window instead of narrowing it, and the pool it draws from sits
below both shared ranges. Both halves are asserted here.
"""

from __future__ import annotations

import errno
import socket
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from envd_service.runtime.context import _MCP_PORT_BASE
from tests.conftest import _PORT_POOL_MAX, _ServerThread, _bind_low_port


def test_a_reserved_port_cannot_be_taken_before_the_server_serves_it() -> None:
    """The reservation IS the listening socket uvicorn serves on.

    A second binder -- another test's probe, an outgoing connection's source
    port, a sandbox MCP gateway the worker's port pool hands the same number --
    must fail with ``EADDRINUSE`` in that window instead of taking the port
    from under the server, and the server must then answer on that same socket.
    """
    port, sock = _bind_low_port()
    intruder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        with pytest.raises(OSError) as excinfo:
            intruder.bind(("127.0.0.1", port))
        assert excinfo.value.errno == errno.EADDRINUSE
    finally:
        intruder.close()

    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    server = _ServerThread(app, port, sock=sock)
    server.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert response.status_code == 200
        assert response.json() == {"ok": "yes"}
    finally:
        server.stop()


def test_the_harness_port_pool_stays_out_of_the_shared_ranges() -> None:
    """The pool sits below the local ephemeral floor and the MCP pool base.

    Both ranges are handed out without a bind of their own -- the kernel for
    connection source ports, ``McpPortPool`` (base 51000) for sandbox gateways
    -- so a harness port chosen inside them can be claimed by a *non-listener*
    at any moment. Any value in the pool above either floor reintroduces the
    class of collision this flake was.
    """
    ephemeral_floor = 49152  # macOS default; Linux reports its own below
    try:
        first, _last = (
            Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()
        )
        ephemeral_floor = int(first)
    except (OSError, ValueError):
        pass
    assert _PORT_POOL_MAX <= ephemeral_floor
    assert _PORT_POOL_MAX <= _MCP_PORT_BASE
