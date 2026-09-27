"""E7: the platform-account alert (``used``/``budget`` per node).

S2 already publishes the platform account to the node view (``platformDiskUsedMB``
/ ``platformDiskBudgetMB``, see ``docs/checkpoint-restore-e2b-half.md`` §6(f)),
and the decision behind it was "soft ledger, concurrent overshoot allowed, and
say so" -- but nothing *reads* the number, so a node whose platform account has
filled up is only noticed when a capture is refused. This pins the missing
consumer: a periodic scan that warns once per crossing, with a single flight so
two replicas cannot each warn.

The cases are deterministic on purpose: the fleet is a fake store of records
and the round is driven directly, so "exactly one line" is a fact about the
policy rather than about when the wall clock happened to tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from control_plane.app import create_app
from control_plane.config import Settings
from control_plane.registry.ledger_alert import (
    LEDGER_ALERT_RATIO_ENV,
    PlatformLedgerAlerter,
    ledger_alert_ratio,
)
from control_plane.registry.nodes import NodeRegistry

_logger = logging.getLogger("control_plane.registry.ledger_alert")


def _node(node_id: str, *, used_mb: int, budget_mb: int) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=node_id,
        platform_disk_used_mb=used_mb,
        platform_disk_budget_mb=budget_mb,
    )


class _FakeFleet:
    """A node view that answers one scripted row set, round after round."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.reads = 0

    def list(self, *, healthy_only: bool = False):
        self.reads += 1
        return list(self.rows)

    def set(self, rows: list[SimpleNamespace]) -> None:
        self.rows = list(rows)


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _logger.name]


def test_the_threshold_defaults_to_08_and_zero_turns_alerting_off(monkeypatch):
    monkeypatch.delenv(LEDGER_ALERT_RATIO_ENV, raising=False)
    assert ledger_alert_ratio() == 0.8

    monkeypatch.setenv(LEDGER_ALERT_RATIO_ENV, "0.5")
    assert ledger_alert_ratio() == 0.5

    monkeypatch.setenv(LEDGER_ALERT_RATIO_ENV, "0")
    assert ledger_alert_ratio() == 0.0

    assert ledger_alert_ratio(SimpleNamespace(ledger_alert_ratio=0.25)) == 0.25


def test_a_node_over_the_ratio_is_named_once(caplog):
    caplog.set_level(logging.WARNING, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=900, budget_mb=1000)])

    alerter.scan(fleet)

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 900 MiB of 1000 MiB "
        "(ratio 0.90 >= 0.80)"
    ]
    assert fleet.reads == 1


def test_a_node_over_the_ratio_does_not_repeat_every_round(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=950, budget_mb=1000)])

    alerter.scan(fleet)
    alerter.scan(fleet)
    alerter.scan(fleet)

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 950 MiB of 1000 MiB "
        "(ratio 0.95 >= 0.80)"
    ]


def test_a_node_under_the_ratio_never_warns(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=799, budget_mb=1000)])

    alerter.scan(fleet)
    alerter.scan(fleet)

    assert _messages(caplog) == []


def test_exactly_the_ratio_is_over(caplog):
    caplog.set_level(logging.WARNING, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    alerter.scan(_FakeFleet([_node("e2b-worker-1", used_mb=800, budget_mb=1000)]))

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 800 MiB of 1000 MiB "
        "(ratio 0.80 >= 0.80)"
    ]


def test_a_budget_of_zero_is_unlimited_and_never_warns(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    alerter.scan(_FakeFleet([_node("e2b-worker-1", used_mb=10_000, budget_mb=0)]))

    assert _messages(caplog) == []


def test_leaving_and_re_entering_warns_again(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=900, budget_mb=1000)])

    alerter.scan(fleet)
    fleet.set([_node("e2b-worker-1", used_mb=500, budget_mb=1000)])
    alerter.scan(fleet)
    fleet.set([_node("e2b-worker-1", used_mb=950, budget_mb=1000)])
    alerter.scan(fleet)

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 900 MiB of 1000 MiB "
        "(ratio 0.90 >= 0.80)",
        "platform ledger back under budget: node e2b-worker-1 used 500 MiB of "
        "1000 MiB (ratio 0.50 < 0.80)",
        "platform ledger over budget: node e2b-worker-1 used 950 MiB of 1000 MiB "
        "(ratio 0.95 >= 0.80)",
    ]


def test_a_node_that_leaves_the_view_is_dropped_without_a_recovery_line(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=900, budget_mb=1000)])

    alerter.scan(fleet)
    fleet.set([])
    alerter.scan(fleet)
    fleet.set([_node("e2b-worker-1", used_mb=950, budget_mb=1000)])
    alerter.scan(fleet)

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 900 MiB of 1000 MiB "
        "(ratio 0.90 >= 0.80)",
        "platform ledger over budget: node e2b-worker-1 used 950 MiB of 1000 MiB "
        "(ratio 0.95 >= 0.80)",
    ]


def test_a_budget_that_is_removed_does_not_claim_a_recovery(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=900, budget_mb=1000)])

    alerter.scan(fleet)
    fleet.set([_node("e2b-worker-1", used_mb=900, budget_mb=0)])
    alerter.scan(fleet)
    fleet.set([_node("e2b-worker-1", used_mb=950, budget_mb=1000)])
    alerter.scan(fleet)

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 900 MiB of 1000 MiB "
        "(ratio 0.90 >= 0.80)",
        "platform ledger is no longer budgeted: node e2b-worker-1 had used 900 MiB",
        "platform ledger over budget: node e2b-worker-1 used 950 MiB of 1000 MiB "
        "(ratio 0.95 >= 0.80)",
    ]


@pytest.mark.asyncio
async def test_the_alert_round_is_single_flight(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(
        threshold_ratio=0.8,
        interval_seconds=0.02,
        claim=lambda: False,
    )
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=900, budget_mb=1000)])

    alerter.start(fleet)
    try:
        await asyncio.sleep(0.2)
    finally:
        await alerter.stop()

    assert _messages(caplog) == []
    assert fleet.reads == 0


@pytest.mark.asyncio
async def test_the_loop_warns_once_per_crossing(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8, interval_seconds=0.02)
    fleet = _FakeFleet([_node("e2b-worker-1", used_mb=900, budget_mb=1000)])

    alerter.start(fleet)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _messages(caplog):
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    finally:
        await alerter.stop()

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 900 MiB of 1000 MiB "
        "(ratio 0.90 >= 0.80)"
    ]


def test_a_fleet_that_cannot_be_listed_is_not_an_alert_source(caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    alerter = PlatformLedgerAlerter(threshold_ratio=0.8)

    alerter.scan(SimpleNamespace())

    assert _messages(caplog) == []


@pytest.mark.asyncio
async def test_the_app_starts_the_alert_scan(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=_logger.name)
    nodes = NodeRegistry()
    record = nodes.register(
        node_id="e2b-worker-1",
        address="http://worker-1:49984",
        total_memory_mb=0,
        total_cpu_percent=0,
        total_disk_mb=0,
        total_processes=0,
    )
    record.update_usage(platform_disk_used_mb=900, platform_disk_budget_mb=1000)
    nodes.publish(record)

    app = create_app(
        settings=Settings(
            api_keys=("local-key",),
            workspace_base=tmp_path,
            create_queue_timeout_s=0,
        ),
        runtime_registry=SimpleNamespace(unregister=lambda sandbox_id: None),
        nodes_registry=nodes,
        workspace_base=tmp_path,
    )
    async with app.router.lifespan_context(app):
        assert app.state.ledger_alerter is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _messages(caplog):
            await asyncio.sleep(0.02)

    assert _messages(caplog) == [
        "platform ledger over budget: node e2b-worker-1 used 900 MiB of 1000 MiB "
        "(ratio 0.90 >= 0.80)"
    ]
