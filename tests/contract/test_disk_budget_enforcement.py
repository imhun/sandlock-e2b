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


async def test_over_budget_report_pauses_the_sandbox(apps, control_client):
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

    # The freeze push cannot land (no agent at :39999) and that is *not* a
    # heartbeat failure: the record is paused and the retry rides the next
    # pulse, because rolling back would hand the reservation back to a
    # sandbox that is still writing.
    assert response.status_code == 204
    assert registry.get(sid).state == "paused"
    assert nodes.get("node_disk").reserved_disk_mb == 0
    assert registry.global_reserved()["disk"] == 0


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
