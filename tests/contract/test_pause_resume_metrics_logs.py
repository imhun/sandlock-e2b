"""Pause/resume, metrics and logs contract tests."""

from __future__ import annotations


async def _create(control_client):
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


async def test_pause_resume_lifecycle(control_client):
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]

    paused = await control_client.post(
        f"/sandboxes/{sid}/pause", headers={"X-API-Key": "local-key"}, json={"memory": True}
    )
    assert paused.status_code == 204

    again = await control_client.post(
        f"/sandboxes/{sid}/pause", headers={"X-API-Key": "local-key"}, json={}
    )
    assert again.status_code == 409

    info = await control_client.get(f"/sandboxes/{sid}", headers={"X-API-Key": "local-key"})
    assert info.json()["state"] == "paused"

    resumed = await control_client.post(
        f"/sandboxes/{sid}/resume", headers={"X-API-Key": "local-key"}, json={}
    )
    assert resumed.status_code == 204
    info = await control_client.get(f"/sandboxes/{sid}", headers={"X-API-Key": "local-key"})
    assert info.json()["state"] == "running"

    conflict = await control_client.post(
        f"/sandboxes/{sid}/resume", headers={"X-API-Key": "local-key"}, json={}
    )
    assert conflict.status_code == 409


async def test_connect_resumes_paused_sandbox(control_client):
    """Sandbox.connect() auto-resumes: the persisted state must flip back to
    running (regression: the resumed record was previously overwritten by
    connect()'s re-read of the stale store payload)."""
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]

    paused = await control_client.post(
        f"/sandboxes/{sid}/pause", headers={"X-API-Key": "local-key"}, json={}
    )
    assert paused.status_code == 204

    connected = await control_client.post(
        f"/sandboxes/{sid}/connect", headers={"X-API-Key": "local-key"}, json={}
    )
    assert connected.status_code == 200

    info = await control_client.get(f"/sandboxes/{sid}", headers={"X-API-Key": "local-key"})
    assert info.json()["state"] == "running"


async def test_metrics_shape(control_client):
    sandbox = await _create(control_client)
    response = await control_client.get(
        f"/sandboxes/{sandbox['sandboxID']}/metrics",
        headers={"X-API-Key": "local-key"},
    )
    assert response.status_code == 200
    metric = response.json()[0]
    assert set(metric) >= {
        "cpuCount",
        "cpuUsedPct",
        "memTotal",
        "memUsed",
        "diskTotal",
        "diskUsed",
        "timestamp",
    }
    assert metric["memTotal"] == 1024 * 1024 * 1024


async def test_logs_shape(control_client):
    sandbox = await _create(control_client)
    response = await control_client.get(
        f"/sandboxes/{sandbox['sandboxID']}/logs",
        headers={"X-API-Key": "local-key"},
    )
    assert response.status_code == 200
    logs = response.json()
    assert isinstance(logs, list)
    assert any(log["line"] == "sandbox created" for log in logs)
