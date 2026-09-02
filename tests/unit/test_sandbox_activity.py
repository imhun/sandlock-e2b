"""E9.1: sandbox activity accounting (the input to idle-based eviction).

Idle detection is only useful if the timestamp survives a store round trip,
cannot move backwards, is throttled on the write path and is actually fed by
the worker that sees in-sandbox traffic. Each test pins one of those.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import (
    PRIORITY_DEFAULT,
    SandboxRecord,
    SandboxRegistry,
    UnknownSandboxError,
)
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common.timeutil import to_iso_z, utcnow


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry, **kw):
    kwargs = dict(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    kwargs.update(kw)
    return registry.create(**kwargs)


# -- record level ---------------------------------------------------------


def test_new_record_starts_active_at_default_priority():
    before = utcnow()
    record = SandboxRecord(
        template_id="base", sandbox_id="sbx_a", client_id="cli_a"
    )
    assert record.priority == PRIORITY_DEFAULT
    assert before <= record.last_active_at <= utcnow()
    assert record.is_idle(300) is False


def test_touch_never_moves_the_timestamp_backwards():
    now = utcnow()
    record = SandboxRecord(
        template_id="base",
        sandbox_id="sbx_a",
        client_id="cli_a",
        last_active_at=now,
    )
    assert record.touch(now - timedelta(seconds=30)) is False
    assert record.last_active_at == now
    assert record.touch(now + timedelta(seconds=5)) is True
    assert record.last_active_at == now + timedelta(seconds=5)


def test_naive_activity_time_is_treated_as_utc():
    naive = utcnow().replace(tzinfo=None) + timedelta(seconds=60)
    record = SandboxRecord(
        template_id="base",
        sandbox_id="sbx_a",
        client_id="cli_a",
        last_active_at=utcnow(),
    )
    assert record.touch(naive) is True
    assert record.last_active_at.tzinfo is not None


def test_is_idle_uses_the_configured_threshold():
    record = SandboxRecord(
        template_id="base",
        sandbox_id="sbx_a",
        client_id="cli_a",
        last_active_at=utcnow() - timedelta(seconds=400),
    )
    assert record.idle_seconds() == pytest.approx(400, abs=2)
    assert record.is_idle(300) is True
    assert record.is_idle(600) is False
    # threshold 0 = idleness disabled: nothing is ever an eviction candidate.
    assert record.is_idle(0) is False


def test_activity_and_priority_survive_a_storage_round_trip():
    record = SandboxRecord(
        template_id="base",
        sandbox_id="sbx_a",
        client_id="cli_a",
        last_active_at=utcnow().replace(microsecond=0),
        priority=2,
    )
    stored = record.to_storage_dict()
    assert stored["last_active_at"] == to_iso_z(record.last_active_at)
    assert stored["priority"] == 2

    restored = SandboxRecord.from_storage_dict(stored)
    assert restored.last_active_at == record.last_active_at
    assert restored.priority == 2


def test_legacy_record_without_activity_fields_defaults_to_started_at():
    """Records persisted before E9.1 must not look brand-new or crash."""
    stored = {
        "template_id": "base",
        "sandbox_id": "sbx_old",
        "client_id": "cli_old",
        "started_at": "2026-08-01T00:00:00.000Z",
        "end_at": "2026-08-01T00:05:00.000Z",
    }
    restored = SandboxRecord.from_storage_dict(stored)
    assert restored.last_active_at == restored.started_at
    assert restored.priority == PRIORITY_DEFAULT
    assert restored.is_idle(300) is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, PRIORITY_DEFAULT), ("abc", PRIORITY_DEFAULT), (99, 10), (-3, 0)],
)
def test_stored_priority_is_coerced_into_range(raw, expected):
    stored = SandboxRecord(
        template_id="base", sandbox_id="sbx_a", client_id="cli_a"
    ).to_storage_dict()
    stored["priority"] = raw
    assert SandboxRecord.from_storage_dict(stored).priority == expected


# -- registry level -------------------------------------------------------


def test_create_accepts_priority():
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_p", priority=9)
    assert record.priority == 9
    assert registry.get("sbx_p").priority == 9


def test_listed_payload_exposes_activity_fields():
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_l", priority=1)
    listed = record.as_listed()
    assert listed["priority"] == 1
    assert listed["lastActiveAt"] == to_iso_z(record.last_active_at)


def test_mark_active_throttles_shared_store_writes(workspace):
    client = fakeredis.FakeRedis(decode_responses=True)
    registry = SandboxRegistry(_settings(), redis_client=client)
    record = _create(registry, sandbox_id="sbx_throttle")
    record.node_id = "node_a"
    registry.save(record)

    # Backdate the stored stamp so the next mark is outside the throttle
    # window: the write must land in the shared store.
    interval = registry._settings.activity_persist_interval_s
    old = utcnow() - timedelta(seconds=interval + 5)
    record.last_active_at = old
    registry.save(record)
    stamp = old + timedelta(seconds=10)
    assert registry._record_store.get("sbx_throttle")["last_active_at"] == to_iso_z(old)
    assert registry.mark_active(record, when=stamp) is True
    assert registry._record_store.get("sbx_throttle")["last_active_at"] != to_iso_z(old)

    # A second mark right away updates memory only (write amplification is
    # the whole point of the throttle).
    persisted = registry._record_store.get("sbx_throttle")["last_active_at"]
    later = stamp + timedelta(seconds=1)
    assert registry.mark_active(record, when=later) is True
    assert record.last_active_at == later
    assert registry._record_store.get("sbx_throttle")["last_active_at"] == persisted


def test_apply_activity_report_merges_worker_timestamps(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_rep")
    record.node_id = "node_a"
    registry.save(record)
    stale = record.last_active_at

    reported = (utcnow() + timedelta(seconds=120)).timestamp()
    updated = registry.apply_activity_report(
        "node_a",
        {
            "sbx_rep": reported,
            "sbx_unknown": reported,  # not in the registry: ignored
            "sbx_other_node": reported,  # different node: ignored
            "sbx_rep_bad": "not-a-number",  # malformed: ignored
        },
    )
    assert updated == 1
    assert registry.get("sbx_rep").last_active_at > stale
    assert registry.get("sbx_rep").is_idle(300) is False


def test_apply_activity_report_ignores_malformed_payloads(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_x")
    record.node_id = "node_a"
    registry.save(record)
    before = record.last_active_at
    for payload in (None, {}, {"sbx_x": None}, {"sbx_x": 1e300}, {"../etc": 1}):
        assert registry.apply_activity_report("node_a", payload) == 0
    assert registry.get("sbx_x").last_active_at == before


def test_touch_raises_for_unknown_sandbox(workspace):
    registry = SandboxRegistry(_settings())
    with pytest.raises(UnknownSandboxError):
        registry.touch("sbx_missing")


# -- worker (envd) side ---------------------------------------------------


def test_runtime_registry_tracks_activity_per_sandbox(tmp_path):
    marks: list[tuple[str, float]] = []
    registry = RuntimeRegistry(tmp_path)
    registry.add_activity_callback(lambda sandbox_id, moment: marks.append((sandbox_id, moment)))
    registry.register(
        sandbox_id="sbx_act", access_token="tok", workspace_dir=str(tmp_path / "sbx_act")
    )

    # Unknown sandboxes are never marked (the map must not grow unbounded).
    registry.mark_active("sbx_never_registered")
    assert registry.activity_snapshot() == {}
    assert marks == []

    registry.mark_active("sbx_act")
    first = registry.activity_snapshot()
    assert list(first) == ["sbx_act"]
    assert marks == [("sbx_act", first["sbx_act"])]

    # Coalesced: a second mark inside the window reports nothing new.
    assert registry.mark_active("sbx_act") is None
    assert registry.activity_snapshot() == first
    assert len(marks) == 1

    registry.unregister("sbx_act")
    assert registry.activity_snapshot() == {}
