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


# ---------------------------------------------------------------------------
# In-band pings for a *streaming* RPC (N37)
# ---------------------------------------------------------------------------
#
# The connection rule above is about a connection that is *idle between
# requests*. A streaming RPC has a second, harder case: one response is open
# for minutes while the sandbox prints nothing, so the only thing that can keep
# the connection looking alive is something sent **inside** that stream. The
# official SDK knows this and asks for exactly that: every ``Start`` (and the
# filesystem watch) carries ``Keepalive-Ping-Interval: 50``, and the protocol
# has an empty ``ProcessEvent.KeepAlive`` for the server to answer with.
#
# Measured 2026-09-27 on this deployment (N37): the edge in front of the API
# (``http://172.18.78.49:3000`` -> node ``.140:31907``) cuts a stream whose
# body has been silent for **60.0 s**, which the SDK reports as
# ``TimeoutException: ... unexpected EOF during chunk size line``. The same
# command against the control plane through a ``kubectl port-forward`` (no
# edge) runs to completion, and a command that keeps printing survives the
# edge -- so the cut is the edge's idle timeout and the fix is to keep the
# stream non-silent. The filesystem watch has always done this
# (``envd_service/filesystem/watch.py``, every 15 s); the process stream
# (``envd_service/rpc.py::_consume_stream``) did not, which is why a long
# silent command -- a 4000-file write on NFS, 94 s -- died at 60 s while the
# same work split into 2000-file commands (47 s each) survived.

#: What the official SDK asks for, ``e2b/connection_config.py``
#: ``KEEPALIVE_PING_INTERVAL_SEC``; it travels as the
#: ``Keepalive-Ping-Interval`` request header. Pinned against the *installed*
#: SDK by ``tests/contract/test_process_keepalive.py`` so a client that changes
#: its request cannot silently invalidate the two numbers below.
SDK_KEEPALIVE_PING_INTERVAL_S = 50.0

#: The ping cadence when a client does not ask for one -- the same 15 s the
#: filesystem watch uses, and comfortably under the edge's measured idle cut.
STREAM_KEEPALIVE_S = 15.0

#: Upper bound on a client-requested interval. The value only buys liveness,
#: so a client asking for something at or beyond the edge's idle window gets
#: the floor instead of a stream the edge is about to cut. Strictly below
#: ``EDGE_IDLE_CUT_S``.
STREAM_KEEPALIVE_MAX_S = 30.0

#: The deployment's edge idle cut, in seconds (measured 2026-09-27, N37).
#: Not ours to configure -- it is the reverse proxy in front of the API -- so
#: it is a *given* here: the numbers above are only useful while they stay
#: below it.
EDGE_IDLE_CUT_S = 60.0


def stream_keepalive_interval_s(header_value: str | None) -> float:
    """Resolve the in-band ping interval for one streaming RPC.

    ``header_value`` is the raw ``Keepalive-Ping-Interval`` request header.
    A missing, unparsable, or non-positive value falls back to
    :data:`STREAM_KEEPALIVE_S`; a value above :data:`STREAM_KEEPALIVE_MAX_S`
    is clamped down to it, because pinging *more* often than asked is
    harmless (the events are four bytes and the SDK ignores them) while
    pinging less often is exactly the failure this exists to prevent.
    """
    if header_value is None:
        return STREAM_KEEPALIVE_S
    try:
        requested = float(str(header_value).strip())
    except (TypeError, ValueError):
        return STREAM_KEEPALIVE_S
    if requested <= 0:
        return STREAM_KEEPALIVE_S
    return min(requested, STREAM_KEEPALIVE_MAX_S)
