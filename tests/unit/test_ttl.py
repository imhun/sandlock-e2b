"""TTL set / renew / reap."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.ttl import TTLSweeper


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=10,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry, timeout=300):
    return registry.create(
        template_id="base",
        timeout=timeout,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )


def test_set_timeout_refreshes():
    registry = SandboxRegistry(_settings())
    record = _create(registry, timeout=300)
    registry.set_timeout(record.sandbox_id, 600)
    assert (record.end_at - record.started_at).total_seconds() >= 599


def test_expired_sandbox_is_removed():
    registry = SandboxRegistry(_settings())
    record = _create(registry, timeout=300)
    record.end_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    expired = registry.remove_expired()
    assert [r.sandbox_id for r in expired] == [record.sandbox_id]
    assert registry.count() == 0


@pytest.mark.asyncio
async def test_ttl_sweeper_reaps():
    registry = SandboxRegistry(_settings())
    record = _create(registry, timeout=300)
    record.end_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    reaped = []
    sweeper = TTLSweeper(interval_seconds=0.05, on_expired=lambda r: reaped.append(r))
    sweeper.start(registry)
    try:
        for _ in range(50):
            if reaped:
                break
            await asyncio.sleep(0.05)
    finally:
        await sweeper.stop()
    assert [r.sandbox_id for r in reaped] == [record.sandbox_id]


async def test_ttl_sweeper_awaits_an_async_callback():
    """A node-teardown callback must be awaited, not dropped.

    The callback that tears a sandbox down on its worker reaches the node over
    HTTP. Reaching it with a *blocking* call from this loop is what the N32
    measurement caught (a 76 s stall that made two live workers look gone), so
    the callback is async and the sweeper has to await it -- a coroutine that
    is merely called would never run, and the tree would stay on the worker
    with nobody noticing.
    """
    registry = SandboxRegistry(_settings())
    record = _create(registry, timeout=300)
    record.end_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=1
    )
    reaped: list[str] = []
    finished = asyncio.Event()

    async def _on_expired(r) -> None:
        await asyncio.sleep(0.01)
        reaped.append(r.sandbox_id)
        finished.set()

    sweeper = TTLSweeper(interval_seconds=0.05, on_expired=_on_expired)
    sweeper.start(registry)
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        await sweeper.stop()
    assert reaped == [record.sandbox_id]


@pytest.mark.asyncio
async def test_the_record_survives_the_teardown_it_authorizes():
    """The worker's teardown must still be able to name the sandbox (N53).

    The sweep tears the sandbox down on its worker *after* the record is gone,
    and the worker's teardown asks the control plane to remove the tree
    (``remove-workspace``) -- a request authorized against the control plane's
    own records. Measured on the fleet 2026-10-01: freed first, the record made
    that request come back 404, so the tree and the worker's runtime record
    stayed on disk with nobody left who may act on them (and the worker then
    re-asked every disk round: ~5 req/s of 404s per stale record).
    """
    registry = SandboxRegistry(_settings())
    record = _create(registry, timeout=300)
    record.end_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=1
    )
    seen: list[bool] = []
    finished = asyncio.Event()

    async def _on_expired(r) -> None:
        seen.append(registry.get(r.sandbox_id) is not None)
        finished.set()

    sweeper = TTLSweeper(interval_seconds=0.05, on_expired=_on_expired)
    sweeper.start(registry)
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        await sweeper.stop()

    assert seen == [True], "the teardown ran after its record was already released"
    assert registry.count() == 0, "the record still has to go once the teardown ran"
