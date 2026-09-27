"""E6: the paused-sandbox TTL sweep (``E2B_PAUSED_TTL_S``, default 0 = off).

Parking a sandbox is *supposed* to preserve the session, so the ordinary TTL
sweep deliberately skips ``paused`` records (``SandboxRegistry._ttl_reapable``)
-- which also means a parked sandbox whose owner walked away keeps the platform
account (its checkpoint image is the largest thing the platform holds for it)
forever. ``E2B_PAUSED_TTL_S`` is the opt-in that ends that, and it is
**destructive** (the parked session is gone), so the default has to be "do
nothing" and the switch has to be proved in both directions.

The cleanup is deliberately the *same one delete runs*: each case here either
drives the sweeper through the app's own wiring or hands it a teardown that
stands in for it, and asserts the post-conditions delete promises -- the record
is gone, the reservation ledger is at zero, and one named line says which
sandbox went and what went with it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from control_plane.app import create_app
from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry, UnknownSandboxError
from control_plane.registry.nodes import NodeRegistry
from gateway_common.timeutil import utcnow
from control_plane.registry.paused_ttl import (
    PAUSED_TTL_ENV,
    PausedTTLSweeper,
    expired_paused_records,
    paused_ttl_seconds,
    reap_paused_sandbox,
)

#: The instant every case ages its records against -- never the wall clock, so
#: "600 s old" cannot drift while the suite runs.
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)

LEDGER_ZERO = {"memory": 0, "cpu": 0, "disk": 0, "processes": 0}
LEDGER_ONE = {"memory": 512, "cpu": 100, "disk": 0, "processes": 64}

_logger = logging.getLogger("control_plane.registry.paused_ttl")


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=0,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=0,
        default_max_processes=64,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
        internal_api_key="internal-key",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry, sandbox_id: str):
    return registry.create(
        template_id="base",
        sandbox_id=sandbox_id,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )


def _pause(
    registry,
    sandbox_id: str,
    *,
    age_s: float,
    released: bool = True,
    reference: datetime = NOW,
):
    """A sandbox that has been ``paused`` for ``age_s`` seconds.

    ``released=True`` is today's pause (E9.2 hands the reservation back at
    pause time). ``released=False`` is a record from before that release
    existed -- still holding its reservation while parked -- which is the shape
    that makes "the reservation comes back" observable at all.

    ``reference`` is ``NOW`` for every case that injects a clock into the
    sweeper, and the wall clock for the two cases that go through
    ``create_app`` (whose sweeper runs on ``utcnow`` by construction).
    """
    record = _create(registry, sandbox_id)
    if released:
        registry.pause(record)
    else:
        record.pause()
        registry.save(record)
    record.paused_at = reference - timedelta(seconds=age_s)
    return registry.save(record)


async def _teardown(record):
    """Stand-in for the node teardown delete performs (worker DELETE)."""
    return ["runtime", "checkpoint-image"]


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _logger.name]


def test_the_switch_defaults_to_zero_and_a_positive_value_turns_it_on(monkeypatch):
    monkeypatch.delenv(PAUSED_TTL_ENV, raising=False)
    assert paused_ttl_seconds() == 0.0

    monkeypatch.setenv(PAUSED_TTL_ENV, "600")
    assert paused_ttl_seconds() == 600.0

    # A settings object that grows the field wins over the environment.
    assert paused_ttl_seconds(SimpleNamespace(paused_ttl_s=30.5)) == 30.5


def test_ttl_zero_selects_nothing_however_old_the_sandbox_is(tmp_path):
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    _pause(registry, "sbx_ancient", age_s=10_000_000)

    assert expired_paused_records(
        registry.list(state_filter=["paused"]), ttl_s=0, now=NOW
    ) == []

    sweeper = PausedTTLSweeper(ttl_s=0, on_expired=_teardown, now=lambda: NOW)
    assert sweeper.enabled is False
    assert sweeper.due(registry) == []


def test_only_a_paused_record_past_the_ttl_is_selected(tmp_path):
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    _pause(registry, "sbx_paused_young", age_s=30)
    old = _pause(registry, "sbx_paused_old", age_s=600)
    _create(registry, "sbx_running")
    expired_running = _create(registry, "sbx_running_expired")
    expired_running.end_at = NOW - timedelta(seconds=10)
    registry.save(expired_running)
    _create(registry, "sbx_gone")
    registry.delete("sbx_gone")

    sweeper = PausedTTLSweeper(ttl_s=300, on_expired=_teardown, now=lambda: NOW)
    assert [(r.sandbox_id, age) for r, age in sweeper.due(registry)] == [
        (old.sandbox_id, 600.0)
    ]


def test_the_selection_skips_a_running_record_by_its_state(tmp_path):
    """The state check is load-bearing: pin it on the function that decides.

    ``due()`` asks the registry for ``paused`` records only, so a case that
    goes through the sweeper cannot see the check inside
    ``expired_paused_records`` at all. A caller that hands it the whole fleet
    -- which is what a future caller will do, and what this case does -- has to
    get parked sandboxes back and nothing else: without this, dropping the
    state check would be invisible, and an ordinary sandbox that merely
    outlived its TTL would be torn down as if it had been parked.
    """
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    _pause(registry, "sbx_paused_old", age_s=600)
    expired_running = _create(registry, "sbx_running_expired")
    expired_running.end_at = NOW - timedelta(seconds=3600)
    registry.save(expired_running)

    assert [
        (r.sandbox_id, age)
        for r, age in expired_paused_records(registry.list(), ttl_s=300, now=NOW)
    ] == [("sbx_paused_old", 600.0)]


@pytest.mark.asyncio
async def test_the_reap_gives_a_still_held_reservation_back(tmp_path):
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    record = _pause(registry, "sbx_still_held", age_s=600, released=False)
    assert registry.global_reserved() == LEDGER_ONE

    torn: list[str] = []

    async def _torn(record):
        torn.append(record.sandbox_id)
        return ["runtime", "checkpoint-image"]

    assert await reap_paused_sandbox(
        record, registry=registry, teardown=_torn
    ) == ["runtime", "checkpoint-image", "record", "quota"]
    assert torn == ["sbx_still_held"]
    with pytest.raises(UnknownSandboxError):
        registry.get("sbx_still_held")
    assert registry.global_reserved() == LEDGER_ZERO


@pytest.mark.asyncio
async def test_a_paused_record_that_already_gave_its_quota_back_is_not_released_twice(
    tmp_path,
):
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    record = _pause(registry, "sbx_released", age_s=600)
    assert registry.global_reserved() == LEDGER_ZERO

    assert await reap_paused_sandbox(
        record, registry=registry, teardown=_teardown
    ) == ["runtime", "checkpoint-image", "record"]
    assert registry.global_reserved() == LEDGER_ZERO
    with pytest.raises(UnknownSandboxError):
        registry.get("sbx_released")


@pytest.mark.asyncio
async def test_the_sweeper_leaves_one_named_line_per_reaped_sandbox(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=_logger.name)
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    record = _pause(registry, "sbx_named", age_s=600)

    async def _on_expired(record, paused_for_s):
        return await reap_paused_sandbox(
            record, registry=registry, teardown=_teardown
        )

    sweeper = PausedTTLSweeper(
        ttl_s=300,
        interval_seconds=0.02,
        on_expired=_on_expired,
        now=lambda: NOW,
    )
    sweeper.start(registry)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _messages(caplog):
            await asyncio.sleep(0.02)
    finally:
        await sweeper.stop()

    assert _messages(caplog) == [
        "paused TTL: sandbox sbx_named had been paused 600s (>= 300s); "
        "removed runtime, checkpoint-image, record"
    ]
    assert registry.count() == 0
    assert record.sandbox_id == "sbx_named"


@pytest.mark.asyncio
async def test_a_lost_claim_skips_the_round(tmp_path):
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    _pause(registry, "sbx_claimed", age_s=600)

    swept: list[str] = []

    async def _on_expired(record, paused_for_s):
        swept.append(record.sandbox_id)
        return ["record"]

    sweeper = PausedTTLSweeper(
        ttl_s=300,
        interval_seconds=0.02,
        on_expired=_on_expired,
        claim=lambda: False,
        now=lambda: NOW,
    )
    sweeper.start(registry)
    try:
        await asyncio.sleep(0.2)
    finally:
        await sweeper.stop()

    assert swept == []
    assert registry.count() == 1


@pytest.mark.asyncio
async def test_a_shared_claim_means_exactly_one_reaper(tmp_path):
    registry = SandboxRegistry(_settings(workspace_base=tmp_path))
    _pause(registry, "sbx_shared", age_s=600)

    claims: list[bool] = []
    torn: list[str] = []

    def _claim() -> bool:
        # One TTL'd key: the first caller in the window wins, every other
        # caller (the second replica) loses.
        claims.append(len(claims) == 0)
        return claims[-1]

    async def _torn_teardown(record):
        torn.append(record.sandbox_id)
        return ["runtime", "checkpoint-image"]

    async def _on_expired(record, paused_for_s):
        return await reap_paused_sandbox(
            record, registry=registry, teardown=_torn_teardown
        )

    sweeper_a = PausedTTLSweeper(
        ttl_s=300, interval_seconds=0.02, on_expired=_on_expired,
        claim=_claim, now=lambda: NOW,
    )
    sweeper_b = PausedTTLSweeper(
        ttl_s=300, interval_seconds=0.02, on_expired=_on_expired,
        claim=_claim, now=lambda: NOW,
    )
    sweeper_a.start(registry)
    sweeper_b.start(registry)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and registry.count() != 0:
            await asyncio.sleep(0.02)
    finally:
        await sweeper_a.stop()
        await sweeper_b.stop()

    assert torn == ["sbx_shared"]
    assert claims.count(True) == 1
    assert registry.count() == 0


@pytest.mark.asyncio
async def test_the_app_wires_the_sweep_into_the_same_teardown_delete_uses(
    tmp_path, monkeypatch, caplog
):
    """The wiring, not the policy: this is the one that catches "nothing calls it".

    ``create_app`` must take the sweep's node teardown down the delete path --
    the worker's ``DELETE /agent/sandboxes/{id}`` (which is what removes the
    checkpoint image) -- and then drop the record. A sweeper that is never
    started, or that deletes the record without the teardown, passes every
    policy case above and fails this one.
    """
    monkeypatch.setenv(PAUSED_TTL_ENV, "300")
    caplog.set_level(logging.WARNING, logger=_logger.name)

    deletes: list[tuple[str, dict]] = []

    class _FakeResponse:
        status_code = 204
        text = ""

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def delete(self, url, headers=None, params=None):
            deletes.append((url, dict(headers or {})))
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    settings = _settings(workspace_base=tmp_path)
    nodes = NodeRegistry()
    nodes.register(
        node_id="e2b-worker-1",
        address="http://worker-1:49984",
        total_memory_mb=0,
        total_cpu_percent=0,
        total_disk_mb=0,
        total_processes=0,
    )
    app = create_app(
        settings=settings,
        runtime_registry=SimpleNamespace(
            unregister=lambda sandbox_id: None
        ),
        nodes_registry=nodes,
        workspace_base=tmp_path,
    )
    async with app.router.lifespan_context(app):
        registry = app.state.registry
        record = _pause(
            registry, "sbx_wired", age_s=600, reference=utcnow()
        )
        record.node_id = "e2b-worker-1"
        registry.save(record)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not deletes:
            await asyncio.sleep(0.02)

    assert deletes == [
        (
            "http://worker-1:49984/agent/sandboxes/sbx_wired",
            {"X-Internal-Key": "internal-key"},
        )
    ]
    assert registry.count() == 0
    assert registry.global_reserved() == LEDGER_ZERO
    messages = _messages(caplog)
    assert len(messages) == 1
    assert re.fullmatch(
        r"paused TTL: sandbox sbx_wired had been paused \d+s \(>= 300s\); "
        r"removed runtime, checkpoint-image, record",
        messages[0],
    ) is not None


@pytest.mark.asyncio
async def test_the_app_does_not_start_the_sweep_when_the_switch_is_off(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(PAUSED_TTL_ENV, raising=False)
    settings = _settings(workspace_base=tmp_path)
    app = create_app(
        settings=settings,
        runtime_registry=SimpleNamespace(unregister=lambda sandbox_id: None),
        nodes_registry=NodeRegistry(),
        workspace_base=tmp_path,
    )
    async with app.router.lifespan_context(app):
        assert app.state.paused_sweeper.enabled is False
        assert app.state.paused_sweeper._task is None
        registry = app.state.registry
        record = _pause(
            registry, "sbx_untouched", age_s=10_000_000, reference=utcnow()
        )
        await asyncio.sleep(0.1)
        assert registry.get(record.sandbox_id).state == "paused"
    assert registry.count() == 1
