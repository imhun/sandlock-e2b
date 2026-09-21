"""N25/L2b: the worker's measured-disk report pauses a runaway, end to end.

The worker is the only party that can measure (it owns the mount) and the
control plane is the only party that can pause (it owns state), so the whole
feature lives or dies on the heartbeat contract between them. This drives the
real endpoint and then reads the registry back.
"""

from __future__ import annotations

import httpx
import pytest


async def _create(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


def _own_by(registry, nodes, sandbox_id: str):
    """Move a record onto a registered remote worker, as a real create would."""
    node = nodes.register(
        node_id="node_disk",
        address="http://127.0.0.1:39999",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
        images=[],
    )
    record = registry.get(sandbox_id)
    record.node_id = node.node_id
    registry.save(record)
    # The worker's slice has to hold the sandbox, or "the pause gave the slice
    # back" would be trivially true.
    nodes.set_reserved(
        node.node_id,
        memory_mb=record.memory_mb,
        cpu_percent=record.cpu_count * 100,
        disk_mb=record.disk_size_mb,
        processes=record.max_processes,
    )
    return record


async def test_over_budget_report_blocks_writes_and_never_pauses(apps, control_client):
    """N25 (2026-09-20): over budget is *no writes*, not a freeze.

    This test used to pin the opposite -- a paused record with its reservation
    handed back -- and that semantic was deliberately removed: freezing a
    sandbox over its disk took away the deletes that would bring it back
    inside, which is why the write side is enforced where the writes are (the
    worker turns the measured number into a zero `RLIMIT_FSIZE` plus `ENOSPC`
    for the entry-creating operations, §22.5.9). The control plane's half is
    the accounting and the warning, and *nothing else*:
    ``SandboxRegistry.enforce_disk_budget`` records and returns the crossing
    without touching the state.
    """
    control_app, _ = apps
    registry = control_app.state.registry
    nodes = control_app.state.nodes
    sid = (await _create(control_client))["sandboxID"]
    record = _own_by(registry, nodes, sid)

    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}
    over = record.disk_size_mb * 1024 * 1024 + 1
    response = await control_client.post(
        "/internal/nodes/node_disk/heartbeat",
        headers=headers,
        json={"sandboxDiskUsage": {sid: over}},
    )

    assert response.status_code == 204
    after = registry.get(sid)
    assert after.state == "running", "a crossing must not pause the sandbox"
    assert after.workspace_disk_used_bytes == over
    # The reservation is untouched too: the sandbox is still here and still
    # holding its capacity, so handing it back would be the same mistake in a
    # different register.
    assert nodes.get("node_disk").reserved_disk_mb == record.disk_size_mb
    assert registry.global_reserved()["disk"] == record.disk_size_mb
    # ...and no pause reason was written (the line the old semantic produced).
    assert [entry for entry in after.logs if "paused" in entry["line"]] == []


async def test_a_tree_within_budget_leaves_the_sandbox_running(apps, control_client):
    control_app, _ = apps
    registry = control_app.state.registry
    nodes = control_app.state.nodes
    sid = (await _create(control_client))["sandboxID"]
    record = _own_by(registry, nodes, sid)

    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}
    response = await control_client.post(
        "/internal/nodes/node_disk/heartbeat",
        headers=headers,
        json={"sandboxDiskUsage": {sid: record.disk_size_mb * 1024 * 1024}},
    )

    assert response.status_code == 204
    assert registry.get(sid).state == "running"
    assert nodes.get("node_disk").reserved_disk_mb == record.disk_size_mb


@pytest.mark.parametrize("bad", [[1, 2], "nope", 7])
async def test_a_malformed_disk_report_is_rejected(apps, control_client, bad):
    """A worker is untrusted input even when it holds the internal key."""
    control_app, _ = apps
    control_app.state.nodes.register(
        node_id="node_disk",
        address="http://127.0.0.1:39999",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
        images=[],
    )
    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}
    response = await control_client.post(
        "/internal/nodes/node_disk/heartbeat",
        headers=headers,
        json={"sandboxDiskUsage": bad},
    )
    assert response.status_code == 400
    assert response.json()["message"] == "sandboxDiskUsage must be a JSON object"


# -- N28/D: the measurement is the accounting -------------------------------


async def test_the_measurement_lands_on_the_record(apps, control_client):
    """Every report is recorded, not only the ones about to be paused.

    Without this the fleet's only per-sandbox disk number would exist exactly
    for the sandboxes that just got frozen -- and ``GET /sandboxes/{id}/
    metrics`` answered a flat ``diskUsed: 0`` for every remote sandbox, whose
    record carries no ``workspace_dir`` for the control plane to walk.
    """
    control_app, _ = apps
    registry = control_app.state.registry
    nodes = control_app.state.nodes
    sid = (await _create(control_client))["sandboxID"]
    record = _own_by(registry, nodes, sid)
    measured = record.disk_size_mb * 1024 * 1024 // 2

    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}
    response = await control_client.post(
        "/internal/nodes/node_disk/heartbeat",
        headers=headers,
        json={"sandboxDiskUsage": {sid: measured}},
    )

    assert response.status_code == 204
    assert registry.get(sid).workspace_disk_used_bytes == measured
    assert registry.get(sid).state == "running"
    metrics = await control_client.get(
        f"/sandboxes/{sid}/metrics", headers={"X-API-Key": "local-key"}
    )
    assert metrics.status_code == 200
    assert metrics.json()[-1]["diskUsed"] == measured


async def test_the_crossing_is_reported_and_clears_when_the_tree_comes_back(
    apps, control_client, caplog
):
    """The operator-facing half of the same rule (N25).

    Over budget is not silent and not a pause: the crossing is named once in
    the control plane's log, the fleet counter carries it, and the counter is
    rebuilt from the *current* report rather than accumulated -- so a sandbox
    that comes back inside (by deleting, which is exactly the operation a
    freeze would have taken away) stops being counted instead of lingering as
    a phantom overrun.
    """
    control_app, _ = apps
    registry = control_app.state.registry
    nodes = control_app.state.nodes
    sid = (await _create(control_client))["sandboxID"]
    record = _own_by(registry, nodes, sid)
    over_mib = record.disk_size_mb + 316

    headers = {"X-Internal-Key": control_app.state.settings.internal_api_key}
    await control_client.post(
        "/internal/nodes/node_disk/heartbeat",
        headers=headers,
        json={"sandboxDiskUsage": {sid: over_mib * 1024 * 1024}},
    )

    reported = registry.get(sid)
    assert reported.state == "running"
    assert reported.workspace_disk_used_bytes == over_mib * 1024 * 1024
    assert registry.disk_overrun_stats() == {"sandboxes": 1, "overMB": 316}
    assert "over its workspace budget" in caplog.text
    assert "writes are blocked until it is back inside" in caplog.text

    # Back inside: the counter drops it, and the number on the record follows
    # the report (it is the accounting, not a high-water mark).
    await control_client.post(
        "/internal/nodes/node_disk/heartbeat",
        headers=headers,
        json={"sandboxDiskUsage": {sid: record.disk_size_mb * 1024 * 1024}},
    )
    assert registry.disk_overrun_stats() == {"sandboxes": 0, "overMB": 0}
    assert registry.get(sid).workspace_disk_used_bytes == record.disk_size_mb * 1024 * 1024
