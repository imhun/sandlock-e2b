"""E9.3 API contract: resource-driven eviction on POST /sandboxes.

The pool configured here fits exactly two sandboxes, so "full" is reachable
and every eviction outcome is observable through the public endpoints: a
successful create with the idle victim gone (kill), paused (prefer_pause),
an unchanged 503 when nobody is idle, and the eviction notice surfacing as
the special 404 message + ``x-e2b-eviction-reason`` header.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from control_plane.config import Settings as ControlSettings
from gateway_common.timeutil import utcnow

API = {"X-API-Key": "local-key"}


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=1024,
        max_total_cpu_percent=200,
        max_total_disk_mb=2048,
        max_total_processes=128,
        sandbox_idle_threshold_s=600,
        eviction_min_interval_s=0,
        # E9.4 isolation: these cases assert E9.3 eviction semantics (503
        # once eviction cannot help); the create queue must not stall them.
        create_queue_timeout_s=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


async def _create(client, sandbox_id=None, **body):
    payload = {"templateID": "base", "timeout": 300}
    payload.update(body)
    headers = dict(API)
    if sandbox_id:
        headers["X-Sandbox-Id"] = sandbox_id
    return await client.post("/sandboxes", headers=headers, json=payload)


def _backdate(registry, sandbox_id, seconds=1200):
    """Make a record look idle (E9.1 test pattern; no real waiting)."""
    record = registry.get(sandbox_id)
    record.last_active_at = utcnow() - timedelta(seconds=seconds)
    registry.save(record)
    return record


@pytest.mark.asyncio
async def test_full_pool_evicts_idle_low_priority_victim_with_notice(make_apps):
    control, _envd = make_apps(control_settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_victim", priority=1)).status_code == 201
        assert (await _create(client, "sbx_filler", priority=9)).status_code == 201
        _backdate(control.state.registry, "sbx_victim")

        created = await _create(client, "sbx_new", priority=5)
        assert created.status_code == 201
        assert created.json()["sandboxID"] == "sbx_new"

        registry = control.state.registry
        assert registry.count() == 2
        assert registry.get("sbx_filler").state == "running"

        gone = await client.get("/sandboxes/sbx_victim", headers=API)
        assert gone.status_code == 404
        assert gone.json()["message"] == (
            "Sandbox sbx_victim not found (evicted: evicted-idle)"
        )
        assert gone.headers["x-e2b-eviction-reason"] == "evicted-idle"

        # A sandbox that never existed keeps the plain 404, no eviction header.
        ghost = await client.get("/sandboxes/sbx_ghost1", headers=API)
        assert ghost.status_code == 404
        assert ghost.json()["message"] == "Sandbox sbx_ghost1 not found"
        assert "x-e2b-eviction-reason" not in ghost.headers


@pytest.mark.asyncio
async def test_all_active_keeps_503_and_nobody_is_evicted(make_apps):
    control, _envd = make_apps(control_settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_active_a")).status_code == 201
        assert (await _create(client, "sbx_active_b")).status_code == 201
        registry = control.state.registry
        before = registry.count()

        refused = await _create(client, "sbx_new")
        assert refused.status_code == 503
        assert refused.json()["message"] == "No resources available"
        assert registry.count() == before


@pytest.mark.asyncio
async def test_eviction_disabled_keeps_today_behavior(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(eviction_enabled=False)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_victim")).status_code == 201
        assert (await _create(client, "sbx_filler")).status_code == 201
        _backdate(control.state.registry, "sbx_victim")

        refused = await _create(client, "sbx_new")
        assert refused.status_code == 503
        assert refused.json()["message"] == "No resources available"

        registry = control.state.registry
        assert registry.count() == 2
        assert registry.get("sbx_victim").state == "running"
        info = await client.get("/sandboxes/sbx_victim", headers=API)
        assert info.status_code == 200
        assert info.json()["state"] == "running"


@pytest.mark.asyncio
async def test_prefer_pause_leaves_victim_paused_and_quota_released(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(eviction_prefer_pause=True)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_victim")).status_code == 201
        assert (await _create(client, "sbx_filler")).status_code == 201
        _backdate(control.state.registry, "sbx_victim")

        created = await _create(client, "sbx_new")
        assert created.status_code == 201

        registry = control.state.registry
        assert registry.count() == 3
        victim = registry.get("sbx_victim")
        assert victim.state == "paused"
        assert victim.quota_released is True
        info = await client.get("/sandboxes/sbx_victim", headers=API)
        assert info.status_code == 200
        assert info.json()["state"] == "paused"


@pytest.mark.asyncio
async def test_low_priority_idle_evicted_before_high_priority_idle(make_apps):
    control, _envd = make_apps(control_settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_low", priority=0)).status_code == 201
        assert (await _create(client, "sbx_high", priority=9)).status_code == 201
        registry = control.state.registry
        _backdate(registry, "sbx_low")
        _backdate(registry, "sbx_high")

        created = await _create(client, "sbx_new", priority=5)
        assert created.status_code == 201
        assert created.json()["sandboxID"] == "sbx_new"

        # Exactly the low-priority victim was sacrificed; the high-priority
        # idle sandbox survived because one slot was enough.
        assert registry.count() == 2
        assert registry.get("sbx_high").state == "running"
        assert registry.get("sbx_new").state == "running"
        gone = await client.get("/sandboxes/sbx_low", headers=API)
        assert gone.status_code == 404
        assert gone.headers["x-e2b-eviction-reason"] == "evicted-idle"
        still_here = await client.get("/sandboxes/sbx_high", headers=API)
        assert still_here.status_code == 200


@pytest.mark.asyncio
async def test_eviction_retry_spends_one_rate_limit_token(make_apps):
    """A retried create must not bill the client twice (E9.3 review)."""
    control, _envd = make_apps(
        control_settings=_settings(create_rate_limit_per_min=3)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_rl1", priority=1)).status_code == 201
        assert (await _create(client, "sbx_rl2", priority=9)).status_code == 201
        _backdate(control.state.registry, "sbx_rl1")

        # Tokens 1 and 2 are spent; attempt #1 of this request takes token 3
        # and fails on capacity, so a retry that re-checked the limiter would
        # answer 429 for a request the client sent exactly once.
        created = await _create(client, "sbx_rl3", priority=5)
        assert created.status_code == 201
        assert created.json()["sandboxID"] == "sbx_rl3"

        # The budget really is exhausted now: the next request is 429.
        over = await _create(client, "sbx_rl4")
        assert over.status_code == 429
        assert over.json()["message"] == "Sandbox create rate limit exceeded"
