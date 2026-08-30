"""MCP gateway unit tests: configurable port, envd start path, /mcp proxy,
auth primitives/middleware, port allocation and gateway fallback paths."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import envd_service.mcp.gateway as gw
from envd_service.config import Settings
from envd_service.http.auth import HttpAuthError, http_error_response
from envd_service.http.mcp import router as mcp_router
from envd_service.runtime.context import SandboxRuntimeContext
from envd_service.runtime.registry import RuntimeSandbox
from tests.conftest import _ServerThread, _free_port


def test_gateway_port_default() -> None:
    assert gw.PORT == 50005


def test_gateway_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PORT", "51234")
    importlib.reload(gw)
    assert gw.PORT == 51234
    monkeypatch.delenv("MCP_PORT")
    importlib.reload(gw)
    assert gw.PORT == 50005


def test_authorized_empty_token_allows_everything() -> None:
    scope = {"headers": [(b"authorization", b"Bearer anything")]}
    assert gw._authorized(scope, "") is True


def test_authorized_bearer_matches() -> None:
    scope = {"headers": [(b"authorization", b"Bearer tok")]}
    assert gw._authorized(scope, "tok") is True


def test_authorized_x_mcp_access_token_matches() -> None:
    scope = {"headers": [(b"x-mcp-access-token", b"tok")]}
    assert gw._authorized(scope, "tok") is True


def test_authorized_header_names_case_insensitive() -> None:
    scope = {"headers": [(b"Authorization", b"Bearer tok")]}
    assert gw._authorized(scope, "tok") is True
    scope = {"headers": [(b"X-Mcp-Access-Token", b"tok")]}
    assert gw._authorized(scope, "tok") is True


def test_authorized_wrong_token_rejected() -> None:
    scope = {"headers": [(b"authorization", b"Bearer nope")]}
    assert gw._authorized(scope, "tok") is False
    scope = {"headers": [(b"x-mcp-access-token", b"nope")]}
    assert gw._authorized(scope, "tok") is False


def _auth_wrapped_app(token: str) -> gw.AuthMiddleware:
    async def inner(scope, receive, send) -> None:  # noqa: ANN001
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)

    return gw.AuthMiddleware(inner, token)


@pytest.mark.asyncio
async def test_auth_middleware_rejects_unauthorized() -> None:
    app = _auth_wrapped_app("tok")
    async with _client(app) as c:
        resp = await c.get("/mcp")
        assert resp.status_code == 401
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["error"]["code"] == -32001


@pytest.mark.asyncio
async def test_auth_middleware_passes_authorized() -> None:
    app = _auth_wrapped_app("tok")
    async with _client(app) as c:
        resp = await c.get("/mcp", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


class _FakeExecutor:
    def __init__(self) -> None:
        self.started: list = []

    async def start(self, config):  # noqa: ANN001
        self.started.append(config)
        return SimpleNamespace(pid=123)


@pytest.mark.asyncio
async def test_start_mcp_gateway_allocates_port_writes_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp={"name": "echo", "command": "python3", "args": ["-c", "x"]},
    )
    ctx = SandboxRuntimeContext(record, Settings())
    fake = _FakeExecutor()
    monkeypatch.setattr(ctx, "executor", fake)

    await ctx.start_mcp_gateway({"name": "echo", "command": "python3"}, "tok-123")

    assert ctx.mcp_port is not None and ctx.mcp_port >= 51000
    assert ctx.mcp_token == "tok-123"
    cfg = fake.started[0]
    # The gateway runs through the interpreter (sandlock chroot exec handler
    # only supports ELF binaries, not shebang scripts).
    assert cfg.cmd[0] == "/usr/local/bin/python3"
    assert cfg.cmd[1].endswith("mcp-gateway")
    assert cfg.env["GATEWAY_ACCESS_TOKEN"] == "tok-123"
    assert cfg.env["MCP_PORT"] == str(ctx.mcp_port)
    assert cfg.env["PATH"].startswith("/usr/local/bin")
    # The SDK reads the token via the files API at /etc/mcp-gateway/.token
    # (resolved under the workspace).
    token_file = tmp_path / "etc" / "mcp-gateway" / ".token"
    assert token_file.read_text(encoding="utf-8") == "tok-123"


@pytest.mark.asyncio
async def test_start_mcp_gateway_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp_idem",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp={"name": "echo", "command": "python3"},
    )
    ctx = SandboxRuntimeContext(record, Settings())
    fake = _FakeExecutor()
    monkeypatch.setattr(ctx, "executor", fake)

    first = await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    second = await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    assert first is second
    assert len(fake.started) == 1


@pytest.mark.asyncio
async def test_start_mcp_gateway_primary_bin_and_full_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preferred /usr/bin/mcp-gateway is used when present; the ExecConfig
    carries cwd, disabled stdin, the JSON config and the MCP port env."""
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/usr/bin/mcp-gateway")
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp_bin",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp={"name": "echo", "command": "python3"},
    )
    ctx = SandboxRuntimeContext(record, Settings())
    fake = _FakeExecutor()
    monkeypatch.setattr(ctx, "executor", fake)

    await ctx.start_mcp_gateway({"name": "echo", "args": ["-c", "x"]}, "tok")
    cfg = fake.started[0]
    assert cfg.cmd[0] == "/usr/local/bin/python3"
    assert cfg.cmd[1] == "/usr/bin/mcp-gateway"
    assert cfg.cmd[2:] == [
        "--config",
        json.dumps({"name": "echo", "args": ["-c", "x"]}, separators=(",", ":")),
        "--foreground",
    ]
    assert cfg.cwd == str(tmp_path)
    assert cfg.stdin_enabled is False
    assert cfg.env["GATEWAY_ACCESS_TOKEN"] == "tok"
    assert cfg.env["MCP_PORT"] == str(ctx.mcp_port)
    assert cfg.env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_next_mcp_port_monotonic() -> None:
    from envd_service.runtime.context import _MCP_PORT_BASE, _next_mcp_port

    a = _next_mcp_port()
    b = _next_mcp_port()
    assert a >= _MCP_PORT_BASE + 1
    assert b == a + 1


def test_next_mcp_port_thread_safe() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from envd_service.runtime.context import _next_mcp_port

    with ThreadPoolExecutor(max_workers=8) as pool:
        ports = list(pool.map(lambda _: _next_mcp_port(), range(64)))
    assert len(set(ports)) == 64


def _proxy_app(runtimes: dict) -> FastAPI:
    app = FastAPI()
    app.state.runtimes = runtimes
    app.add_exception_handler(HttpAuthError, http_error_response)
    app.include_router(mcp_router)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_mcp_proxy_routes_to_sandbox_gateway() -> None:
    upstream = FastAPI()

    @upstream.api_route("/mcp", methods=["GET", "POST"])
    async def up(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "method": request.method})

    server = _ServerThread(upstream, _free_port())
    server.start()
    app = _proxy_app(
        {"sbx_1": SimpleNamespace(mcp_port=server.server.config.port, mcp_token="tok")}
    )
    try:
        async with _client(app) as c:
            resp = await c.get(
                "/mcp",
                headers={"E2b-Sandbox-Id": "sbx_1", "Authorization": "Bearer tok"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "method": "GET"}
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_mcp_proxy_auth_and_lookup() -> None:
    app = _proxy_app(
        {"sbx_1": SimpleNamespace(mcp_port=1, mcp_token="tok")}
    )
    async with _client(app) as c:
        # Missing sandbox id -> 401.
        resp = await c.get("/mcp")
        assert resp.status_code == 401
        # Unknown sandbox -> 404.
        resp = await c.get(
            "/mcp",
            headers={"E2b-Sandbox-Id": "sbx_missing", "Authorization": "Bearer tok"},
        )
        assert resp.status_code == 404
        # Sandbox without MCP -> 404.
        resp = await c.get(
            "/mcp",
            headers={"E2b-Sandbox-Id": "sbx_2", "Authorization": "Bearer tok"},
        )
        assert resp.status_code == 404
        # Wrong token -> 401.
        resp = await c.get(
            "/mcp",
            headers={"E2b-Sandbox-Id": "sbx_1", "Authorization": "Bearer nope"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_proxy_sandbox_without_mcp_returns_404() -> None:
    """A known sandbox that never started its gateway -> 404, not 500."""
    app = _proxy_app({"sbx_2": SimpleNamespace(mcp_port=None, mcp_token=None)})
    async with _client(app) as c:
        resp = await c.get(
            "/mcp",
            headers={"E2b-Sandbox-Id": "sbx_2", "Authorization": "Bearer tok"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mcp_proxy_post_body_path_query_and_delete() -> None:
    """POST body, sub-path and query string reach the sandbox gateway; the
    DELETE method (MCP session teardown) is proxied too."""
    upstream = FastAPI()

    @upstream.api_route("/mcp/{path:path}", methods=["GET", "POST", "DELETE"])
    async def up(path: str, request: Request) -> JSONResponse:
        body = await request.body()
        return JSONResponse(
            {
                "ok": True,
                "path": path,
                "method": request.method,
                "query": dict(request.query_params),
                "body": body.decode(),
            }
        )

    server = _ServerThread(upstream, _free_port())
    server.start()
    app = _proxy_app(
        {"sbx_1": SimpleNamespace(mcp_port=server.server.config.port, mcp_token="tok")}
    )
    try:
        async with _client(app) as c:
            resp = await c.post(
                "/mcp/session",
                params={"v": "1"},
                headers={
                    "E2b-Sandbox-Id": "sbx_1",
                    "Authorization": "Bearer tok",
                    "Content-Type": "application/json",
                },
                content='{"jsonrpc":"2.0","id":1}',
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["method"] == "POST"
            assert data["path"] == "session"
            assert data["query"] == {"v": "1"}
            assert data["body"] == '{"jsonrpc":"2.0","id":1}'

            resp = await c.delete(
                "/mcp/session/sess-abc",
                headers={"E2b-Sandbox-Id": "sbx_1", "Authorization": "Bearer tok"},
            )
            assert resp.status_code == 200
            assert resp.json()["method"] == "DELETE"
            assert resp.json()["path"] == "session/sess-abc"
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_mcp_proxy_accepts_x_mcp_access_token() -> None:
    upstream = FastAPI()

    @upstream.get("/mcp")
    async def up() -> JSONResponse:
        return JSONResponse({"ok": True})

    server = _ServerThread(upstream, _free_port())
    server.start()
    app = _proxy_app(
        {"sbx_1": SimpleNamespace(mcp_port=server.server.config.port, mcp_token="tok")}
    )
    try:
        async with _client(app) as c:
            resp = await c.get(
                "/mcp",
                headers={"E2b-Sandbox-Id": "sbx_1", "x-mcp-access-token": "tok"},
            )
            assert resp.status_code == 200
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_mcp_proxy_strips_transfer_headers() -> None:
    """Hop-by-hop headers are stripped before the response reaches the client;
    entity headers from the upstream gateway are preserved."""
    upstream = FastAPI()

    @upstream.get("/mcp")
    async def up() -> JSONResponse:
        return JSONResponse(
            {"ok": True},
            headers={
                "Transfer-Encoding": "chunked",
                "Connection": "keep-alive",
                "X-Mcp-Custom": "kept",
            },
        )

    server = _ServerThread(upstream, _free_port())
    server.start()
    app = _proxy_app(
        {"sbx_1": SimpleNamespace(mcp_port=server.server.config.port, mcp_token="tok")}
    )
    try:
        async with _client(app) as c:
            resp = await c.get(
                "/mcp",
                headers={"E2b-Sandbox-Id": "sbx_1", "Authorization": "Bearer tok"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            assert "transfer-encoding" not in resp.headers
            assert "connection" not in resp.headers
            assert "content-length" not in resp.headers
            assert resp.headers["x-mcp-custom"] == "kept"
    finally:
        server.stop()
