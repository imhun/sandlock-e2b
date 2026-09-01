"""E3.6: internal key rotation window (old + new valid, then old dies)."""

from __future__ import annotations

import httpx

from control_plane.config import Settings
from envd_service.gateway import create_gateway


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=(),
        internal_api_key="new-key",
        internal_api_keys=("old-key", "new-key"),
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_rotation_window_accepts_old_and_new_key(make_apps):
    control, _envd = make_apps(control_settings=_settings())
    async with _client(control) as client:
        old = await client.get(
            "/internal/nodes", headers={"X-Internal-Key": "old-key"}
        )
        assert old.status_code == 200
        new = await client.get(
            "/internal/nodes", headers={"X-Internal-Key": "new-key"}
        )
        assert new.status_code == 200
        stale = await client.get(
            "/internal/nodes", headers={"X-Internal-Key": "stale-key"}
        )
        assert stale.status_code == 401
        assert stale.json() == {"code": 401, "message": "Unauthorized"}


async def test_old_key_invalid_after_finalize(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(internal_api_keys=("new-key",))
    )
    async with _client(control) as client:
        new = await client.get(
            "/internal/nodes", headers={"X-Internal-Key": "new-key"}
        )
        assert new.status_code == 200
        old = await client.get(
            "/internal/nodes", headers={"X-Internal-Key": "old-key"}
        )
        assert old.status_code == 401
        assert old.json() == {"code": 401, "message": "Unauthorized"}


async def test_legacy_single_internal_key_still_works(make_apps):
    control, _envd = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            internal_api_key="internal-key",
            internal_api_keys=(),
        )
    )
    async with _client(control) as client:
        ok = await client.get(
            "/internal/nodes", headers={"X-Internal-Key": "internal-key"}
        )
        assert ok.status_code == 200
        bad = await client.get(
            "/internal/nodes", headers={"X-Internal-Key": "other-key"}
        )
        assert bad.status_code == 401


async def test_gateway_invalidate_route_rotation_window():
    gateway = create_gateway(
        control_plane_url="http://control-plane:3000",
        internal_api_key="new-key",
        internal_api_keys=("old-key", "new-key"),
    )
    async with _client(gateway) as client:
        for key in ("old-key", "new-key"):
            resp = await client.post(
                "/internal/routes/sbx_1/invalidate",
                headers={"X-Internal-Key": key},
            )
            assert resp.status_code == 204
        stale = await client.post(
            "/internal/routes/sbx_1/invalidate",
            headers={"X-Internal-Key": "stale-key"},
        )
        assert stale.status_code == 401

    finalized = create_gateway(
        control_plane_url="http://control-plane:3000",
        internal_api_key="new-key",
        internal_api_keys=("new-key",),
    )
    async with _client(finalized) as client:
        old = await client.post(
            "/internal/routes/sbx_1/invalidate",
            headers={"X-Internal-Key": "old-key"},
        )
        assert old.status_code == 401
        new = await client.post(
            "/internal/routes/sbx_1/invalidate",
            headers={"X-Internal-Key": "new-key"},
        )
        assert new.status_code == 204
