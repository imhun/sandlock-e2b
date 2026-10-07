"""N25/L2b: the worker's measured-disk report is the disk accounting, end to end.

The worker is the only party that can measure (it owns the mount) and the
control plane is the only party that owns the fleet's view of it, so the whole
feature lives or dies on the heartbeat contract between them. What that
contract carries is the measurement and a crossing notice -- **not** a
freeze: over budget means the writes are refused where the writes are (the
worker's zero file-size ceiling, plus ``ENOSPC`` for the entry-creating calls
the ceiling cannot reach), so the sandbox stays ``running`` and its owner can
still delete its way back inside. This drives the real endpoint and then reads
the registry back.
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.config import Settings as ControlSettings
from control_plane.registry.manager import SandboxRecord, SandboxRegistry


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

    assert response.status_code == 200
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

    assert response.status_code == 200
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

    assert response.status_code == 200
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


# -- N30: the contract itself, pinned where the semantics live ---------------

_BUDGET_MB = 64
_MIB = 1024 * 1024


@pytest.fixture()
def registry(make_apps) -> SandboxRegistry:
    """A control-plane registry that sells 64 MiB of workspace disk.

    These three cases drive ``enforce_disk_budget`` directly rather than
    through the heartbeat endpoint (the endpoint has its own cases above):
    what they pin is a property of the registry's semantics -- what a crossing
    must *not* do to the record -- which is exactly what a later change would
    regress, and it is the level at which the outward behaviour is decided.
    """
    control_app, _ = make_apps(
        control_settings=ControlSettings(
            api_keys=("local-key",),
            create_queue_timeout_s=0,
            default_disk_mb=_BUDGET_MB,
        )
    )
    return control_app.state.registry


def _sandbox_with_budget(registry: SandboxRegistry) -> SandboxRecord:
    """A live record sold exactly ``_BUDGET_MB`` of workspace disk."""
    return registry.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
        sandbox_id="sbx_budget_contract",
    )


def test_over_budget_records_the_measurement_and_keeps_the_sandbox_running(registry):
    """N30: over budget is *writes refused*, not a freeze -- and the number the
    fleet advertises is the worker's measurement, not a local walk.

    Both halves are the product: the record stays ``running`` so the reads, the
    exec and above all the deletes that give the space back all keep working,
    and ``diskUsed`` is exactly what the worker reported. The write side is
    refused where the writes are (the worker's zero file-size ceiling plus
    ``ENOSPC`` for the entry-creating calls a ceiling cannot reach), which this
    method never assumed the job of.
    """
    record = _sandbox_with_budget(registry)
    assert record.disk_size_mb == _BUDGET_MB
    measured = 128 * _MIB

    over = registry.enforce_disk_budget({record.sandbox_id: measured})

    assert [r.sandbox_id for r in over] == [record.sandbox_id]
    after = registry.get(record.sandbox_id)
    assert after.state == "running"
    assert after.workspace_disk_used_bytes == measured
    # The outward half: ``GET /sandboxes/{id}/metrics`` reads these two fields.
    assert after.sample_metric()["diskUsed"] == measured
    assert after.sample_metric()["diskTotal"] == _BUDGET_MB * _MIB


def test_an_in_budget_report_is_recorded_without_being_reported_as_a_crossing(
    registry,
):
    """The recording is the fleet's ledger, not only the reaction to a crossing.

    A number that only existed for the sandboxes that just crossed would leave
    every other sandbox unaccounted for.
    """
    record = _sandbox_with_budget(registry)

    assert registry.enforce_disk_budget({record.sandbox_id: 1_000_000}) == []
    after = registry.get(record.sandbox_id)
    assert after.state == "running"
    assert after.workspace_disk_used_bytes == 1_000_000
    assert after.sample_metric()["diskUsed"] == 1_000_000


def test_enforce_disk_budget_does_not_pause_or_release_anything(registry):
    """The reverse nail: nothing about the record's *standing* moves either.

    Written against the semantics that were deliberately removed -- freezing a
    sandbox over its disk also handed its reservation back (E9.2), which is
    what made the freeze stick. Both halves are pinned here so that neither can
    come back quietly.
    """
    record = _sandbox_with_budget(registry)
    before = registry.global_reserved()
    assert before["disk"] == _BUDGET_MB

    registry.enforce_disk_budget({record.sandbox_id: 999 * _MIB})

    assert registry.global_reserved() == before
    after = registry.get(record.sandbox_id)
    assert after.state == "running"
    assert after.quota_released is False
    assert [entry["line"] for entry in after.logs] == []
