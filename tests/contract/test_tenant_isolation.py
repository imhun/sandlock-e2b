"""E3.1: tenant isolation API matrix (design doc §7)."""

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
        tenant_map={T1: ["keyA", "keyB"], T2: ["keyC"]},
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


async def _create_sandbox(client, key: str, **body) -> httpx.Response:
    payload = {"templateID": "base", "timeout": 120}
    payload.update(body)
    return await client.post("/sandboxes", headers={"X-API-Key": key}, json=payload)


@pytest.fixture()
async def control(make_apps):
    control, _envd = make_apps(control_settings=_tenant_settings())
    async with await _client(control) as client:
        yield client


async def test_list_filtering_by_tenant(control):
    sbx = await _create_sandbox(control, "keyA")
    assert sbx.status_code == 201
    sandbox_id = sbx.json()["sandboxID"]

    vol = await control.post(
        "/volumes", headers={"X-API-Key": "keyA"}, json={"name": "t1-vol"}
    )
    assert vol.status_code == 201
    volume_id = vol.json()["volumeID"]

    sec = await control.post(
        "/secrets",
        headers={"X-API-Key": "keyB"},
        json={"name": "t1-secret", "value": "v"},
    )
    assert sec.status_code == 201
    secret_id = sec.json()["secretID"]

    tpl = await control.post(
        "/v3/templates", headers={"X-API-Key": "keyA"}, json={"name": "t1-tpl"}
    )
    assert tpl.status_code == 202
    template_id = tpl.json()["templateID"]

    other = await _create_sandbox(control, "keyC")
    assert other.status_code == 201
    other_id = other.json()["sandboxID"]

    # t1 (keyA) sees only t1 resources.
    assert [s["sandboxID"] for s in (await control.get(
        "/sandboxes", headers={"X-API-Key": "keyA"}
    )).json()] == [sandbox_id]
    assert [v["volumeID"] for v in (await control.get(
        "/volumes", headers={"X-API-Key": "keyA"}
    )).json()] == [volume_id]
    assert [s["secretID"] for s in (await control.get(
        "/secrets", headers={"X-API-Key": "keyB"}
    )).json()] == [secret_id]
    assert [t["templateID"] for t in (await control.get(
        "/templates", headers={"X-API-Key": "keyA"}
    )).json()] == [template_id]

    # t2 (keyC) sees only t2 resources.
    assert [s["sandboxID"] for s in (await control.get(
        "/sandboxes", headers={"X-API-Key": "keyC"}
    )).json()] == [other_id]
    assert (await control.get("/volumes", headers={"X-API-Key": "keyC"})).json() == []

    # Admin sees everything.
    assert len((await control.get(
        "/sandboxes", headers={"X-API-Key": ADMIN}
    )).json()) == 2
    assert len((await control.get(
        "/volumes", headers={"X-API-Key": ADMIN}
    )).json()) == 1
    assert len((await control.get(
        "/secrets", headers={"X-API-Key": ADMIN}
    )).json()) == 1
    assert len((await control.get(
        "/templates", headers={"X-API-Key": ADMIN}
    )).json()) == 1


async def test_cross_tenant_single_resource_returns_404(control):
    vol = await control.post(
        "/volumes", headers={"X-API-Key": "keyC"}, json={"name": "t2-vol"}
    )
    volume_id = vol.json()["volumeID"]
    sec = await control.post(
        "/secrets", headers={"X-API-Key": "keyC"}, json={"name": "t2-secret", "value": "v"}
    )
    secret_id = sec.json()["secretID"]
    tpl = await control.post(
        "/v3/templates", headers={"X-API-Key": "keyC"}, json={"name": "t2-tpl"}
    )
    template_id = tpl.json()["templateID"]
    sbx = await _create_sandbox(control, "keyC")
    sandbox_id = sbx.json()["sandboxID"]
    snap = await control.post(
        f"/sandboxes/{sandbox_id}/snapshots",
        headers={"X-API-Key": "keyC"},
        json={"name": "t2-snap"},
    )
    snapshot_id = snap.json()["snapshotID"]

    # Identical 404 to the missing-resource case: no existence leak.
    for path, label, expected_id in (
        (f"/sandboxes/{sandbox_id}", "Sandbox", sandbox_id),
        (f"/volumes/{volume_id}", "Volume", volume_id),
        (f"/secrets/{secret_id}", "Secret", secret_id),
        (
            f"/templates/{template_id}/files/abc123",
            "Template",
            template_id,
        ),
    ):
        resp = await control.get(path, headers={"X-API-Key": "keyA"})
        assert resp.status_code == 404
        assert resp.json() == {
            "code": 404,
            "message": f"{label} {expected_id} not found",
        }

    # Snapshots only expose a single-resource DELETE (GET /templates/{id}
    # is the unsupported catch-all).
    deleted = await control.delete(
        f"/templates/{snapshot_id}", headers={"X-API-Key": "keyA"}
    )
    assert deleted.status_code == 404
    assert deleted.json() == {"code": 404, "message": f"Snapshot {snapshot_id} not found"}

    # Same id via a missing sandbox id: message is identical.
    missing = await control.get("/sandboxes/sbx_missing", headers={"X-API-Key": "keyA"})
    assert missing.json() == {"code": 404, "message": "Sandbox sbx_missing not found"}


async def test_cross_resource_operations_403(control):
    # t1 key mounts a t2 volume.
    vol = await control.post(
        "/volumes", headers={"X-API-Key": "keyC"}, json={"name": "t2-vol"}
    )
    volume_id = vol.json()["volumeID"]
    resp = await _create_sandbox(
        control, "keyA", volumeMounts=[{"name": volume_id, "path": "/data"}]
    )
    assert resp.status_code == 403
    assert resp.json()["message"] == (
        f"Volume {volume_id} does not belong to this tenant"
    )

    # t1 key creates from a t2 snapshot.
    sbx = await _create_sandbox(control, "keyC")
    sandbox_id = sbx.json()["sandboxID"]
    snap = await control.post(
        f"/sandboxes/{sandbox_id}/snapshots",
        headers={"X-API-Key": "keyC"},
        json={"name": "t2-snap"},
    )
    snapshot_id = snap.json()["snapshotID"]
    resp = await _create_sandbox(control, "keyA", templateID=snapshot_id)
    assert resp.status_code == 403

    # t1 key creates from a t2 template.
    tpl = await control.post(
        "/v3/templates", headers={"X-API-Key": "keyC"}, json={"name": "t2-tpl"}
    )
    template_id = tpl.json()["templateID"]
    resp = await _create_sandbox(control, "keyA", templateID=template_id)
    assert resp.status_code == 403

    # t1 key injects a t2 secret.
    await control.post(
        "/secrets", headers={"X-API-Key": "keyC"}, json={"name": "t2-secret", "value": "v"}
    )
    resp = await _create_sandbox(
        control, "keyA", envVars={"TOKEN": "${t2-secret}"}
    )
    assert resp.status_code == 403
    assert resp.json()["message"] == "Secret t2-secret does not belong to this tenant"


async def test_admin_can_operate_any_tenant_resource(control):
    sbx = await _create_sandbox(control, "keyA")
    sandbox_id = sbx.json()["sandboxID"]
    vol = await control.post(
        "/volumes", headers={"X-API-Key": "keyA"}, json={"name": "t1-vol"}
    )
    volume_id = vol.json()["volumeID"]

    fetched = await control.get(
        f"/sandboxes/{sandbox_id}", headers={"X-API-Key": ADMIN}
    )
    assert fetched.status_code == 200
    assert fetched.json()["sandboxID"] == sandbox_id

    deleted = await control.delete(
        f"/volumes/{volume_id}", headers={"X-API-Key": ADMIN}
    )
    assert deleted.status_code == 204
    assert (
        await control.get(f"/volumes/{volume_id}", headers={"X-API-Key": ADMIN})
    ).status_code == 404

    # Admin is exempt from tenant limits: can create past a t1 cap.
    made = await _create_sandbox(control, ADMIN)
    assert made.status_code == 201
    assert (await control.get(
        f"/sandboxes/{made.json()['sandboxID']}", headers={"X-API-Key": ADMIN}
    )).status_code == 200


async def test_compat_mode_unchanged(make_apps):
    control, _envd = make_apps(
        control_settings=Settings(
            api_keys=("key1", "key2"),
            max_sandboxes=50,
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        )
    )
    async with await _client(control) as client:
        created = await _create_sandbox(client, "key1")
        assert created.status_code == 201
        sandbox_id = created.json()["sandboxID"]
        # No tenant configured: every key sees and operates everything.
        listed = await client.get("/sandboxes", headers={"X-API-Key": "key2"})
        assert [s["sandboxID"] for s in listed.json()] == [sandbox_id]
        got = await client.get(
            f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "key2"}
        )
        assert got.status_code == 200


async def test_tenant_quota_503_and_isolation(make_apps):
    control, _envd = make_apps(
        control_settings=_tenant_settings(
            tenant_limits={
                T1: {"max_sandboxes": 1},
                T2: {"max_sandboxes": 1},
            }
        )
    )
    async with await _client(control) as client:
        first = await _create_sandbox(client, "keyA")
        assert first.status_code == 201
        second = await _create_sandbox(client, "keyB")
        assert second.status_code == 503
        assert second.json() == {"code": 503, "message": "tenant quota exceeded"}
        other = await _create_sandbox(client, "keyC")
        assert other.status_code == 201


async def test_unconfigured_tenant_limits_global_only(make_apps):
    control, _envd = make_apps(control_settings=_tenant_settings())
    async with await _client(control) as client:
        first = await _create_sandbox(client, "keyA")
        assert first.status_code == 201
        second = await _create_sandbox(client, "keyB")
        assert second.status_code == 201


async def test_tenant_rate_limit_not_just_per_key(make_apps):
    control, _envd = make_apps(
        control_settings=_tenant_settings(
            tenant_rate_limits={T1: 1},
            create_rate_limit_per_min=120,
        )
    )
    async with await _client(control) as client:
        first = await _create_sandbox(client, "keyA")
        assert first.status_code == 201
        # keyB shares t1's budget: blocked by the tenant limiter.
        second = await _create_sandbox(client, "keyB")
        assert second.status_code == 429
        # t2 has no per-tenant rate configured: falls back to global.
        other = await _create_sandbox(client, "keyC")
        assert other.status_code == 201


async def test_sandbox_delete_checks_ownership(control):
    sbx = await _create_sandbox(control, "keyA")
    sandbox_id = sbx.json()["sandboxID"]
    resp = await control.delete(
        f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "keyC"}
    )
    assert resp.status_code == 404
    # Resource still exists for the owner.
    assert (
        await control.get(
            f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "keyA"}
        )
    ).status_code == 200


async def test_unmapped_key_fail_closed_in_isolation_mode(make_apps):
    """A legacy key that maps to no tenant is rejected on every resource."""
    control, _envd = make_apps(
        control_settings=_tenant_settings(api_keys=("legacy-key",))
    )
    async with await _client(control) as client:
        sbx = await _create_sandbox(client, "keyA")
        assert sbx.status_code == 201
        sandbox_id = sbx.json()["sandboxID"]
        vol = await client.post(
            "/volumes", headers={"X-API-Key": "keyA"}, json={"name": "t1-vol"}
        )
        assert vol.status_code == 201
        volume_id = vol.json()["volumeID"]
        sec = await client.post(
            "/secrets",
            headers={"X-API-Key": "keyA"},
            json={"name": "t1-secret", "value": "v"},
        )
        assert sec.status_code == 201
        secret_id = sec.json()["secretID"]
        tpl = await client.post(
            "/v3/templates", headers={"X-API-Key": "keyA"}, json={"name": "t1-tpl"}
        )
        assert tpl.status_code == 202
        template_id = tpl.json()["templateID"]

        forbidden = {"code": 403, "message": "API key is not mapped to any tenant"}
        # Lists are rejected wholesale (no partial visibility).
        for path in ("/sandboxes", "/volumes", "/secrets", "/templates", "/nodes"):
            resp = await client.get(path, headers={"X-API-Key": "legacy-key"})
            assert resp.status_code == 403
            assert resp.json() == forbidden
        # Single-resource reads and deletes are rejected.
        for path in (
            f"/sandboxes/{sandbox_id}",
            f"/volumes/{volume_id}",
            f"/secrets/{secret_id}",
            f"/templates/{template_id}/files/abc123",
        ):
            assert (
                await client.get(path, headers={"X-API-Key": "legacy-key"})
            ).status_code == 403
        for path in (f"/sandboxes/{sandbox_id}", f"/volumes/{volume_id}"):
            assert (
                await client.delete(path, headers={"X-API-Key": "legacy-key"})
            ).status_code == 403
        # Creates (including secret-injection attempts) are rejected, so the
        # key cannot create unowned resources either.
        assert (
            await _create_sandbox(
                client, "legacy-key", envVars={"TOKEN": "${t1-secret}"}
            )
        ).status_code == 403
        assert (
            await client.post(
                "/volumes",
                headers={"X-API-Key": "legacy-key"},
                json={"name": "legacy-vol"},
            )
        ).status_code == 403
        # The tenant's resources are untouched by the rejected requests.
        assert (
            await client.get(
                f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "keyA"}
            )
        ).status_code == 200
        assert (
            await client.get(
                f"/volumes/{volume_id}", headers={"X-API-Key": "keyA"}
            )
        ).status_code == 200


async def test_unmapped_key_cannot_bypass_tenant_quota_or_rate_limit(make_apps):
    control, _envd = make_apps(
        control_settings=_tenant_settings(
            api_keys=("legacy-key",),
            tenant_limits={T1: {"max_sandboxes": 1}},
        )
    )
    async with await _client(control) as client:
        assert (
            await _create_sandbox(client, "legacy-key")
        ).status_code == 403
        first = await _create_sandbox(client, "keyA")
        assert first.status_code == 201
        second = await _create_sandbox(client, "keyB")
        assert second.status_code == 503
        assert second.json() == {"code": 503, "message": "tenant quota exceeded"}

    control, _envd = make_apps(
        control_settings=_tenant_settings(
            api_keys=("legacy-key",),
            tenant_rate_limits={T1: 1},
        )
    )
    async with await _client(control) as client:
        first = await _create_sandbox(client, "keyA")
        assert first.status_code == 201
        second = await _create_sandbox(client, "keyB")
        assert second.status_code == 429


async def test_template_trigger_status_cross_tenant_404_matches_missing(make_apps):
    control, _envd = make_apps(control_settings=_tenant_settings())
    async with await _client(control) as client:
        tpl = await client.post(
            "/v3/templates", headers={"X-API-Key": "keyC"}, json={"name": "t2-tpl"}
        )
        assert tpl.status_code == 202
        template_id = tpl.json()["templateID"]
        build_id = tpl.json()["buildID"]

        missing = await client.get(
            "/templates/tpl_missing/builds/bld_missing/status",
            headers={"X-API-Key": "keyA"},
        )
        assert missing.status_code == 404
        assert missing.json() == {
            "code": 404,
            "message": "Template tpl_missing not found",
        }

        # Cross-tenant trigger and status return the same 404 as a missing
        # template: the attacker cannot tell "exists, not mine" apart from
        # "does not exist".
        status = await client.get(
            f"/templates/{template_id}/builds/{build_id}/status",
            headers={"X-API-Key": "keyA"},
        )
        assert status.status_code == 404
        assert status.json() == {
            "code": 404,
            "message": f"Template {template_id} not found",
        }
        trigger = await client.post(
            f"/v2/templates/{template_id}/builds/{build_id}",
            headers={"X-API-Key": "keyA"},
            json={"fromImage": "python:3.11-slim", "steps": []},
        )
        assert trigger.status_code == 404
        assert trigger.json() == {
            "code": 404,
            "message": f"Template {template_id} not found",
        }

        # An owned template with a missing build keeps the build-scoped
        # message (only reachable for the owner's own resources).
        owned = await client.get(
            f"/templates/{template_id}/builds/bld_unknown/status",
            headers={"X-API-Key": "keyC"},
        )
        assert owned.status_code == 404
        assert owned.json() == {
            "code": 404,
            "message": "Template build bld_unknown not found",
        }
