"""Pause/resume, metrics and logs contract tests."""

from __future__ import annotations

import httpx

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.connect.codec import encode_message
from envd_service.runtime.registry import RuntimeRegistry
from tests._memory_budget import per_sandbox_memory_mb


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
    # The per-sandbox ceiling the run is configured for, not a fixed 1 GiB.
    assert metric["memTotal"] == per_sandbox_memory_mb() * 1024 * 1024


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


async def _run_command(envd_client, sandbox, cmd, args) -> None:
    request = {
        "process": {"cmd": cmd, "args": args, "envs": {}, "cwd": "/"},
        "stdin": False,
    }
    response = await envd_client.post(
        "/process.Process/Start",
        headers={
            "E2b-Sandbox-Id": sandbox["sandboxID"],
            "X-Access-Token": sandbox["envdAccessToken"],
            "Content-Type": "application/connect+json",
        },
        content=encode_message(request),
    )
    assert response.status_code == 200


async def test_command_logs_are_read_from_the_state_base(workspace, monkeypatch):
    """N27: with ``E2B_STATE_BASE`` set the platform's own files live *beside*
    the tree base (``<export>/state``), so the control plane has to read the
    command log where the worker wrote it -- and must not quietly fall back to
    the tree base it used to be able to derive from the record."""
    state = workspace / "state"
    state.mkdir()
    monkeypatch.setenv("E2B_STATE_BASE", str(state))
    runtime_registry = RuntimeRegistry(workspace, state_base=state)
    control_app = create_control_app(
        settings=ControlSettings(api_keys=("local-key",), create_queue_timeout_s=0),
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    envd_app = create_envd_app(
        settings=EnvdSettings(executor="local"),
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control_app), base_url="http://test"
        ) as control_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=envd_app), base_url="http://test"
        ) as envd_client,
    ):
        created = await control_client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert created.status_code == 201
        sandbox = created.json()
        sandbox_id = sandbox["sandboxID"]
        await _run_command(envd_client, sandbox, "/bin/echo", ["state-base-log"])
        response = await control_client.get(
            f"/sandboxes/{sandbox_id}/logs",
            headers={"X-API-Key": "local-key"},
        )
        assert response.status_code == 200
        lines = [log["line"] for log in response.json()]
    assert lines.count("state-base-log") == 1
    # The file it read is the worker's, under the state base...
    assert (state / "_runtime" / sandbox_id / "command-logs.jsonl").is_file()
    # ...and the tree base carries no copy it could have read instead.
    assert not (workspace / "_runtime" / sandbox_id / "command-logs.jsonl").exists()
