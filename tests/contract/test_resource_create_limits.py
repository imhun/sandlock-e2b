"""Every resource-creating endpoint is admitted, not just sandbox create.

Sandbox create was throttled (E3.5/E9.3) while snapshot create -- which copies
a sandbox filesystem -- and volume create -- which allocates a quota slice --
were unbounded: an authenticated key could loop them for free. These pin the
admission on both, and pin that the budgets are per endpoint so a burst on one
cannot spend another's.
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.config import Settings

API = {"X-API-Key": "local-key"}


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
        create_rate_limit_per_min=0,
        create_queue_timeout_s=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://c"
    )


async def test_volume_create_is_rate_limited(make_apps):
    control, _envd = make_apps(control_settings=_settings(volume_rate_limit_per_min=2))
    async with _client(control) as client:
        assert (
            await client.post("/volumes", headers=API, json={"name": "v1"})
        ).status_code == 201
        assert (
            await client.post("/volumes", headers=API, json={"name": "v2"})
        ).status_code == 201
        over = await client.post("/volumes", headers=API, json={"name": "v3"})
        assert over.status_code == 429
        assert over.json()["message"] == "Volume create rate limit exceeded"


async def test_snapshot_create_is_rate_limited(make_apps):
    control, envd = make_apps(
        control_settings=_settings(snapshot_rate_limit_per_min=1)
    )
    async with _client(control) as client:
        created = await client.post(
            "/sandboxes", headers=API, json={"templateID": "base", "timeout": 120}
        )
        assert created.status_code == 201
        sandbox_id = created.json()["sandboxID"]
        first = await client.post(
            f"/sandboxes/{sandbox_id}/snapshots", headers=API, json={}
        )
        assert first.status_code == 201
        over = await client.post(
            f"/sandboxes/{sandbox_id}/snapshots", headers=API, json={}
        )
        assert over.status_code == 429
        assert over.json()["message"] == "Snapshot create rate limit exceeded"


async def test_the_budgets_are_per_endpoint(make_apps):
    """A snapshot burst must not spend the sandbox-create budget."""
    control, _envd = make_apps(
        control_settings=_settings(
            snapshot_rate_limit_per_min=1, create_rate_limit_per_min=5
        )
    )
    async with _client(control) as client:
        sandbox = await client.post(
            "/sandboxes", headers=API, json={"templateID": "base", "timeout": 120}
        )
        assert sandbox.status_code == 201
        sandbox_id = sandbox.json()["sandboxID"]
        assert (
            await client.post(
                f"/sandboxes/{sandbox_id}/snapshots", headers=API, json={}
            )
        ).status_code == 201
        # Snapshot budget is now spent...
        assert (
            await client.post(
                f"/sandboxes/{sandbox_id}/snapshots", headers=API, json={}
            )
        ).status_code == 429
        # ...and sandbox create still has its own.
        still = await client.post(
            "/sandboxes", headers=API, json={"templateID": "base", "timeout": 120}
        )
        assert still.status_code == 201


async def test_zero_disables_the_limiter(make_apps):
    """Repo convention: ``0`` disables, and the default is the create budget."""
    assert Settings().volume_rate_limit_per_min == 120
    assert Settings().snapshot_rate_limit_per_min == 120
    assert Settings(volume_rate_limit_per_min=0).volume_rate_limit_per_min == 0
    control, _envd = make_apps(control_settings=_settings(volume_rate_limit_per_min=0))
    async with _client(control) as client:
        for i in range(5):
            assert (
                await client.post("/volumes", headers=API, json={"name": f"v{i}"})
            ).status_code == 201
