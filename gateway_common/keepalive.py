"""Server-side idle keep-alive policy, shared by every uvicorn entry point.

The question this answers is "who closes an idle HTTP/1.1 connection first".
The official SDK talks to these servers through pyqwest, whose connection pool
parks an idle connection for ``pool_idle_timeout`` (90s by default, and the SDK
does not override it) and then reuses it for a *bidi* ``process.Process/Start``
request. That request body is a stream the transport cannot replay, so a
connection that the *server* closed underneath it costs the whole RPC: pyqwest
raises ``WriteError: ... Connection reset by peer (os error 104)`` and neither
the SDK's retry transport nor connectrpc can retry it (observed as flake #3,
``test_memory_quota_gateway_command.py`` on a loaded whole-lane run).

uvicorn's own default (``timeout_keep_alive=5``) is *shorter* than the client's
pool idle window, so the server closes first by construction -- 5s vs 90s, i.e.
the one ordering the client cannot survive. Raising it above the client window
makes the client the side that closes an idle connection, which is what the
usual rule asks for (a reverse proxy keeps its idle timeout above its clients';
nginx 75s vs browser 60s+) and what the peer handles as a plain FIN.

Both numbers are pinned here so they cannot drift apart silently, and
``tests/contract/test_server_keepalive.py`` asserts the *installed* pyqwest
default against ``SERVER_KEEP_ALIVE_S`` plus the behavior on a live server.
"""

from __future__ import annotations

#: pyqwest's ``HTTPTransport``/``SyncHTTPTransport`` ``pool_idle_timeout``
#: default: how long a client parks an idle connection before closing it
#: itself. The SDK builds its transports without overriding it.
CLIENT_POOL_IDLE_TIMEOUT_S = 90.0

#: Idle keep-alive every uvicorn server in this repo runs with. Strictly
#: greater than ``CLIENT_POOL_IDLE_TIMEOUT_S`` so the *client* is the side that
#: closes an idle connection (``>=`` would leave the two timers racing).
SERVER_KEEP_ALIVE_S = 120.0


def uvicorn_keep_alive_kwargs() -> dict[str, float]:
    """``uvicorn.run`` / ``uvicorn.Config`` kwargs pinning ``SERVER_KEEP_ALIVE_S``."""
    return {"timeout_keep_alive": SERVER_KEEP_ALIVE_S}
