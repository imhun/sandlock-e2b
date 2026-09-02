"""E9.4 API contract: POST /sandboxes queues for capacity after eviction.

The pool configured here fits exactly one sandbox and eviction is disabled,
so "full" is deterministic and every queue outcome is observable through the
public endpoints: a queued create completes when capacity is freed (delete or
pause), times out with the original 503, answers 429 when the queue is full,
and never over-sells under concurrent load.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from control_plane.config import Settings as ControlSettings

API = {"X-API-Key": "local-key"}


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=512,
        max_total_cpu_percent=100,
        max_total_disk_mb=1024,
        max_total_processes=64,
        sandbox_idle_threshold_s=600,
        eviction_enabled=False,
        eviction_min_interval_s=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


async def _create(client, sandbox_id=None):
    body = {"templateID": "base", "timeout": 300}
    headers = dict(API)
    if sandbox_id:
        headers["X-Sandbox-Id"] = sandbox_id
    return await client.post("/sandboxes", headers=headers, json=body)


async def _spin_until_waiting(control, count: int) -> None:
    while control.state.create_queue.stats()["waiting"] < count:
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_queued_create_completes_when_capacity_is_deleted(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=5,
            create_queue_max=10,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_qdel_a")).status_code == 201
        waiter = asyncio.create_task(_create(client, "sbx_qdel_b"))
        await _spin_until_waiting(control, 1)

        deleted = await client.delete("/sandboxes/sbx_qdel_a", headers=API)
        assert deleted.status_code == 204

        created = await asyncio.wait_for(waiter, timeout=5)
        assert created.status_code == 201
        assert created.json()["sandboxID"] == "sbx_qdel_b"
        assert control.state.create_queue.stats()["waiting"] == 0


@pytest.mark.asyncio
async def test_queued_create_completes_after_pause_releases_capacity(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=5,
            create_queue_max=10,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_qp_a")).status_code == 201
        waiter = asyncio.create_task(_create(client, "sbx_qp_b"))
        await _spin_until_waiting(control, 1)

        paused = await client.post("/sandboxes/sbx_qp_a/pause", headers=API, json={})
        assert paused.status_code == 204

        created = await asyncio.wait_for(waiter, timeout=5)
        assert created.status_code == 201
        assert created.json()["sandboxID"] == "sbx_qp_b"
        # The paused record kept its identity; only the queued create runs.
        assert control.state.registry.get("sbx_qp_a").state == "paused"
        assert control.state.registry.get("sbx_qp_b").state == "running"


@pytest.mark.asyncio
async def test_full_pool_queue_times_out_with_original_503(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=0.2,
            create_queue_max=10,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_qto_a")).status_code == 201
        started = time.monotonic()
        refused = await _create(client, "sbx_qto_b")
        elapsed = time.monotonic() - started

        assert refused.status_code == 503
        assert refused.json() == {"code": 503, "message": "No resources available"}
        assert elapsed >= 0.15  # the request really waited for the deadline
        assert elapsed < 2.0
        # Timeout keeps the E9.3 recent-failures accounting semantics.
        assert control.state.recent_failures.count() == 1


@pytest.mark.asyncio
async def test_queue_disabled_keeps_immediate_503(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=0,
            create_queue_max=10,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_qoff_a")).status_code == 201
        started = time.monotonic()
        refused = await _create(client, "sbx_qoff_b")
        elapsed = time.monotonic() - started

        assert refused.status_code == 503
        assert refused.json()["message"] == "No resources available"
        assert elapsed < 0.5


@pytest.mark.asyncio
async def test_zero_queue_max_returns_429_with_retry_after(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=5,
            create_queue_max=0,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_q429_a")).status_code == 201
        refused = await _create(client, "sbx_q429_b")
        assert refused.status_code == 429
        assert refused.json() == {
            "code": 429,
            "message": "Sandbox create queue is full",
        }
        assert refused.headers["retry-after"] == "1"
        # A shed request is still a saturation signal: the autoscaler reads
        # recent_failures, so the queue-full path must count it too.
        assert control.state.recent_failures.count() == 1


@pytest.mark.asyncio
async def test_second_waiter_gets_429_when_queue_is_full(make_apps):
    """max_waiters=1: one queued create occupies the slot, another is 429."""
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=5,
            create_queue_max=1,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_qfull_a")).status_code == 201
        waiter = asyncio.create_task(_create(client, "sbx_qfull_b"))
        await _spin_until_waiting(control, 1)

        refused = await _create(client, "sbx_qfull_c")
        assert refused.status_code == 429
        assert refused.json()["message"] == "Sandbox create queue is full"
        assert refused.headers["retry-after"] == "1"

        # Freeing capacity lets the queued waiter (not the 429 request) in.
        assert (
            await client.delete("/sandboxes/sbx_qfull_a", headers=API)
        ).status_code == 204
        created = await asyncio.wait_for(waiter, timeout=5)
        assert created.status_code == 201
        assert created.json()["sandboxID"] == "sbx_qfull_b"


@pytest.mark.asyncio
async def test_same_id_queue_retries_resolve_to_one_sandbox(make_apps):
    """Two queued retries of one X-Sandbox-Id both return 201; one record."""
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=5,
            create_queue_max=10,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_qsame_hold")).status_code == 201
        first = asyncio.create_task(_create(client, "sbx_qsame"))
        second = asyncio.create_task(_create(client, "sbx_qsame"))
        await _spin_until_waiting(control, 2)

        assert (
            await client.delete("/sandboxes/sbx_qsame_hold", headers=API)
        ).status_code == 204

        r1, r2 = await asyncio.wait_for(
            asyncio.gather(first, second), timeout=6
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert {r.json()["sandboxID"] for r in (r1, r2)} == {"sbx_qsame"}
        assert control.state.registry.count() == 1
        assert control.state.registry.get("sbx_qsame").state == "running"


@pytest.mark.asyncio
async def test_concurrent_queued_creates_never_oversell(make_apps):
    """Five concurrent creates on a one-slot pool: exactly one running."""
    control, _envd = make_apps(
        control_settings=_settings(
            create_queue_timeout_s=0.4,
            create_queue_max=20,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        ids = [f"sbx_nosell_{i}" for i in range(5)]
        responses = await asyncio.gather(*(_create(client, sid) for sid in ids))
        by_id = dict(zip(ids, responses))

        created_ids = {sid for sid in ids if by_id[sid].status_code == 201}
        refused_ids = [sid for sid in ids if by_id[sid].status_code == 503]
        assert len(created_ids) == 1
        assert len(refused_ids) == 4
        for sid in refused_ids:
            assert by_id[sid].json()["message"] == "No resources available"

        registry = control.state.registry
        running_ids = {r.sandbox_id for r in registry.list(state_filter=["running"])}
        assert running_ids == created_ids
        assert registry.count() == 1


@pytest.mark.asyncio
async def test_app_wires_queue_from_settings(make_apps):
    """``app.state.create_queue`` must carry the configured bounds (E9.4)."""
    control, _envd = make_apps(
        control_settings=_settings(create_queue_timeout_s=7, create_queue_max=4)
    )
    stats = control.state.create_queue.stats()
    assert stats == {"waiting": 0, "timeout_s": 7.0, "max": 4}
