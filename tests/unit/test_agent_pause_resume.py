"""G1a: worker agent pause/resume delivery routes (recording context).

The agent endpoints are the worker side of the control-plane pause/resume
push: internal-key auth (401), missing runtime (404), and freeze/thaw of a
live context (204). The context is a recording fake because the underlying
``ProcessManager`` semantics (idempotent SIGSTOP/SIGCONT of running children)
are covered by the real-sandlock contract; this file pins the delivery
contract itself.
"""

from __future__ import annotations

import httpx
import pytest

from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.checkpoint_store import checkpoint_image_dir
from envd_service.runtime.registry import RuntimeRegistry


class _RecordingContext:
    """Fake runtime context recording pause()/resume() calls."""

    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1


def _make_worker(workspace):
    runtime_registry = RuntimeRegistry(workspace)
    settings = EnvdSettings(executor="local")
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    return app, settings, runtime_registry


def _register_runtime(
    app,
    runtime_registry,
    sandbox_id: str,
    *,
    live_context: bool,
) -> _RecordingContext | None:
    sandbox_dir = runtime_registry._workspace_base / sandbox_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "workspace").mkdir()
    runtime_registry.register(
        sandbox_id=sandbox_id,
        access_token="tok",
        workspace_dir=str(sandbox_dir),
    )
    if not live_context:
        return None
    ctx = _RecordingContext()
    app.state.runtimes[sandbox_id] = ctx
    return ctx


async def _post(worker, sandbox_id: str, action: str, key: str | None):
    app, _settings, _registry = worker
    headers = {} if key is None else {"X-Internal-Key": key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            f"/agent/sandboxes/{sandbox_id}/{action}", headers=headers
        )


async def test_agent_pause_freezes_live_context_204(workspace) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_pause", live_context=True
    )

    resp = await _post(worker, "sbx_agent_pause", "pause", settings.internal_api_key)

    assert resp.status_code == 204
    assert ctx is not None
    assert ctx.paused == 1
    assert ctx.resumed == 0


async def test_a_refused_capture_removes_the_previous_image(
    workspace, monkeypatch
) -> None:
    """A pause that cannot take a new image must not leave the old one behind.

    A resume cannot tell the two apart, and the stale image describes an older
    process tree -- restoring it silently rewinds the sandbox. Measured
    2026-10-05 on the k0s acceptance: the second pause was refused (the platform
    account was unmeasurable), its image was never written, and the resume
    brought back the *first* ticker, so the counter the test watched never moved
    while the restored process kept writing the first one's file.
    """
    # The capture only runs when the deployment asked for it (the pause's
    # checkpoint hook), so the test has to turn the flag on.
    monkeypatch.setenv("E2B_PAUSE_CHECKPOINT", "1")
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    _register_runtime(app, runtime_registry, "sbx_stale", live_context=True)
    image = checkpoint_image_dir(workspace, "sbx_stale")
    image.mkdir(parents=True)
    (image / "meta.json").write_text("{}", encoding="utf-8")
    assert image.is_dir()

    resp = await _post(worker, "sbx_stale", "pause", settings.internal_api_key)

    assert resp.status_code == 204
    assert not image.exists(), (
        "a refused capture must not leave an older image for a resume to restore"
    )


async def test_agent_pause_is_idempotent_at_http_level(workspace) -> None:
    """Repeated agent pauses both return 204; the ctx-level freeze is
    idempotent (``ProcessManager.pause_all`` no-ops without running
    children)."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_pause2", live_context=True
    )

    first = await _post(worker, "sbx_agent_pause2", "pause", settings.internal_api_key)
    second = await _post(worker, "sbx_agent_pause2", "pause", settings.internal_api_key)

    assert first.status_code == 204
    assert second.status_code == 204
    assert ctx is not None
    assert ctx.paused == 2


async def test_agent_pause_without_live_context_is_204(workspace) -> None:
    """A registered runtime with nothing launched yet has nothing to freeze."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_pause_nctx", live_context=False
    )

    resp = await _post(
        worker, "sbx_agent_pause_nctx", "pause", settings.internal_api_key
    )

    assert resp.status_code == 204
    assert ctx is None


async def test_agent_resume_thaws_live_context_204(workspace) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_resume", live_context=True
    )

    resp = await _post(worker, "sbx_agent_resume", "resume", settings.internal_api_key)

    assert resp.status_code == 204
    assert ctx is not None
    assert ctx.resumed == 1
    assert ctx.paused == 0


async def test_agent_resume_without_live_context_is_204(workspace) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_resume_nctx", live_context=False
    )

    resp = await _post(
        worker, "sbx_agent_resume_nctx", "resume", settings.internal_api_key
    )

    assert resp.status_code == 204
    assert ctx is None


async def test_agent_pause_missing_runtime_returns_404(workspace) -> None:
    worker = _make_worker(workspace)
    _app, settings, _runtime_registry = worker

    resp = await _post(worker, "sbx_agent_unknown", "pause", settings.internal_api_key)

    assert resp.status_code == 404


async def test_agent_resume_missing_runtime_returns_404(workspace) -> None:
    worker = _make_worker(workspace)
    _app, settings, _runtime_registry = worker

    resp = await _post(worker, "sbx_agent_unknown", "resume", settings.internal_api_key)

    assert resp.status_code == 404


async def test_agent_pause_requires_internal_key(workspace) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_pause_401", live_context=True
    )

    missing = await _post(worker, "sbx_agent_pause_401", "pause", None)
    wrong = await _post(worker, "sbx_agent_pause_401", "pause", "not-the-key")

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ctx is not None
    assert ctx.paused == 0


async def test_agent_resume_requires_internal_key(workspace) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_resume_401", live_context=True
    )

    missing = await _post(worker, "sbx_agent_resume_401", "resume", None)
    wrong = await _post(worker, "sbx_agent_resume_401", "resume", "not-the-key")

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ctx is not None
    assert ctx.resumed == 0


async def test_agent_pause_ignores_json_body(workspace) -> None:
    """The agent pause route accepts and ignores a body (symmetry)."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    ctx = _register_runtime(
        app, runtime_registry, "sbx_agent_pause_body", live_context=True
    )
    headers = {"X-Internal-Key": settings.internal_api_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post(
            "/agent/sandboxes/sbx_agent_pause_body/pause",
            headers=headers,
            json={"ignored": True},
        )

    assert resp.status_code == 204
    assert ctx is not None
    assert ctx.paused == 1
