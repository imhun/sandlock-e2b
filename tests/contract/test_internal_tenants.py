"""E3.1: GET /internal/tenants management endpoint."""

from __future__ import annotations

import httpx
import pytest

from control_plane.config import Settings

T1 = "t1"
T2 = "t2"
ADMIN = "admin-key"
INTERNAL = "internal-key"


def _tenant_settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=(),
        tenant_map={T1: ["keyA"], T2: ["keyC"]},
        admin_api_keys=(ADMIN,),
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_internal_tenants_reports_usage_and_limits(make_apps):
    control, _envd = make_apps(
        control_settings=_tenant_settings(
            tenant_limits={T1: {"max_sandboxes": 3, "max_total_memory_mb": 2048}}
        )
    )
    async with await _client(control) as client:
        created = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "keyA"},
            json={"templateID": "base", "timeout": 120},
        )
        assert created.status_code == 201

        resp = await client.get("/internal/tenants", headers={"X-Internal-Key": INTERNAL})
        assert resp.status_code == 200
        body = resp.json()
        assert body["compatibleMode"] is False
        assert "unowned" not in body
        by_tenant = {t["tenantID"]: t for t in body["tenants"]}
        assert set(by_tenant) == {T1, T2}
        assert by_tenant[T1]["used"] == {
            "sandboxes": 1,
            "memoryMB": 512,
            "cpuPercent": 100,
            "diskMB": 1024,
            "processes": 64,
        }
        assert by_tenant[T1]["limits"] == {
            "max_sandboxes": 3,
            "max_total_memory_mb": 2048,
        }
        assert by_tenant[T2]["used"] == {
            "sandboxes": 0,
            "memoryMB": 0,
            "cpuPercent": 0,
            "diskMB": 0,
            "processes": 0,
        }
        assert by_tenant[T2]["limits"] == {}


async def test_internal_tenants_requires_internal_key(make_apps):
    control, _envd = make_apps(control_settings=_tenant_settings())
    async with await _client(control) as client:
        resp = await client.get("/internal/tenants")
        assert resp.status_code == 401
        assert resp.json() == {"code": 401, "message": "Unauthorized"}


async def test_internal_tenants_compat_mode(make_apps):
    control, _envd = make_apps(
        control_settings=Settings(api_keys=("local-key",))
    )
    async with await _client(control) as client:
        created = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 120},
        )
        assert created.status_code == 201
        resp = await client.get("/internal/tenants", headers={"X-Internal-Key": INTERNAL})
        assert resp.status_code == 200
        body = resp.json()
        assert body["compatibleMode"] is True
        assert body["tenants"] == []
        # Unowned usage is reported so migration gaps stay visible.
        assert body["unowned"] == {
            "sandboxes": 1,
            "memoryMB": 512,
            "cpuPercent": 100,
            "diskMB": 1024,
            "processes": 64,
        }


async def test_internal_tenants_reports_unowned_after_mixed_creates(make_apps):
    control, _envd = make_apps(
        control_settings=_tenant_settings(admin_api_keys=(ADMIN,))
    )
    async with await _client(control) as client:
        tenant_made = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "keyA"},
            json={"templateID": "base", "timeout": 120},
        )
        assert tenant_made.status_code == 201
        admin_made = await client.post(
            "/sandboxes",
            headers={"X-API-Key": ADMIN},
            json={"templateID": "base", "timeout": 120},
        )
        assert admin_made.status_code == 201

        body = (await client.get(
            "/internal/tenants", headers={"X-Internal-Key": INTERNAL}
        )).json()
        assert body["unowned"] == {
            "sandboxes": 1,
            "memoryMB": 512,
            "cpuPercent": 100,
            "diskMB": 1024,
            "processes": 64,
        }
