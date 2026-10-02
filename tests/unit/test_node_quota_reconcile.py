"""N59's other half: the **shared** quota ledger heals on node re-registration.

The 2026-10-02 incident had two halves, and the money quote from the operator
was that the accounting was right the whole time: what held the quota were real
sandbox records for sandboxes nobody ever killed (four from Task 3's acceptance
window, four from a smoke loop whose ``kill()`` was cut short). On the
node-local shape a leaked sandbox holds its node slot indefinitely, and
``select_and_reserve`` gives up when the store refuses instead of trying the
next candidate -- so the fleet went from "a few leftovers" to
``503 No resources available`` everywhere.

* the **view** half already healed when a worker re-registered
  (``NodeRegistry.set_reserved``, fed by ``_rebuild_node_reservations``);
* the **ledger** half (``e2b:node:quota:<node>``) had no reconciliation path
  and no TTL, so a leaked reservation stayed until an operator deleted the hash
  by hand -- which is exactly what the operator had to do.

These cases pin the healing of the second half, and the two invariants that keep
it honest: the correction is named in the log, and a deployment with no shared
ledger is a no-op rather than an error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.api.internal import _rebuild_node_reservations
from control_plane.config import Settings as ControlSettings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry

NODE = "node_b"
SANDBOX = "sbx_reconcile"
KEY = "fleet-key"


def _registry(workspace: Path) -> SandboxRegistry:
    settings = ControlSettings(
        api_keys=("local-key",),
        internal_api_key=KEY,
        max_sandboxes=50,
        workspace_base=workspace / "workspaces",
        state_base=workspace / "state",
        shared_workspace_root=str(workspace / "shared"),
        trees_shared=False,
    )
    return SandboxRegistry(settings)


def _one_sandbox(registry: SandboxRegistry):
    record = registry.create(
        template_id="base",
        sandbox_id=SANDBOX,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    record.node_id = NODE
    registry.save(record)
    return record


def _dims(record) -> dict[str, int]:
    return {
        "memory": record.memory_mb,
        "cpu": record.cpu_count * 100,
        "disk": record.disk_size_mb,
        "processes": record.max_processes,
    }


def _request(nodes: NodeRegistry, registry: SandboxRegistry):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(nodes=nodes, registry=registry)
        )
    )


def _register(nodes: NodeRegistry) -> None:
    nodes.register(
        node_id=NODE,
        address="http://10.0.0.2:49983",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
    )


def test_a_leaked_ledger_row_is_reconciled_when_the_node_re_registers(
    workspace, caplog
) -> None:
    """A row that is *above* the records (the leak) is brought back down."""
    fakeredis = pytest.importorskip("fakeredis")
    registry = _registry(workspace)
    record = _one_sandbox(registry)
    nodes = NodeRegistry(heartbeat_timeout=600.0, redis_client=fakeredis.FakeRedis())
    _register(nodes)
    # Leaked reservations on this node, the live shape (`3072 = 3 x 1024` on the
    # wire, one of them the sandbox's own record).
    # Generous limits on purpose: this case is about the ledger healing, not
    # about a node filling up (the live fleet's own limits are 8192 MiB /
    # 1024 processes, which is why the leak showed up as "memory 3072").
    limits = {"memory": 65536, "cpu": 6400, "disk": 65536, "processes": 4096}
    for _ in range(3):
        assert nodes._quota_store.reserve(NODE, limits, _dims(record)) is True
    leaked = nodes._quota_store.get(NODE)
    assert leaked["memory"] == record.memory_mb * 3

    with caplog.at_level(logging.WARNING):
        _rebuild_node_reservations(_request(nodes, registry), nodes.get(NODE))

    # Both halves now say the same thing -- the record, once.
    assert nodes.get(NODE).reserved_memory_mb == record.memory_mb
    assert nodes._quota_store.get(NODE) == _dims(record)
    # ...and the correction is named, with the direction (negative = lowered).
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "reconciled" in message
        and "node_b" in message
        and f"-{record.memory_mb * 2}" in message
        for message in warnings
    ), warnings


def test_a_ledger_row_below_the_records_is_raised_to_them(workspace, caplog) -> None:
    """The other direction, so the two numbers cannot disagree either way.

    The shape is a *lost* ledger (the Redis row is gone while the sandbox
    records survived), which is the direction that over-sells.
    """
    fakeredis = pytest.importorskip("fakeredis")
    registry = _registry(workspace)
    record = _one_sandbox(registry)
    nodes = NodeRegistry(heartbeat_timeout=600.0, redis_client=fakeredis.FakeRedis())
    _register(nodes)
    assert nodes._quota_store.get(NODE) == {}

    with caplog.at_level(logging.WARNING):
        _rebuild_node_reservations(_request(nodes, registry), nodes.get(NODE))

    assert nodes.get(NODE).reserved_memory_mb == record.memory_mb
    assert nodes._quota_store.get(NODE) == _dims(record)


def test_a_ledger_that_already_agrees_is_left_alone(workspace, caplog) -> None:
    """No drift ⇒ no write and no warning (a reconcile that always writes is noise)."""
    fakeredis = pytest.importorskip("fakeredis")
    registry = _registry(workspace)
    record = _one_sandbox(registry)
    nodes = NodeRegistry(heartbeat_timeout=600.0, redis_client=fakeredis.FakeRedis())
    _register(nodes)
    dims = _dims(record)
    nodes._quota_store.reserve(
        NODE,
        {"memory": 65536, "cpu": 6400, "disk": 65536, "processes": 4096},
        dims,
    )
    before = nodes._quota_store.get(NODE)

    with caplog.at_level(logging.WARNING):
        _rebuild_node_reservations(_request(nodes, registry), nodes.get(NODE))

    assert nodes._quota_store.get(NODE) == before
    assert [
        r.message for r in caplog.records if r.levelno == logging.WARNING
    ] == []


def test_a_deployment_without_a_shared_ledger_is_a_no_op(workspace) -> None:
    """No Redis ⇒ no ledger to heal; the rebuild must not fail on its absence."""
    registry = _registry(workspace)
    _one_sandbox(registry)
    nodes = NodeRegistry(heartbeat_timeout=600.0)  # no redis_client
    _register(nodes)

    _rebuild_node_reservations(_request(nodes, registry), nodes.get(NODE))

    assert nodes.get(NODE).reserved_memory_mb > 0
    assert nodes._quota_store is None
    assert nodes.reconcile_quota_ledger(
        NODE, memory_mb=1, cpu_percent=1, disk_mb=1, processes=1
    ) == {}
