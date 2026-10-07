"""E9.1: idle detection across the public API and the worker report path.

Idle-based eviction is only as good as its input, so this pins where
``lastActiveAt`` does and does not move: worker traffic and lifecycle calls
count, read-only polling does not, and a worker cannot mark another node's
sandbox active.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from gateway_common.timeutil import to_iso_z, utcnow


async def _create(control_client, **body):
    payload = {"templateID": "base", "timeout": 300}
    payload.update(body)
    response = await control_client.post(
        "/sandboxes", headers={"X-API-Key": "local-key"}, json=payload
    )
    assert response.status_code == 201
    return response.json()


def _backdate(registry, sandbox_id, seconds=600):
    """Make a record look idle and return the stamp it was given."""
    record = registry.get(sandbox_id)
    stamp = utcnow() - timedelta(seconds=seconds)
    record.last_active_at = stamp
    registry.save(record)
    return stamp


async def test_create_accepts_priority_and_reports_it(control_client):
    sandbox = await _create(control_client, priority=3)
    sid = sandbox["sandboxID"]

    detail = await control_client.get(
        f"/sandboxes/{sid}", headers={"X-API-Key": "local-key"}
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["priority"] == 3
    assert to_iso_z(utcnow())[:11] == body["lastActiveAt"][:11]

    listed = await control_client.get(
        "/v2/sandboxes", headers={"X-API-Key": "local-key"}
    )
    assert listed.status_code == 200
    entry = next(item for item in listed.json() if item["sandboxID"] == sid)
    assert entry["priority"] == 3


@pytest.mark.parametrize("bad", [11, -1, "5", 1.5, True])
async def test_create_rejects_out_of_range_priority(control_client, bad):
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300, "priority": bad},
    )
    assert response.status_code == 400
    assert response.json()["message"] == (
        "priority must be an integer between 0 and 10"
    )


async def test_default_priority_is_five(control_client):
    sandbox = await _create(control_client)
    detail = await control_client.get(
        f"/sandboxes/{sandbox['sandboxID']}", headers={"X-API-Key": "local-key"}
    )
    assert detail.json()["priority"] == 5


async def test_worker_traffic_marks_the_sandbox_active(apps, control_client, envd_client):
    control_app, _envd_app = apps
    registry = control_app.state.registry
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]
    stamp = _backdate(registry, sid)

    health = await envd_client.get(
        "/envs",
        headers={"E2b-Sandbox-Id": sid, "X-Access-Token": sandbox["envdAccessToken"]},
    )
    assert health.status_code == 200

    after = registry.get(sid).last_active_at
    assert after > stamp
    assert registry.get(sid).is_idle(registry._settings.sandbox_idle_threshold_s) is False


async def test_unauthenticated_worker_call_does_not_mark_active(
    apps, control_client, envd_client
):
    control_app, _envd_app = apps
    registry = control_app.state.registry
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]
    stamp = _backdate(registry, sid)

    rejected = await envd_client.get(
        "/envs", headers={"E2b-Sandbox-Id": sid, "X-Access-Token": "wrong"}
    )
    assert rejected.status_code == 401
    assert registry.get(sid).last_active_at == stamp


async def test_lifecycle_calls_mark_the_sandbox_active(control_client, apps):
    control_app, _ = apps
    registry = control_app.state.registry
    sid = (await _create(control_client))["sandboxID"]

    stamp = _backdate(registry, sid)
    timeout = await control_client.post(
        f"/sandboxes/{sid}/timeout",
        headers={"X-API-Key": "local-key"},
        json={"timeout": 600},
    )
    assert timeout.status_code == 204
    assert registry.get(sid).last_active_at > stamp

    stamp = _backdate(registry, sid)
    paused = await control_client.post(
        f"/sandboxes/{sid}/pause", headers={"X-API-Key": "local-key"}, json={}
    )
    assert paused.status_code == 204
    assert registry.get(sid).last_active_at > stamp

    stamp = _backdate(registry, sid)
    connected = await control_client.post(
        f"/sandboxes/{sid}/connect", headers={"X-API-Key": "local-key"}, json={}
    )
    assert connected.status_code == 200
    assert registry.get(sid).last_active_at > stamp


async def test_read_only_polling_does_not_mark_active(control_client, apps):
    """A monitoring loop must not keep an unused sandbox out of eviction reach."""
    control_app, _ = apps
    registry = control_app.state.registry
    sid = (await _create(control_client))["sandboxID"]
    stamp = _backdate(registry, sid)

    info = await control_client.get(
        f"/sandboxes/{sid}", headers={"X-API-Key": "local-key"}
    )
    assert info.status_code == 200
    metrics = await control_client.get(
        f"/sandboxes/{sid}/metrics", headers={"X-API-Key": "local-key"}
    )
    assert metrics.status_code == 200
    logs = await control_client.get(
        f"/sandboxes/{sid}/logs", headers={"X-API-Key": "local-key"}
    )
    assert logs.status_code == 200
    assert registry.get(sid).last_active_at == stamp


async def test_heartbeat_activity_report_marks_records(apps, control_client):
    control_app, _ = apps
    registry = control_app.state.registry
    nodes = control_app.state.nodes
    # Create first (the local node serves it), then move the record onto a
    # registered remote node so the heartbeat has an owner to accept.
    sid = (await _create(control_client))["sandboxID"]
    node = nodes.register(
        node_id="node_worker",
        address="http://127.0.0.1:39999",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
        images=[],
    )
    record = registry.get(sid)
    record.node_id = node.node_id
    registry.save(record)
    stamp = _backdate(registry, sid)

    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}
    reported = (stamp + timedelta(seconds=500)).timestamp()

    # Another node reporting for this sandbox must be ignored (spoofing the
    # idle signal of someone else's sandbox is how eviction gets defeated).
    foreign = await control_client.post(
        "/internal/nodes/node_other/heartbeat",
        headers=headers,
        json={"sandboxActivity": {sid: reported}},
    )
    assert foreign.status_code == 404
    assert registry.get(sid).last_active_at == stamp

    ok = await control_client.post(
        "/internal/nodes/node_worker/heartbeat",
        headers=headers,
        json={"sandboxActivity": {sid: reported, "sbx_unknown": reported}},
    )
    assert ok.status_code == 200
    assert registry.get(sid).last_active_at > stamp
    # A *stale* report never moves the stamp backwards.
    stale = await control_client.post(
        "/internal/nodes/node_worker/heartbeat",
        headers=headers,
        json={"sandboxActivity": {sid: stamp.timestamp()}},
    )
    assert stale.status_code == 200
    assert registry.get(sid).last_active_at > stamp


async def test_heartbeat_cpu_report_lands_on_the_record(apps, control_client):
    """N83 phase 0: the worker's *measured* CPU per sandbox is recorded.

    The number is summed by the sandbox's pooled uid, which covers the
    supervisor as well -- it is the one figure that shows what the platform
    spends on a sandbox's behalf. It is recorded, never enforced here; the
    internal node view exposes it so an operator (and phase 1) can see who is
    over their declared allowance.
    """
    control_app, _ = apps
    registry = control_app.state.registry
    nodes = control_app.state.nodes
    sid = (await _create(control_client))["sandboxID"]
    node = nodes.register(
        node_id="node_worker",
        address="http://127.0.0.1:39999",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
        images=[],
    )
    record = registry.get(sid)
    record.node_id = node.node_id
    registry.save(record)
    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}

    # A foreign node cannot report for this sandbox; unknown ids and malformed
    # values are dropped -- the number is a reading of a real measurement, so a
    # spoofed or nonsensical one must not land on a record.
    foreign = await control_client.post(
        "/internal/nodes/node_other/heartbeat",
        headers=headers,
        json={"sandboxCpu": {sid: 999.0}},
    )
    assert foreign.status_code == 404
    assert registry.get(sid).measured_cpu_percent is None

    ok = await control_client.post(
        "/internal/nodes/node_worker/heartbeat",
        headers=headers,
        json={
            "sandboxCpu": {
                sid: 380.0,
                "sbx_unknown": 12.0,
                "sbx_negative": -1.0,
                "sbx_nonsense": "hot",
            }
        },
    )
    assert ok.status_code == 200
    assert registry.get(sid).measured_cpu_percent == 380.0

    view = await control_client.get(
        f"/internal/nodes/{node.node_id}/sandboxes", headers=headers
    )
    assert view.status_code == 200
    body = view.json()
    assert body["sandboxIDs"] == [sid], "the reconcile snapshot is unchanged"
    assert body["sandboxes"] == [
        {"sandboxID": sid, "measuredCpuPercent": 380.0}
    ]


async def test_heartbeat_rejects_malformed_activity(apps, control_client):
    control_app, _ = apps
    nodes = control_app.state.nodes
    nodes.register(
        node_id="node_worker",
        address="http://127.0.0.1:39999",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
        images=[],
    )
    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}
    bad = await control_client.post(
        "/internal/nodes/node_worker/heartbeat",
        headers=headers,
        json={"sandboxActivity": "not-an-object"},
    )
    assert bad.status_code == 400
    assert bad.json()["message"] == "sandboxActivity must be a JSON object"


async def test_mcp_proxy_traffic_counts_as_activity(apps, control_client, envd_client, monkeypatch):
    """E9.1 gap: /mcp authenticates inline, so it must mark activity itself."""
    from types import SimpleNamespace

    import envd_service.http.mcp as mcp_module

    control_app, envd_app = apps
    registry = control_app.state.registry
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]
    envd_app.state.runtimes[sid] = SimpleNamespace(mcp_port=59999, mcp_token="mtok")

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, method, url, headers=None, content=None):
            assert url.startswith("http://127.0.0.1:59999/mcp")
            return httpx.Response(
                200, content=b'{"ok":true}', headers={"content-type": "application/json"}
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(mcp_module.httpx, "AsyncClient", _FakeClient)
    stamp = _backdate(registry, sid)

    wrong = await envd_client.post(
        "/mcp", headers={"E2b-Sandbox-Id": sid, "x-mcp-access-token": "nope"}, json={}
    )
    assert wrong.status_code == 401
    assert registry.get(sid).last_active_at == stamp

    served = await envd_client.post(
        "/mcp", headers={"E2b-Sandbox-Id": sid, "x-mcp-access-token": "mtok"}, json={}
    )
    assert served.status_code == 200
    assert await served.aread() == b'{"ok":true}'
    assert registry.get(sid).last_active_at > stamp
