"""Task 8: the runtime record's write leaves the create's response path (P2b).

``write_json_atomically`` on the deployment's NAS is a mkdir, a write, an
``fsync`` and a rename -- measured 2026-10-01 at **47 ms**, the largest single
piece of the worker's own create work. It is also the last thing a create does,
so it sits squarely in the latency the caller feels.

What makes moving it safe is the marker from the previous task: the record is
still "what says the create finished", so a teardown that arrives in between
waits for the marker, and the marker does not come off until the record is
durable. The record itself is in memory from ``register`` on, so the RPC path
sees the sandbox immediately -- the disk copy is for *other processes*.

The failure mode this must not have: a create that answers 201, never lands a
record, and leaves nothing behind that says so. Hence "a failed persist keeps
the marker and is named": the disk then says "a create was in flight and did
not finish", which is exactly what the orphan path reclaims.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
import pytest

from envd_service import agent as agent_module
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime import registry as registry_module
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common import paths

SANDBOX = "sbx_deferred"
KEY = "internal-key"
SLOW_WRITE_S = 0.2


def _worker(workspace: Path):
    workspace_base = workspace / "workspaces"
    state_base = workspace / "state"
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace_base,
        state_base=state_base,
        shared_volume_root=None,
        internal_api_key=KEY,
    )
    registry = RuntimeRegistry(workspace_base, state_base=state_base)
    app = create_envd_app(
        settings=settings, runtime_registry=registry, workspace_base=workspace_base
    )
    return app, settings, registry


async def _create(app) -> tuple[httpx.Response, float]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        started = time.monotonic()
        resp = await client.post(
            "/agent/sandboxes",
            json={"sandboxID": SANDBOX},
            headers={"X-Internal-Key": KEY},
        )
        return resp, time.monotonic() - started


def _record_path(settings, sandbox_id: str = SANDBOX) -> Path:
    return Path(settings.state_base) / "_runtime" / sandbox_id / "sandbox.json"


def _marker(settings, sandbox_id: str = SANDBOX) -> Path:
    return paths.sandbox_creating_marker(
        settings.workspace_base, sandbox_id, state_base=settings.state_base
    )


async def _await_record(settings, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _record_path(settings).is_file():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_the_create_response_does_not_wait_for_the_record_write(
    workspace, monkeypatch
) -> None:
    app, settings, _registry = _worker(workspace)
    real_write = registry_module.write_json_atomically

    def slow_write(path, payload):
        time.sleep(SLOW_WRITE_S)
        return real_write(path, payload)

    monkeypatch.setattr(registry_module, "write_json_atomically", slow_write)

    resp, elapsed = await _create(app)

    assert resp.status_code == 201
    # The write takes 200 ms; a create that waited for it cannot answer this
    # fast. The bound leaves room for the machine, not for the write.
    assert elapsed < 0.1
    # ...and the record does land, so the create is not merely fast.
    assert await _await_record(settings) is True


@pytest.mark.asyncio
async def test_the_marker_stays_until_the_record_is_durable(
    workspace, monkeypatch
) -> None:
    app, settings, _registry = _worker(workspace)
    real_write = registry_module.write_json_atomically

    def slow_write(path, payload):
        time.sleep(SLOW_WRITE_S)
        return real_write(path, payload)

    monkeypatch.setattr(registry_module, "write_json_atomically", slow_write)

    resp, _elapsed = await _create(app)

    assert resp.status_code == 201
    # Answered, record not yet on the disk: the marker is the only thing that
    # says "a create is still finishing here".
    assert _record_path(settings).exists() is False
    assert _marker(settings).is_file() is True
    assert await _await_record(settings) is True
    assert _marker(settings).exists() is False


@pytest.mark.asyncio
async def test_the_rpc_path_sees_the_record_before_it_is_durable(
    workspace, monkeypatch
) -> None:
    app, settings, registry = _worker(workspace)
    real_write = registry_module.write_json_atomically

    def slow_write(path, payload):
        time.sleep(SLOW_WRITE_S)
        return real_write(path, payload)

    monkeypatch.setattr(registry_module, "write_json_atomically", slow_write)

    resp, _elapsed = await _create(app)

    assert resp.status_code == 201
    assert _record_path(settings).exists() is False
    record = registry.get(SANDBOX)
    assert record is not None
    assert record.sandbox_id == SANDBOX


@pytest.mark.asyncio
async def test_a_failed_persist_is_named_and_keeps_the_marker(
    workspace, monkeypatch, caplog
) -> None:
    """Answering 201 and losing the record silently is the failure to avoid."""
    app, settings, registry = _worker(workspace)

    def failing_write(path, payload):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(registry_module, "write_json_atomically", failing_write)

    with caplog.at_level(logging.WARNING, logger=registry_module.logger.name):
        resp, _elapsed = await _create(app)
        # Let the deferred task run to completion (and fail).
        await asyncio.sleep(0.2)

    assert resp.status_code == 201
    assert _record_path(settings).exists() is False
    assert _marker(settings).is_file() is True
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == registry_module.logger.name
    ] == [
        f"could not persist the runtime record for {SANDBOX}: [Errno 28] No "
        "space left on device: the sandbox is registered in memory only, and "
        "its create marker stays until the record is durable"
    ]
    # A retry answers the same way: ``False``, never an exception, and it still
    # writes nothing.
    assert registry.persist(SANDBOX) is False
    assert _marker(settings).is_file() is True


def test_persist_of_an_unknown_sandbox_is_false(workspace) -> None:
    """Nothing to write is not a success -- the caller must be able to tell."""
    _app, _settings, registry = _worker(workspace)
    assert registry.persist("sbx_nobody") is False
