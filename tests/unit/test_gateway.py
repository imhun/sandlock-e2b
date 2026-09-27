"""Gateway unit tests: configurable route TTL and single retry after the
node moved (migration) or the stale route failed to connect."""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from envd_service.gateway import (
    RouteCache,
    RouteInvalidationSubscriber,
    create_gateway,
)
from gateway_common import GATEWAY_ROUTE_INVALIDATE_CHANNEL
from tests.conftest import _ServerThread, _bind_low_port, _free_container_port


def _make_node(statuses: list[int], body: bytes = b"ok") -> tuple[FastAPI, list[str]]:
    """Mock sandbox node: returns the configured status per call."""
    app = FastAPI()
    calls: list[str] = []

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def handler(path: str) -> Response:
        calls.append(path)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        return Response(status_code=status, content=body)

    return app, calls


class _ControlPlane:
    """Mock control plane: per-sandbox route list, counts lookups."""

    def __init__(self) -> None:
        self.lookups: dict[str, int] = {}
        self.routes: dict[str, list[str]] = {}
        self.app = FastAPI()

        @self.app.get("/internal/routes/{sandbox_id}")
        def route(sandbox_id: str) -> dict[str, str]:
            self.lookups[sandbox_id] = self.lookups.get(sandbox_id, 0) + 1
            addrs = self.routes[sandbox_id]
            if len(addrs) > 1:
                self.routes[sandbox_id] = addrs[1:]
            return {"address": addrs[0]}

    def start(self) -> tuple[_ServerThread, int]:
        port, sock = _bind_low_port()
        server = _ServerThread(self.app, port, sock=sock)
        server.start()
        return server, port


async def _start_gateway(cp_url: str) -> tuple[_ServerThread, int]:
    app = create_gateway(
        control_plane_url=cp_url,
        internal_api_key="internal-key",
    )
    port, sock = _bind_low_port()
    server = _ServerThread(app, port, sock=sock)
    server.start()
    return server, port


async def _get(url: str, sandbox_id: str = "sbx_1", body: bytes = b"hi") -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.post(
            url,
            headers={
                "E2b-Sandbox-Id": sandbox_id,
                "Content-Type": "text/plain",
            },
            content=body,
        )


def test_route_cache_ttl_expiry() -> None:
    cache = RouteCache(ttl=0.2)
    cache.put("sbx_1", "http://node:49983")
    assert cache.get("sbx_1") == "http://node:49983"
    time.sleep(0.3)
    assert cache.get("sbx_1") is None


def test_route_cache_ttl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_GATEWAY_ROUTE_TTL", "2.5")
    app = create_gateway(
        control_plane_url="http://127.0.0.1:1",
        internal_api_key="internal-key",
    )
    assert app.state.route_cache._ttl == 2.5


@pytest.mark.asyncio
async def test_the_sdk_keepalive_interval_reaches_the_node() -> None:
    """N37: the forwarded header is what lets the worker pick the cadence.

    ``Keepalive-Ping-Interval`` rides the request the SDK sends on a streaming
    RPC, and the worker's process stream resolves its ping interval from it.
    The gateway forwards a *whitelist*, so a header missing from that list is
    dropped here and the node can never see it -- the client's only lever over
    an edge whose idle cut is tighter than ours. Asserted end to end (a real
    request through the real app into the node) rather than by inspecting the
    set, so the forwarding path itself is under test.
    """
    seen: dict[str, str] = {}
    node = FastAPI()

    @node.post("/process.Process/Start")
    async def start(request: Request) -> Response:
        seen.update(request.headers)
        return Response(status_code=200, content=b"")

    node_port, node_sock = _bind_low_port()
    node_server = _ServerThread(node, node_port, sock=node_sock)
    node_server.start()
    cp = _ControlPlane()
    cp.routes["sbx_1"] = [f"http://127.0.0.1:{node_server.server.config.port}"]
    cp_server, cp_port = cp.start()
    gw_server, gw_port = await _start_gateway(f"http://127.0.0.1:{cp_port}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"http://127.0.0.1:{gw_port}/process.Process/Start",
                headers={
                    "E2b-Sandbox-Id": "sbx_1",
                    "X-Access-Token": "tok",
                    "Content-Type": "application/connect+json",
                    "Keepalive-Ping-Interval": "50",
                },
                content=b"{}",
            )
        assert response.status_code == 200
        assert seen["keepalive-ping-interval"] == "50"
        assert seen["e2b-sandbox-id"] == "sbx_1"
        assert seen["x-access-token"] == "tok"
        # The framing headers stay the gateway's own, never the client's.
        assert seen["host"] == f"127.0.0.1:{node_server.server.config.port}"
    finally:
        for s in (node_server, cp_server, gw_server):
            s.stop()


@pytest.mark.asyncio
async def test_retry_replays_after_node_moved_502() -> None:
    """First node answers 502, control plane route moved; retry succeeds."""
    node1, calls1 = _make_node([502])
    node2, calls2 = _make_node([200], body=b"migrated-ok")
    s1_port, s1_sock = _bind_low_port()
    s2_port, s2_sock = _bind_low_port()
    s1 = _ServerThread(node1, s1_port, sock=s1_sock)
    s2 = _ServerThread(node2, s2_port, sock=s2_sock)
    s1.start()
    s2.start()
    cp = _ControlPlane()
    cp.routes["sbx_1"] = [
        f"http://127.0.0.1:{s1.server.config.port}",
        f"http://127.0.0.1:{s2.server.config.port}",
    ]
    cp_server, cp_port = cp.start()
    gw_server, gw_port = await _start_gateway(f"http://127.0.0.1:{cp_port}")
    try:
        resp = await _get(f"http://127.0.0.1:{gw_port}/commands")
        assert resp.status_code == 200
        assert resp.content == b"migrated-ok"
        assert cp.lookups["sbx_1"] == 2
        assert calls1 == ["commands"]
        assert calls2 == ["commands"]
    finally:
        for s in (s1, s2, cp_server, gw_server):
            s.stop()


@pytest.mark.asyncio
async def test_retry_replays_after_connection_failure() -> None:
    """First node is unreachable (connection refused); retry hits the new node."""
    dead_port = _free_container_port()  # nothing listens here
    node2, calls2 = _make_node([200], body=b"recovered")
    s2_port, s2_sock = _bind_low_port()
    s2 = _ServerThread(node2, s2_port, sock=s2_sock)
    s2.start()
    cp = _ControlPlane()
    cp.routes["sbx_1"] = [
        f"http://127.0.0.1:{dead_port}",
        f"http://127.0.0.1:{s2.server.config.port}",
    ]
    cp_server, cp_port = cp.start()
    gw_server, gw_port = await _start_gateway(f"http://127.0.0.1:{cp_port}")
    try:
        resp = await _get(f"http://127.0.0.1:{gw_port}/commands")
        assert resp.status_code == 200
        assert resp.content == b"recovered"
        assert cp.lookups["sbx_1"] == 2
        assert calls2 == ["commands"]
    finally:
        for s in (s2, cp_server, gw_server):
            s.stop()


@pytest.mark.asyncio
async def test_no_retry_loop_when_both_attempts_fail() -> None:
    """Stale route that never moves: exactly two attempts, then 502."""
    node1, calls1 = _make_node([502])
    s1_port, s1_sock = _bind_low_port()
    s1 = _ServerThread(node1, s1_port, sock=s1_sock)
    s1.start()
    cp = _ControlPlane()
    cp.routes["sbx_1"] = [f"http://127.0.0.1:{s1.server.config.port}"]
    cp_server, cp_port = cp.start()
    gw_server, gw_port = await _start_gateway(f"http://127.0.0.1:{cp_port}")
    try:
        resp = await _get(f"http://127.0.0.1:{gw_port}/commands")
        assert resp.status_code == 502
        assert len(calls1) == 2
        assert cp.lookups["sbx_1"] == 2
    finally:
        for s in (s1, cp_server, gw_server):
            s.stop()


@pytest.mark.asyncio
async def test_happy_path_streams_without_retry() -> None:
    node, calls = _make_node([200], body=b"ok")
    s_port, s_sock = _bind_low_port()
    s = _ServerThread(node, s_port, sock=s_sock)
    s.start()
    cp = _ControlPlane()
    cp.routes["sbx_1"] = [f"http://127.0.0.1:{s.server.config.port}"]
    cp_server, cp_port = cp.start()
    gw_server, gw_port = await _start_gateway(f"http://127.0.0.1:{cp_port}")
    try:
        resp = await _get(f"http://127.0.0.1:{gw_port}/files/read")
        assert resp.status_code == 200
        assert resp.content == b"ok"
        assert cp.lookups["sbx_1"] == 1
        assert calls == ["files/read"]
    finally:
        for s in (s, cp_server, gw_server):
            s.stop()


@pytest.mark.asyncio
async def test_mcp_session_id_header_forwarded() -> None:
    """MCP streamable HTTP needs Mcp-Session-Id on every request; the gateway
    must forward it (dropping it makes upstream tool calls fail)."""
    node_app = FastAPI()
    seen: dict[str, str] = {}

    @node_app.api_route("/mcp", methods=["GET", "POST"])
    async def node(request: Request) -> Response:
        seen["mcp-session-id"] = request.headers.get("mcp-session-id", "")
        return Response(status_code=200, content=b"ok")

    s_port, s_sock = _bind_low_port()
    s = _ServerThread(node_app, s_port, sock=s_sock)
    s.start()
    cp = _ControlPlane()
    cp.routes["sbx_1"] = [f"http://127.0.0.1:{s.server.config.port}"]
    cp_server, cp_port = cp.start()
    gw_server, gw_port = await _start_gateway(f"http://127.0.0.1:{cp_port}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"http://127.0.0.1:{gw_port}/mcp",
                headers={
                    "E2b-Sandbox-Id": "sbx_1",
                    "Mcp-Session-Id": "sess-abc",
                    "Authorization": "Bearer tok",
                },
            )
        assert resp.status_code == 200
        assert seen.get("mcp-session-id") == "sess-abc"
    finally:
        for srv in (s, cp_server, gw_server):
            srv.stop()


@pytest.mark.asyncio
async def test_invalidation_endpoint_drops_cache() -> None:
    node, _ = _make_node([200])
    s_port, s_sock = _bind_low_port()
    s = _ServerThread(node, s_port, sock=s_sock)
    s.start()
    cp = _ControlPlane()
    cp.routes["sbx_1"] = [f"http://127.0.0.1:{s.server.config.port}"]
    cp_server, cp_port = cp.start()
    gw_server, gw_port = await _start_gateway(f"http://127.0.0.1:{cp_port}")
    try:
        assert (await _get(f"http://127.0.0.1:{gw_port}/commands")).status_code == 200
        assert cp.lookups["sbx_1"] == 1
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"http://127.0.0.1:{gw_port}/internal/routes/sbx_1/invalidate",
                headers={"X-Internal-Key": "internal-key"},
            )
        assert resp.status_code == 204
        assert (await _get(f"http://127.0.0.1:{gw_port}/commands")).status_code == 200
        assert cp.lookups["sbx_1"] == 2
    finally:
        for s in (s, cp_server, gw_server):
            s.stop()


def test_route_subscriber_invalidates_on_redis_broadcast() -> None:
    """A published sandbox id drops the cached route on this replica."""
    import fakeredis

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    cache = RouteCache(ttl=30.0)
    cache.put("sbx_1", "http://node:49983")
    subscriber = RouteInvalidationSubscriber(cache, client=client)
    subscriber.start()
    try:
        client.publish(GATEWAY_ROUTE_INVALIDATE_CHANNEL, "sbx_1")
        deadline = time.time() + 5
        while cache.get("sbx_1") is not None and time.time() < deadline:
            time.sleep(0.05)
        assert cache.get("sbx_1") is None
    finally:
        subscriber.stop()


@pytest.mark.asyncio
async def test_control_plane_publishes_route_invalidation() -> None:
    """Migration/kill publishes the sandbox id to the invalidation channel."""
    import fakeredis

    from control_plane.api.sandboxes import _invalidate_gateway_route

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    pubsub = client.pubsub()
    pubsub.subscribe(GATEWAY_ROUTE_INVALIDATE_CHANNEL)
    time.sleep(0.1)

    class _Settings:
        gateway_url = None
        internal_api_key = "internal-key"

    class _State:
        settings = _Settings()
        redis_client = client

    class _App:
        state = _State()

    class _Request:
        app = _App()

    await _invalidate_gateway_route(_Request(), "sbx_9")

    deadline = time.time() + 5
    got = None
    while time.time() < deadline:
        message = pubsub.get_message(ignore_subscribe_messages=True)
        if message:
            got = message
            break
        time.sleep(0.05)
    assert got is not None
    assert got["data"] == "sbx_9"
