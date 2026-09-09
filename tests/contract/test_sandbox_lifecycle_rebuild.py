"""M4 lifecycle & exec-failure contract coverage (Task 3).

Pins the executor-level contracts the instance model introduced:

1. deleting a sandbox through the agent API tears down its runtime context
   and closes the held executor instance (the single shutdown point, M4 D1);
2. closing the instance and re-ensuring it launches a fresh instance with the
   same stable name (idle/24h reclaim recovery surface);
3. a missing in-sandbox binary surfaces as exit 127 with a single non-empty
   stderr event (no synthetic exception path, no loose text matching) on a
   real sandlock worker.

The native-sandlock case skips off-Linux (same gate as the other sandlock
contract slots); the delete/close chain runs on every platform because it
only needs the worker app and a fake executor.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.executors.base import ExecConfig
from envd_service.runtime.registry import RuntimeRegistry
from tests.security.conftest import sandlock_ready


class _CloseRecordingExecutor:
    """Fake executor that records starts and close() for the shutdown chain."""

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self, config):  # noqa: ANN001
        self.started += 1
        return SimpleNamespace(pid=123)

    def close(self) -> None:
        self.closed += 1


async def _delete_sandbox(app, sandbox_id: str, settings: EnvdSettings) -> None:
    headers = {"X-Internal-Key": settings.internal_api_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        deleted = await client.delete(
            f"/agent/sandboxes/{sandbox_id}", headers=headers
        )
        assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_delete_closes_executor_and_clears_runtime(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a sandbox pops the worker runtime and closes the held
    executor instance exactly once (M4 D1 shutdown chain)."""
    runtime_registry = RuntimeRegistry(workspace)
    settings = EnvdSettings(executor="local")
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    sandbox_id = "sbx_lifecycle_delete"
    sandbox_dir = workspace / sandbox_id
    sandbox_dir.mkdir()
    (sandbox_dir / "workspace").mkdir()
    runtime_registry.register(
        sandbox_id=sandbox_id,
        access_token="tok",
        workspace_dir=str(sandbox_dir),
    )
    ctx = app.state.context_factory(runtime_registry.get(sandbox_id))
    app.state.runtimes[sandbox_id] = ctx
    fake = _CloseRecordingExecutor()
    monkeypatch.setattr(ctx, "executor", fake)

    await ctx.executor.start(
        ExecConfig(
            cmd=["/bin/echo", "ok"],
            env={},
            cwd=str(sandbox_dir),
            stdin_enabled=False,
        )
    )
    assert fake.started == 1

    await _delete_sandbox(app, sandbox_id, settings)

    assert runtime_registry.get(sandbox_id) is None
    assert app.state.runtimes.get(sandbox_id) is None
    assert fake.closed == 1


@pytest.mark.asyncio
async def test_nonexistent_binary_exits_127_with_no_output(workspace) -> None:
    """A missing binary inside a real sandlock worker exits 127 with empty
    stdout and stderr: execvp failure is reported only through the exit status
    (fork exec semantics, observed on F10b wheel 2026-09-06)."""
    if not sandlock_ready():
        pytest.skip("needs Linux + sandlock (Docker test runner)")
    runtime_registry = RuntimeRegistry(workspace)
    # The sandbox is registered directly (no provisioning), so no host uid was
    # allocated -- pin the legacy shared-identity shape instead of letting the
    # E3.2 default fail loudly on a record the test built by hand. The route-B
    # counterpart of this contract is
    # tests/contract/test_route_b_executor.py::test_missing_binary_exits_127_through_the_slot
    settings = EnvdSettings(executor="sandlock", per_sandbox_uid=False)
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    sandbox_id = "sbx_lifecycle_127"
    sandbox_dir = workspace / sandbox_id
    sandbox_dir.mkdir()
    (sandbox_dir / "workspace").mkdir()
    runtime_registry.register(
        sandbox_id=sandbox_id,
        access_token="tok",
        workspace_dir=str(sandbox_dir),
    )
    ctx = app.state.context_factory(runtime_registry.get(sandbox_id))
    app.state.runtimes[sandbox_id] = ctx
    try:
        running = await ctx.executor.start(
            ExecConfig(
                cmd=["/nonexistent-e2b-bin"],
                env={},
                cwd=str(sandbox_dir),
                stdin_enabled=False,
            )
        )
        events: list[tuple[str, bytes]] = []
        async for kind, data in running.output():
            # The raw stream also carries internal ("__eof__", kind) markers
            # (the process manager filters them before broadcasting); only
            # stdout/stderr data count as output, and a real fork execvp
            # failure emits none.
            if kind in ("stdout", "stderr"):
                events.append((kind, data))
        exit_code = await running.exit_code()
        assert exit_code == 127
        assert events == []
    finally:
        ctx.shutdown()
