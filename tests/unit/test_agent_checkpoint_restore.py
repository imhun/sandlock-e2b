"""The agent endpoints of checkpoint/restore, and the pause/resume wiring (S2/S3).

Delivery contract first, like ``test_agent_pause_resume``: internal-key auth,
404 for a sandbox this worker does not own, and a ``captured: false`` /
``restored: false`` answer that carries a reason rather than an empty string.
The executor is faked because the engine's own half is pinned in the fork
(``test_restore`` / ``test_instance*``); what is asserted here is that the
worker routes a capture through the sandbox's slot, keeps the image in the
platform's own directory, and never lets a checkpoint problem change what
``pause`` does.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common.paths import sandbox_checkpoint_dir


class _RecordingExecutor:
    """The executor's checkpoint verbs, plus an ordered event log."""

    def __init__(
        self,
        *,
        live_session: bool = True,
        capture_reply: dict | None = None,
        restore_reply: dict | None = None,
        events: list[str],
    ) -> None:
        self.instance_handle = object() if live_session else None
        self._capture_reply = capture_reply
        self._restore_reply = restore_reply
        self._events = events
        self.captures: list[str] = []
        self.restores: list[str] = []

    def capture_checkpoint(self, dir: str, name: str | None = None) -> dict:
        self.captures.append(dir)
        self._events.append("executor.capture_checkpoint")
        if self._capture_reply is not None:
            return dict(self._capture_reply)
        image = Path(dir)
        image.mkdir(parents=True, exist_ok=True)
        (image / "parts.bin").write_bytes(b"c" * 4096)
        return {
            "captured": True,
            "reason": "",
            "dir": dir,
            "name": name,
            "pid": 4242,
            "fds": 3,
        }

    def restore_checkpoint(self, dir: str) -> dict:
        self.restores.append(dir)
        self._events.append("executor.restore_checkpoint")
        if self._restore_reply is not None:
            return dict(self._restore_reply)
        return {
            "restored": True,
            "reason": "",
            "dir": dir,
            "child_id": 7,
            "pid": 31337,
            "restore_skipped": [{"fd": 5, "path": "socket:[1]"}],
        }


class _RecordingContext:
    """The runtime context: it owns the executor and it is what freezes/thaws."""

    def __init__(self, executor, events: list[str]) -> None:
        self.executor = executor
        self._events = events
        self.paused = 0
        self.resumed = 0

    def pause(self) -> None:
        self.paused += 1
        self._events.append("ctx.pause")

    def resume(self) -> None:
        self.resumed += 1
        self._events.append("ctx.resume")

    def shutdown(self) -> None:
        pass


def _make_worker(workspace, *, pause_checkpoint: bool = False):
    runtime_registry = RuntimeRegistry(workspace)
    settings = EnvdSettings(executor="local", pause_checkpoint=pause_checkpoint)
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    return app, settings, runtime_registry


def _register_runtime(app, runtime_registry, sandbox_id: str, ctx=None) -> None:
    sandbox_dir = runtime_registry._workspace_base / sandbox_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "workspace").mkdir()
    runtime_registry.register(
        sandbox_id=sandbox_id,
        access_token="tok",
        workspace_dir=str(sandbox_dir),
    )
    if ctx is not None:
        app.state.runtimes[sandbox_id] = ctx


def _live_context(app, runtime_registry, sandbox_id: str, executor, events):
    ctx = _RecordingContext(executor, events)
    _register_runtime(app, runtime_registry, sandbox_id, ctx)
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


async def test_checkpoint_requires_the_internal_key(workspace) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    _register_runtime(app, runtime_registry, "sbx_ckpt_auth")

    anonymous = await _post(worker, "sbx_ckpt_auth", "checkpoint", None)
    wrong = await _post(worker, "sbx_ckpt_auth", "checkpoint", "not-the-key")

    assert anonymous.status_code == 401
    assert wrong.status_code == 401


async def test_checkpoint_of_a_sandbox_this_worker_does_not_own_is_404(
    workspace,
) -> None:
    worker = _make_worker(workspace)
    _app, settings, _registry = worker

    resp = await _post(worker, "sbx_ckpt_missing", "checkpoint", settings.internal_api_key)

    assert resp.status_code == 404


async def test_checkpoint_returns_the_image_and_the_platform_account(
    workspace,
) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(events=events)
    _live_context(app, runtime_registry, "sbx_ckpt_ok", executor, events)
    expected = sandbox_checkpoint_dir(workspace, "sbx_ckpt_ok") / "latest"

    resp = await _post(worker, "sbx_ckpt_ok", "checkpoint", settings.internal_api_key)

    assert resp.status_code == 200
    assert resp.json() == {
        "sandbox_id": "sbx_ckpt_ok",
        "captured": True,
        "reason": "",
        "image": str(expected),
        "imageMB": 0,
        "platformDiskUsedMB": 0,
        "platformDiskBudgetMB": 0,
        "pid": 4242,
        "fds": 3,
    }
    assert executor.captures == [str(expected)]
    assert (expected / "parts.bin").is_file()


async def test_checkpoint_without_a_live_session_says_so(workspace) -> None:
    """A registered sandbox with nothing launched has nothing to capture."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    _register_runtime(app, runtime_registry, "sbx_ckpt_nctx")

    resp = await _post(
        worker, "sbx_ckpt_nctx", "checkpoint", settings.internal_api_key
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "sandbox_id": "sbx_ckpt_nctx",
        "captured": False,
        "reason": "no live session on this worker to capture",
        "image": None,
        "imageMB": 0,
        "platformDiskUsedMB": 0,
        "platformDiskBudgetMB": 0,
    }


async def test_restore_returns_the_fds_that_could_not_come_back(workspace) -> None:
    """S4/D6: the reply carries the engine's skipped-fd list, verbatim."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(live_session=False, events=events)
    _live_context(app, runtime_registry, "sbx_restore", executor, events)
    image = sandbox_checkpoint_dir(workspace, "sbx_restore") / "latest"
    image.mkdir(parents=True)
    (image / "meta.json").write_text("{}", encoding="utf-8")

    resp = await _post(worker, "sbx_restore", "restore", settings.internal_api_key)

    assert resp.status_code == 200
    assert resp.json() == {
        "sandbox_id": "sbx_restore",
        "restored": True,
        "reason": "",
        "image": str(image),
        "consumed": True,
        "child_id": 7,
        "pid": 31337,
        "unrecoveredFds": [{"fd": 5, "path": "socket:[1]"}],
        "unrecoveredFdCount": 1,
    }
    assert executor.restores == [str(image)]
    assert not image.exists()


async def test_restore_without_an_image_keeps_the_endpoint_sayable(workspace) -> None:
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(live_session=False, events=events)
    _live_context(app, runtime_registry, "sbx_restore_none", executor, events)

    resp = await _post(
        worker, "sbx_restore_none", "restore", settings.internal_api_key
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "sandbox_id": "sbx_restore_none",
        "restored": False,
        "reason": "no checkpoint image for this sandbox",
        "image": None,
    }
    assert executor.restores == []


async def test_pause_with_the_flag_checkpoints_before_it_freezes(workspace) -> None:
    """S3's ordering, pinned: capture first, freeze after.

    The engine's capture stops the target child and resumes it when it is done,
    so a capture of an already SIGSTOPped sandbox would *undo* the pause. This
    is the one ordering the feature cannot get wrong, and the events make it a
    fact rather than a comment.
    """
    worker = _make_worker(workspace, pause_checkpoint=True)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(events=events)
    ctx = _live_context(app, runtime_registry, "sbx_pause_ckpt", executor, events)

    resp = await _post(
        worker, "sbx_pause_ckpt", "pause", settings.internal_api_key
    )

    assert resp.status_code == 204
    assert events == ["executor.capture_checkpoint", "ctx.pause"]
    assert ctx.paused == 1
    assert runtime_registry.get("sbx_pause_ckpt").state == "paused"
    assert (sandbox_checkpoint_dir(workspace, "sbx_pause_ckpt") / "latest").is_dir()


async def test_pause_without_the_flag_never_touches_the_checkpoint_verb(
    workspace,
) -> None:
    """D4: default off means pause is exactly what it was."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(events=events)
    ctx = _live_context(app, runtime_registry, "sbx_pause_plain", executor, events)

    resp = await _post(
        worker, "sbx_pause_plain", "pause", settings.internal_api_key
    )

    assert resp.status_code == 204
    assert events == ["ctx.pause"]
    assert ctx.paused == 1
    assert executor.captures == []
    assert not sandbox_checkpoint_dir(workspace, "sbx_pause_plain").exists()


async def test_pause_survives_a_slot_that_refuses_to_capture(workspace) -> None:
    """A refusal is not a failed pause: the sandbox is frozen in place."""
    worker = _make_worker(workspace, pause_checkpoint=True)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(
        events=events,
        capture_reply={
            "captured": False,
            "reason": "checkpoint requires exactly one live child, found 2",
        },
    )
    ctx = _live_context(app, runtime_registry, "sbx_pause_refused", executor, events)

    resp = await _post(
        worker, "sbx_pause_refused", "pause", settings.internal_api_key
    )

    assert resp.status_code == 204
    assert events == ["executor.capture_checkpoint", "ctx.pause"]
    assert ctx.paused == 1
    assert runtime_registry.get("sbx_pause_refused").state == "paused"
    assert not (sandbox_checkpoint_dir(workspace, "sbx_pause_refused") / "latest").exists()


async def test_resume_restores_the_image_when_the_session_is_gone(
    workspace,
) -> None:
    """D5: a resume on a worker with no session rebuilds it from the image."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(live_session=False, events=events)
    ctx = _live_context(app, runtime_registry, "sbx_resume_moved", executor, events)
    runtime_registry.set_state("sbx_resume_moved", "paused", None)
    # The pause itself is exercised above; this log is about the resume.
    events.clear()
    image = sandbox_checkpoint_dir(workspace, "sbx_resume_moved") / "latest"
    image.mkdir(parents=True)
    (image / "meta.json").write_text("{}", encoding="utf-8")

    resp = await _post(
        worker, "sbx_resume_moved", "resume", settings.internal_api_key
    )

    assert resp.status_code == 204
    assert events == ["executor.restore_checkpoint", "ctx.resume"]
    assert executor.restores == [str(image)]
    assert not image.exists()
    assert runtime_registry.get("sbx_resume_moved").state == "running"


async def test_resume_thaws_a_session_that_is_still_here(workspace) -> None:
    """The fast path: the session is on this worker, so nothing is rebuilt."""
    worker = _make_worker(workspace)
    app, settings, runtime_registry = worker
    events: list[str] = []
    executor = _RecordingExecutor(live_session=True, events=events)
    ctx = _live_context(app, runtime_registry, "sbx_resume_local", executor, events)
    runtime_registry.set_state("sbx_resume_local", "paused", None)
    events.clear()
    image = sandbox_checkpoint_dir(workspace, "sbx_resume_local") / "latest"
    image.mkdir(parents=True)

    resp = await _post(
        worker, "sbx_resume_local", "resume", settings.internal_api_key
    )

    assert resp.status_code == 204
    assert events == ["ctx.resume"]
    assert ctx.resumed == 1
    assert executor.restores == []
    assert not image.exists(), (
        "a stale image must not survive the sandbox running again, or the next "
        "resume would rewind it to an older process"
    )
