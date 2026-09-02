"""E6.3 contract: MCP gateway ports are released on sandbox delete and
reused by later sandboxes on the same worker."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry


class _FakeExecutor:
    def __init__(self) -> None:
        self.started: list = []

    async def start(self, config):  # noqa: ANN001
        self.started.append(config)
        return SimpleNamespace(pid=123)


@pytest.mark.asyncio
async def test_delete_releases_port_for_next_sandbox(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a sandbox through the agent API returns its MCP port to the
    pool; the next sandbox on the same worker reuses the exact same port."""
    import envd_service.runtime.context as context_mod

    pool = context_mod.McpPortPool()
    monkeypatch.setattr(context_mod, "_next_mcp_port", pool.allocate)
    monkeypatch.setattr(context_mod, "_release_mcp_port", pool.release)

    runtime_registry = RuntimeRegistry(workspace)
    settings = EnvdSettings(executor="local")
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    sandbox_dir = workspace / "sbx_mcp_contract"
    sandbox_dir.mkdir()
    (sandbox_dir / "workspace").mkdir()
    runtime_registry.register(
        sandbox_id="sbx_mcp_contract",
        access_token="tok",
        workspace_dir=str(sandbox_dir),
        mcp={"name": "echo", "command": "python3"},
    )
    ctx = app.state.context_factory(runtime_registry.get("sbx_mcp_contract"))
    app.state.runtimes["sbx_mcp_contract"] = ctx
    fake = _FakeExecutor()
    monkeypatch.setattr(ctx, "executor", fake)
    await ctx.start_mcp_gateway({"name": "echo"}, "tok")
    port = ctx.mcp_port
    assert port is not None

    headers = {"X-Internal-Key": settings.internal_api_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        deleted = await client.delete(
            "/agent/sandboxes/sbx_mcp_contract", headers=headers
        )
        assert deleted.status_code == 204

    # Unregister popped the runtime context and shutdown released the port.
    assert runtime_registry.get("sbx_mcp_contract") is None
    assert app.state.runtimes.get("sbx_mcp_contract") is None
    assert not sandbox_dir.exists()
    # The freed port is immediately reusable by the next sandbox.
    assert pool.allocate() == port
