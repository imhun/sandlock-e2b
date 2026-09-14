"""MCP gateway unit tests: configurable port, envd start path, /mcp proxy,
auth primitives/middleware, port allocation and gateway fallback paths."""

from __future__ import annotations

import importlib
import json
import logging
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
from envd_service.runtime.context import (
    SandboxRuntimeContext,
    _MCP_PORT_BASE,
    _MCP_PORT_MAX,
    _watch_mcp_gateway_exit,
)
from envd_service.runtime.registry import RuntimeSandbox
from tests.conftest import _ServerThread, _bind_low_port


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


@pytest.fixture(autouse=True)
def _no_registry_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep these unit tests off the network.

    ``create_executor`` resolves ``base_image`` through the OCI distribution
    API before building the executor, and the fixture replaces the executor
    with a fake right afterwards. ``python-mcp:3.14`` is the project's own MCP
    base image (built by deploy/scripts/build-and-push.sh into the configured
    registry), so a bare-name lookup against Docker Hub can only fail -- the
    resolution is stubbed here, and the real OCI path has its own tests.
    """
    from envd_service.executors import factory

    rootfs = tmp_path / "stub-rootfs"
    rootfs.mkdir(exist_ok=True)
    monkeypatch.setattr(
        factory, "resolve_image_rootfs", lambda image, cache_dir, **kw: rootfs
    )


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

    # M4 D3: the MCP port is pre-allocated at context creation (record.mcp
    # present) and pushed into the executor as the instance bind ceiling;
    # start_mcp_gateway consumes it instead of allocating again.
    port_at_create = ctx.mcp_port
    assert port_at_create is not None
    # The pool's band is outside the kernel's ephemeral range and the harness
    # pool (tests/unit/test_mcp_port_pool.py pins the band itself).
    assert _MCP_PORT_BASE < port_at_create <= _MCP_PORT_MAX

    await ctx.start_mcp_gateway({"name": "echo", "command": "python3"}, "tok-123")

    assert ctx.mcp_port == port_at_create
    assert ctx.mcp_token == "tok-123"
    cfg = fake.started[0]
    # The gateway runs through the interpreter (sandlock chroot exec handler
    # only supports ELF binaries, not shebang scripts).
    assert cfg.cmd[0] == "/usr/local/bin/python3"
    assert cfg.cmd[1].endswith("mcp-gateway")
    assert cfg.env["GATEWAY_ACCESS_TOKEN"] == "tok-123"
    assert cfg.env["MCP_PORT"] == str(port_at_create)
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


def test_mcp_port_pool_reuses_freed_ports() -> None:
    """E6.3: freed ports are reused instead of the counter drifting upward."""
    from envd_service.runtime.context import _MCP_PORT_BASE, McpPortPool

    pool = McpPortPool()
    first = pool.allocate()
    second = pool.allocate()
    assert first == _MCP_PORT_BASE + 1
    assert second == first + 1

    pool.release(first)
    assert pool.allocate() == first  # freed port is reused
    pool.release(second)
    pool.release(second)  # duplicate release is idempotent
    assert pool.allocate() == second

    # Out-of-range releases (never allocated / above the watermark) are
    # ignored and cannot corrupt the pool.
    pool.release(_MCP_PORT_BASE)
    pool.release(_MCP_PORT_BASE + 1000)
    # Both freed ports were already reused above; the counter continues.
    assert pool.allocate() == _MCP_PORT_BASE + 3


def test_mcp_port_pool_concurrent_allocations_never_conflict() -> None:
    """E6.3: concurrent create/delete cannot hand out the same port twice."""
    from concurrent.futures import ThreadPoolExecutor

    from envd_service.runtime.context import _MCP_PORT_BASE, McpPortPool

    pool = McpPortPool()
    with ThreadPoolExecutor(max_workers=8) as exec:
        ports = list(exec.map(lambda _: pool.allocate(), range(64)))
    assert len(set(ports)) == 64
    assert all(p >= _MCP_PORT_BASE + 1 for p in ports)

    # Release everything, then re-allocate under the same contention: the
    # pool reuses the freed set and still never duplicates.
    with ThreadPoolExecutor(max_workers=8) as exec:
        list(exec.map(pool.release, ports))
    with ThreadPoolExecutor(max_workers=8) as exec:
        reused = list(exec.map(lambda _: pool.allocate(), range(64)))
    assert set(reused) == set(ports)


@pytest.mark.asyncio
async def test_shutdown_releases_mcp_port_for_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E6.3: deleting a sandbox returns its MCP port; the next sandbox gets
    the same port again."""
    import envd_service.runtime.context as context_mod

    pool = context_mod.McpPortPool()
    monkeypatch.setattr(context_mod, "_next_mcp_port", pool.allocate)
    monkeypatch.setattr(context_mod, "_release_mcp_port", pool.release)
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp_reuse_1",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp={"name": "echo", "command": "python3"},
    )
    ctx = SandboxRuntimeContext(record, Settings(executor="local"))
    monkeypatch.setattr(ctx, "executor", _FakeExecutor())
    await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    port = ctx.mcp_port
    assert port is not None
    ctx.shutdown()
    assert ctx.mcp_port is None

    ctx2 = SandboxRuntimeContext(record, Settings(executor="local"))
    monkeypatch.setattr(ctx2, "executor", _FakeExecutor())
    await ctx2.start_mcp_gateway({"name": "echo"}, "tok2")
    assert ctx2.mcp_port == port


@pytest.mark.asyncio
async def test_gateway_start_failure_keeps_port_for_retry_until_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 D3: a failed gateway start keeps the allocated port pinned to the
    sandbox (it is the instance bind ceiling), so a retry reuses the same
    port; shutdown returns it to the pool."""
    import envd_service.runtime.context as context_mod

    pool = context_mod.McpPortPool()
    allocated: list[int] = []
    original_allocate = pool.allocate

    def _alloc():
        port = original_allocate()
        allocated.append(port)
        return port

    monkeypatch.setattr(context_mod, "_next_mcp_port", _alloc)
    monkeypatch.setattr(context_mod, "_release_mcp_port", pool.release)
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp_fail",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp={"name": "echo", "command": "python3"},
    )
    ctx = SandboxRuntimeContext(record, Settings(executor="local"))

    class _FailExecutor:
        async def start(self, config):  # noqa: ANN001
            raise RuntimeError("gateway start failed")

    monkeypatch.setattr(ctx, "executor", _FailExecutor())
    with pytest.raises(RuntimeError, match="gateway start failed"):
        await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    # The port stays allocated to this sandbox (no second allocation).
    assert ctx.mcp_port == allocated[0]
    assert ctx.mcp_token is None
    assert len(allocated) == 1
    # A retry goes through the same path and reuses the same port.
    with pytest.raises(RuntimeError, match="gateway start failed"):
        await ctx.start_mcp_gateway({"name": "echo"}, "tok2")
    assert ctx.mcp_port == allocated[0]
    assert len(allocated) == 1
    # While the sandbox lives the port is not reusable elsewhere...
    assert pool.allocate() != allocated[0]
    # ...shutdown returns it, and the next allocation reuses it.
    ctx.shutdown()
    assert ctx.mcp_port is None
    assert pool.allocate() == allocated[0]


@pytest.mark.asyncio
async def test_gateway_start_failure_logs_error_with_port_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """D10: an executor.start failure in the MCP gateway path logs ERROR with
    sandbox_id/port and the exception text (logging only; the SDK contract --
    port pinned for retry, token cleared -- is unchanged)."""
    import envd_service.runtime.context as context_mod

    pool = context_mod.McpPortPool()
    monkeypatch.setattr(context_mod, "_next_mcp_port", pool.allocate)
    monkeypatch.setattr(context_mod, "_release_mcp_port", pool.release)
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp_fail",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp={"name": "echo", "command": "python3"},
    )
    ctx = SandboxRuntimeContext(record, Settings(executor="local"))

    class _FailExecutor:
        async def start(self, config):  # noqa: ANN001
            raise RuntimeError("gateway start failed")

    monkeypatch.setattr(ctx, "executor", _FailExecutor())
    with caplog.at_level(
        logging.ERROR, logger="envd_service.runtime.context"
    ):
        with pytest.raises(RuntimeError, match="gateway start failed"):
            await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    # Port stays pinned to the sandbox (first port of the pool's band).
    assert ctx.mcp_port == _MCP_PORT_BASE + 1
    assert [r.message for r in caplog.records] == [
        "MCP gateway start failed sandbox_id=sbx_mcp_fail "
        f"port={_MCP_PORT_BASE + 1} error_type=RuntimeError error=gateway start failed"
    ]


@pytest.mark.asyncio
async def test_mcp_gateway_early_exit_logs_error_with_stderr_text(
    caplog,
) -> None:
    """D10/Task 10: a gateway process that terminates non-zero (the failure
    mode that is silent to the SDK) logs ERROR with port/sandbox_id and its
    stderr/exit text."""

    class _EarlyExitGateway:
        async def output(self):
            yield ("stdout", b"ignored")
            yield ("stderr", b"gateway boom\n")

        async def exit_code(self) -> int:
            return 2

    with caplog.at_level(
        logging.ERROR, logger="envd_service.runtime.context"
    ):
        await _watch_mcp_gateway_exit(
            _EarlyExitGateway(), sandbox_id="sbx_gw", port=51001
        )
    assert [r.message for r in caplog.records] == [
        "MCP gateway exited sandbox_id=sbx_gw port=51001 exit_code=2 "
        "stderr='gateway boom\\n'"
    ]


class _EarlyExitGateway:
    """Gateway process stub that drains to a non-zero exit with stderr."""

    async def output(self):
        yield ("stdout", b"ignored")
        yield ("stderr", b"gateway boom\n")

    async def exit_code(self) -> int:
        return 2


@pytest.mark.asyncio
async def test_gateway_early_exit_is_recorded_for_the_command_path(caplog) -> None:
    """FUP #4/Task D1: the watcher hands the death to its recorder as one
    typed record whose text is exactly what it logs, so the command path can
    replay it verbatim (logging alone was the pre-FUP#4 behaviour)."""
    from envd_service.runtime.context import McpGatewayFailure

    recorded: list = []
    with caplog.at_level(logging.ERROR, logger="envd_service.runtime.context"):
        await _watch_mcp_gateway_exit(
            _EarlyExitGateway(),
            sandbox_id="sbx_gw",
            port=51001,
            on_failure=recorded.append,
        )
    assert recorded == [
        McpGatewayFailure(
            text=(
                "mcp gateway failed to start sandbox_id=sbx_gw port=51001 "
                "exit_code=2 stderr='gateway boom\\n'"
            ),
            exit_code=2,
        )
    ]
    # ...and the pre-existing ERROR log line is unchanged.
    assert [r.message for r in caplog.records] == [
        "MCP gateway exited sandbox_id=sbx_gw port=51001 exit_code=2 "
        "stderr='gateway boom\\n'"
    ]


class _FailedGatewayCtx:
    """Runtime context whose gateway died; touching ``processes`` fails.

    The whole point of the FUP #4 surface is that the command fails *before*
    it execs anything, so this stub turns any exec attempt into an error
    instead of a silently-skipped collaborator.
    """

    _network = None

    def __init__(self, failure) -> None:  # noqa: ANN001
        self.mcp_gateway_failure = failure

    @property
    def processes(self):  # noqa: ANN201
        raise AssertionError("a command must not exec after a gateway failure")


@pytest.mark.asyncio
async def test_command_after_gateway_failure_returns_the_recorded_reason() -> None:
    """FUP #4/Task D1: the next command's whole stderr is the recorded text
    verbatim and its exit code is the gateway's own non-zero code."""
    import base64

    import envd_service.rpc as rpc
    from envd_service.runtime.context import McpGatewayFailure

    failure = McpGatewayFailure(
        text=(
            "mcp gateway failed to start sandbox_id=sbx_gw port=51001 "
            "exit_code=2 stderr='gateway boom\\n'"
        ),
        exit_code=2,
    )
    sandbox_id = "sbx_gw"
    ctx = _FailedGatewayCtx(failure)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                runtimes={sandbox_id: ctx},
                context_factory=None,
            )
        )
    )
    runtime = SimpleNamespace(
        sandbox_id=sandbox_id,
        network=None,
        workspace_dir="/tmp/sbx_gw_workspace",
        env_vars={},
    )
    _unary, stream = rpc.build_process_handlers()
    generator = await stream["process.Process/Start"](
        request, {"process": {"cmd": "echo", "args": ["hi"]}}, runtime
    )
    events = [event async for event in generator]
    start_pid = events[0]["event"]["start"]["pid"]
    assert events == [
        {"event": {"start": {"pid": start_pid}}},
        {
            "event": {
                "data": {
                    "stderr": base64.b64encode(
                        (failure.text + "\n").encode("utf-8")
                    ).decode("ascii")
                }
            }
        },
        {
            "event": {
                "end": {
                    "exitCode": 2,
                    "exited": True,
                    "status": "exited",
                    "error": None,
                }
            }
        },
    ]

@pytest.mark.asyncio
async def test_gateway_late_start_with_existing_instance_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 D3: a gateway start after the instance exists (no bind allowance in
    the fixed ceiling) must fail loudly instead of allocating a port the exec
    can never bind."""
    import envd_service.runtime.context as context_mod

    pool = context_mod.McpPortPool()
    allocated: list[int] = []
    original_allocate = pool.allocate

    def _alloc():
        port = original_allocate()
        allocated.append(port)
        return port

    monkeypatch.setattr(context_mod, "_next_mcp_port", _alloc)
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp_late",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp=None,
    )
    ctx = SandboxRuntimeContext(record, Settings(executor="local"))

    class _FixedCeilingExecutor:
        instance_handle = object()

        async def start(self, config):  # noqa: ANN001
            raise AssertionError("start must not be reached")

    monkeypatch.setattr(ctx, "executor", _FixedCeilingExecutor())
    with pytest.raises(RuntimeError, match="ceiling cannot widen"):
        await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    assert ctx.mcp_port is None
    assert allocated == []


@pytest.mark.asyncio
async def test_gateway_late_start_before_instance_allocates_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 D3 defensive path: no record.mcp and no instance yet -- the first
    gateway start allocates the port, and shutdown returns it."""
    import envd_service.runtime.context as context_mod

    pool = context_mod.McpPortPool()
    monkeypatch.setattr(context_mod, "_next_mcp_port", pool.allocate)
    monkeypatch.setattr(context_mod, "_release_mcp_port", pool.release)
    record = RuntimeSandbox(
        sandbox_id="sbx_mcp_late_ok",
        access_token="at",
        workspace_dir=str(tmp_path),
        base_image="python-mcp:3.14",
        mcp=None,
    )
    ctx = SandboxRuntimeContext(record, Settings(executor="local"))
    monkeypatch.setattr(ctx, "executor", _FakeExecutor())

    await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    port = ctx.mcp_port
    assert port is not None
    ctx.shutdown()
    assert ctx.mcp_port is None
    assert pool.allocate() == port


class _ActivityRegistry:
    """Stub for the E9.1 activity mark that ``/mcp`` performs.

    The real end-to-end marking (worker heartbeat -> control plane) is covered
    by ``tests/contract/test_idle_activity.py``; here we only need the route's
    collaborator so it can be asserted: marked on an authenticated request,
    untouched on a rejected one.
    """

    def __init__(self) -> None:
        self.marked: list[str] = []

    def mark_active(self, sandbox_id: str) -> None:
        self.marked.append(sandbox_id)


def _proxy_app(runtimes: dict) -> FastAPI:
    app = FastAPI()
    app.state.runtimes = runtimes
    app.state.runtime_registry = _ActivityRegistry()
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

    port, sock = _bind_low_port()
    server = _ServerThread(upstream, port, sock=sock)
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
            # An authenticated MCP call is activity (E9.1): the route
            # authenticates inline and must mark the sandbox itself.
            assert app.state.runtime_registry.marked == ["sbx_1"]
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
        # Nothing above was authenticated -> nothing was marked active.
        assert app.state.runtime_registry.marked == []


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

    port, sock = _bind_low_port()
    server = _ServerThread(upstream, port, sock=sock)
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

    port, sock = _bind_low_port()
    server = _ServerThread(upstream, port, sock=sock)
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

    port, sock = _bind_low_port()
    server = _ServerThread(upstream, port, sock=sock)
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
