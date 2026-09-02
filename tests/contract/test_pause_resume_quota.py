"""E9.2 API contract: pause releases capacity, resume re-admits.

Two sandboxes fit in the pool configured here, so "full" is reachable and the
503 / stays-paused behaviour can be observed through the public endpoints.
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.config import Settings as ControlSettings
from gateway_common.timeutil import utcnow

from datetime import timedelta

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
        # E9.4 isolation: these cases assert E9.2 semantics (immediate 503 on
        # a full pool); the create queue must not stall them.
        create_queue_timeout_s=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


async def _create(client, sandbox_id=None):
    body = {"templateID": "base", "timeout": 300}
    headers = dict(API)
    if sandbox_id:
        headers["X-Sandbox-Id"] = sandbox_id
    return await client.post("/sandboxes", headers=headers, json=body)


@pytest.mark.asyncio
async def test_pause_frees_capacity_and_resume_buys_it_back(make_apps):
    control, _envd = make_apps(control_settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        assert (await _create(client, "sbx_pqa")).status_code == 201
        assert (await _create(client, "sbx_pqb")).status_code == 201
        full = await _create(client, "sbx_pqc")
        assert full.status_code == 503
        assert full.json()["message"] == "No resources available"

        paused = await client.post("/sandboxes/sbx_pqa/pause", headers=API, json={})
        assert paused.status_code == 204
        assert (await _create(client, "sbx_pqc")).status_code == 201

        # The pool is full again: resuming the parked sandbox must not evict
        # anyone implicitly (that is E9.3's job) — it just fails.
        refused = await client.post("/sandboxes/sbx_pqa/resume", headers=API, json={})
        assert refused.status_code == 503
        assert refused.json()["message"] == "No resources available"
        info = await client.get("/sandboxes/sbx_pqa", headers=API)
        assert info.json()["state"] == "paused"

        # Making room turns the same resume into a success.
        assert (await client.delete("/sandboxes/sbx_pqc", headers=API)).status_code == 204
        resumed = await client.post("/sandboxes/sbx_pqa/resume", headers=API, json={})
        assert resumed.status_code == 204
        info = await client.get("/sandboxes/sbx_pqa", headers=API)
        assert info.json()["state"] == "running"


@pytest.mark.asyncio
async def test_connect_on_full_parked_sandbox_reports_503(make_apps):
    control, _envd = make_apps(control_settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        await _create(client, "sbx_cqa")
        await _create(client, "sbx_cqb")
        assert (
            await client.post("/sandboxes/sbx_cqa/pause", headers=API, json={})
        ).status_code == 204
        assert (await _create(client, "sbx_cqc")).status_code == 201

        connected = await client.post("/sandboxes/sbx_cqa/connect", headers=API, json={})
        assert connected.status_code == 503
        assert connected.json()["message"] == "No resources available"
        # The parked record keeps its identity and stays parked.
        info = await client.get("/sandboxes/sbx_cqa", headers=API)
        assert info.json()["state"] == "paused"


@pytest.mark.asyncio
async def test_node_ledger_matches_global_ledger_across_park_and_kill(make_apps):
    """Killing a parked sandbox must not release its reservation a second time."""
    control, _envd = make_apps(control_settings=_settings(max_total_memory_mb=4096))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        await _create(client, "sbx_nqa")
        await _create(client, "sbx_nqb")
        assert (
            await client.post("/sandboxes/sbx_nqa/pause", headers=API, json={})
        ).status_code == 204
        assert (await client.delete("/sandboxes/sbx_nqa", headers=API)).status_code == 204

        registry = control.state.registry
        node = control.state.nodes.get("local")
        # Only sbx_nqb is live: both ledgers must say exactly one sandbox.
        assert registry._reserved_memory == 512
        assert node.reserved_memory_mb == 512


@pytest.mark.asyncio
async def test_parked_sandbox_survives_its_deadline(make_apps):
    control, _envd = make_apps(control_settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        await _create(client, "sbx_tqa")
        await _create(client, "sbx_tqb")
        assert (
            await client.post("/sandboxes/sbx_tqa/pause", headers=API, json={})
        ).status_code == 204

        registry = control.state.registry
        for sid in ("sbx_tqa", "sbx_tqb"):
            record = registry.get(sid)
            record.end_at = utcnow() - timedelta(seconds=5)
            registry.save(record)

        reaped = registry.remove_expired()
        assert [r.sandbox_id for r in reaped] == ["sbx_tqb"]
        assert registry.get("sbx_tqa").state == "paused"
        # The parked record holds nothing, so the pool is fully free again --
        # which is exactly what lets a new sandbox take over its slot.
        assert registry._reserved_memory == 0
        assert (await _create(client, "sbx_tqc")).status_code == 201
