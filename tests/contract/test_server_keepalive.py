"""Flake #3 (connection reset): which side closes an idle connection first.

The SDK's transport is pyqwest, whose pool parks an idle connection for
``pool_idle_timeout`` (90s by default, not overridden by the SDK) and then
reuses it for a *bidi* ``process.Process/Start``. That request body is a stream
the transport cannot replay, so a connection the *server* closed underneath it
costs the RPC: ``pyqwest.WriteError: ... Connection reset by peer (os error
104)``, observed on a loaded whole-lane round of
``tests/contract/test_memory_quota_gateway_command.py`` after the MCP gateway
needed longer than uvicorn's default ``timeout_keep_alive=5`` to come up.

The rule that removes it is the usual one: the server's idle keep-alive must be
strictly greater than the client's pool idle window, so the *client* is the side
that closes an idle connection (``gateway_common.keepalive``). A longer client
timeout or a retry would not do -- the reset is not replayable.
"""

from __future__ import annotations

import ast
import inspect
import re
import socket
import time

import pytest
from fastapi import FastAPI

from gateway_common.keepalive import (
    CLIENT_POOL_IDLE_TIMEOUT_S,
    SERVER_KEEP_ALIVE_S,
    uvicorn_keep_alive_kwargs,
)
from tests.conftest import PROJECT_ROOT, _ServerThread, _bind_low_port

#: Longer than uvicorn's default 5s (the window the server used to close at),
#: short enough to stay cheap: the point is the *ordering*, which the assertions
#: on the two constants pin exactly.
_IDLE_WINDOW_S = 12.0

#: Every process that serves SDK traffic over uvicorn.
_UVICORN_ENTRY_POINTS = (
    "control_plane/__main__.py",
    "control_plane/combined_main.py",
    "envd_service/__main__.py",
    "envd_service/gateway_main.py",
)


def _request_and_read(conn: socket.socket) -> bytes:
    """Send one ``GET /ping`` and read the complete HTTP/1.1 response."""
    conn.sendall(b"GET /ping HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: */*\r\n\r\n")
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(4096)
        if not chunk:
            raise AssertionError(
                f"the server closed the connection before answering (read {buffer!r})"
            )
        buffer += chunk
    head, _, body = buffer.partition(b"\r\n\r\n")
    length = int(re.search(rb"\r\ncontent-length: *(\d+)", b"\r\n" + head.lower())[1])
    while len(body) < length:
        chunk = conn.recv(4096)
        if not chunk:
            raise AssertionError(
                f"the server closed the connection mid-body (read {body!r})"
            )
        body += chunk
    return head + b"\r\n\r\n" + body[:length]


def test_the_pinned_client_window_is_the_installed_transport_default() -> None:
    """Our two numbers must bracket the client the SDK actually ships.

    ``CLIENT_POOL_IDLE_TIMEOUT_S`` is asserted against the *installed* pyqwest
    default (exact float equality, not a range), so a transport whose idle
    window changes cannot silently invalidate the ordering rule below.
    """
    import pyqwest

    installed = inspect.signature(pyqwest.SyncHTTPTransport).parameters
    assert installed["pool_idle_timeout"].default == CLIENT_POOL_IDLE_TIMEOUT_S
    assert SERVER_KEEP_ALIVE_S > CLIENT_POOL_IDLE_TIMEOUT_S
    assert uvicorn_keep_alive_kwargs() == {"timeout_keep_alive": SERVER_KEEP_ALIVE_S}


def test_an_idle_pooled_connection_is_still_usable_after_the_old_window() -> None:
    """A pooled connection idle past the old 5s window must still answer.

    This is the behavior the SDK depends on: one TCP connection (as its pool
    holds), one request, ``_IDLE_WINDOW_S`` of silence -- which uvicorn's
    default closed at ~4.6s -- and then the *same* connection answers again.
    """
    port, sock = _bind_low_port()
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    server = _ServerThread(app, port, sock=sock)
    server.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
            conn.settimeout(10)
            first = _request_and_read(conn)
            assert first.startswith(b"HTTP/1.1 200 ")
            assert first.endswith(b'{"ok":"yes"}')
            time.sleep(_IDLE_WINDOW_S)
            try:
                second = _request_and_read(conn)
            except (OSError, AssertionError) as exc:
                raise AssertionError(
                    "the server closed a pooled connection that had been idle "
                    f"{_IDLE_WINDOW_S:.0f}s; the SDK would see this as "
                    f"pyqwest.WriteError: Connection reset by peer ({exc})"
                ) from exc
            assert second.startswith(b"HTTP/1.1 200 ")
            assert second.endswith(b'{"ok":"yes"}')
    finally:
        server.stop()


@pytest.mark.parametrize("module", _UVICORN_ENTRY_POINTS)
def test_every_uvicorn_entry_point_pins_the_keep_alive(module: str) -> None:
    """The wiring, not just the constant: every service must pass it to uvicorn.

    Parsed from the source so a call that drops ``**uvicorn_keep_alive_kwargs()``
    (falling back to uvicorn's 5s default, i.e. the failing ordering) is caught
    even though the app itself still starts.
    """
    tree = ast.parse((PROJECT_ROOT / module).read_text(encoding="utf-8"))
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "gateway_common.keepalive"
        and any(alias.name == "uvicorn_keep_alive_kwargs" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imported, f"{module} does not import uvicorn_keep_alive_kwargs"
    runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    ]
    assert len(runs) == 1, f"{module} should call uvicorn.run exactly once"
    spread = [
        keyword
        for keyword in runs[0].keywords
        if keyword.arg is None
        and isinstance(keyword.value, ast.Call)
        and isinstance(keyword.value.func, ast.Name)
        and keyword.value.func.id == "uvicorn_keep_alive_kwargs"
    ]
    assert len(spread) == 1, (
        f"{module}'s uvicorn.run does not spread **uvicorn_keep_alive_kwargs()"
    )
