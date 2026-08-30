"""Control-plane scaling capabilities: draining, fleet metrics and the
adaptive idempotent create flow (X-Sandbox-Id fast/slow paths)."""

from __future__ import annotations

import httpx

from control_plane.config import Settings as ControlSettings


async def _client(control):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    )


async def _create(control, **headers):
    client = await _client(control)
    async with client:
        return await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", **headers},
            json={"templateID": "base", "timeout": 300, "envVars": {}},
        )


async def test_idempotent_create_fast_path(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": "sbx_idem0001"},
            json={"templateID": "base", "timeout": 300, "envVars": {}},
        )
        assert resp.status_code == 201
        assert resp.json()["sandboxID"] == "sbx_idem0001"

        # Retry with the same ID returns the existing sandbox immediately.
        retry = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": "sbx_idem0001"},
            json={"templateID": "base", "timeout": 300, "envVars": {}},
        )
        assert retry.status_code == 201
        assert retry.json()["sandboxID"] == "sbx_idem0001"


async def test_invalid_sandbox_id_rejected(apps):
    control, _ = apps
    resp = await _create(control, **{"X-Sandbox-Id": "../../etc/passwd"})
    assert resp.status_code == 400


async def test_cold_image_without_id_returns_428(make_apps):
    control, _ = make_apps(
        control_settings=ControlSettings(
            api_keys=("local-key",),
            base_image="127.0.0.1:1/nope:latest",
            executor="sandlock",
        )
    )
    resp = await _create(control)
    assert resp.status_code == 428
    body = resp.json()
    assert body["code"] == 428
    assert "warm_required" in body["message"]


async def test_cold_image_slow_path_warm_failure_leaves_no_orphan(make_apps):
    control, _ = make_apps(
        control_settings=ControlSettings(
            api_keys=("local-key",),
            base_image="127.0.0.1:1/nope:latest",
            executor="sandlock",
        )
    )
    resp = await _create(control, **{"X-Sandbox-Id": "sbx_idem0002"})
    assert resp.status_code == 503
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        got = await client.get(
            "/sandboxes/sbx_idem0002", headers={"X-API-Key": "local-key"}
        )
        assert got.status_code == 404
        pending = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": "internal-key"}
        )
        assert pending.status_code == 200


async def test_drain_undrain_and_fleet_metrics(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        reg = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": "internal-key"},
            json={
                "nodeID": "worker-x",
                "address": "http://worker-x:49983",
                "totalMemoryMB": 2048,
                "totalCPUPercent": 200,
                "totalDiskMB": 4096,
                "totalProcesses": 128,
                "images": [],
                "labels": {"node-type": "container"},
            },
        )
        assert reg.status_code == 200

        drain = await client.post(
            "/internal/nodes/worker-x/drain",
            headers={"X-Internal-Key": "internal-key"},
        )
        assert drain.status_code == 200
        assert drain.json()["draining"] is True

        metrics = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": "internal-key"}
        )
        assert metrics.status_code == 200
        body = metrics.json()
        by_id = {n["nodeID"]: n for n in body["nodes"]}
        assert by_id["worker-x"]["draining"] is True
        assert by_id["worker-x"]["status"] == "healthy"
        assert body["fleet"]["memory"]["total"] >= 2048
        assert body["remainingSandboxCapacity"] is not None

        undrain = await client.post(
            "/internal/nodes/worker-x/undrain",
            headers={"X-Internal-Key": "internal-key"},
        )
        assert undrain.status_code == 204
        metrics2 = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": "internal-key"}
        )
        by_id2 = {n["nodeID"]: n for n in metrics2.json()["nodes"]}
        assert by_id2["worker-x"]["draining"] is False
