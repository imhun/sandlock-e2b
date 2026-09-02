"""E9.3: eviction candidate selection and eviction notices.

Selection is a pure function of registry state: running + idle only, sorted
priority (low first) -> idle oldest -> tenant weight -> sandbox id, scoped to
the requester's tenant unless admin / ``eviction_cross_tenant`` is on. Notices
are bounded and expire so the notification table cannot grow forever.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import control_plane.registry.manager as manager_mod
from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry
from gateway_common.timeutil import utcnow


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
        sandbox_idle_threshold_s=600,
        eviction_min_interval_s=0,
        eviction_notice_ttl_s=3600,
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


def _make_idle(registry, record, *, seconds=1200, stamp=None):
    """Backdate a record so it looks idle; return the stamp used."""
    moment = stamp or utcnow() - timedelta(seconds=seconds)
    record.last_active_at = moment
    registry.save(record)
    return moment


# -- ordering -------------------------------------------------------------


def test_eviction_candidates_sorted_by_priority_then_idle_oldest(workspace):
    registry = SandboxRegistry(_settings())
    newest_low = _create(registry, sandbox_id="sbx_a", priority=0)
    older_high = _create(registry, sandbox_id="sbx_b", priority=9)
    newest_high = _create(registry, sandbox_id="sbx_c", priority=9)
    oldest_low = _create(registry, sandbox_id="sbx_d", priority=0)
    _make_idle(registry, newest_low, seconds=700)
    _make_idle(registry, oldest_low, seconds=1000)
    _make_idle(registry, older_high, seconds=900)
    _make_idle(registry, newest_high, seconds=800)

    picked = registry.eviction_candidates()
    assert [r.sandbox_id for r in picked] == [
        "sbx_d",  # priority 0, idle longest
        "sbx_a",  # priority 0, idle shorter
        "sbx_b",  # priority 9, idle oldest
        "sbx_c",  # priority 9, idle newest
    ]


def test_same_priority_same_idle_falls_back_to_sandbox_id(workspace):
    registry = SandboxRegistry(_settings())
    stamp = utcnow() - timedelta(seconds=900)
    zz = _create(registry, sandbox_id="sbx_zz", priority=3)
    aa = _create(registry, sandbox_id="sbx_aa", priority=3)
    _make_idle(registry, zz, stamp=stamp)
    _make_idle(registry, aa, stamp=stamp)

    picked = registry.eviction_candidates()
    assert [r.sandbox_id for r in picked] == ["sbx_aa", "sbx_zz"]


def test_configured_small_quota_tenant_evicted_before_unconfigured(workspace):
    settings = _settings(
        tenant_limits={
            "t_small": {"max_sandboxes": 1},
            "t_tiny": {"max_sandboxes": 5},
        }
    )
    registry = SandboxRegistry(settings)
    unconf = _create(registry, sandbox_id="sbx_free", tenant_id="t_unlimited")
    tiny = _create(registry, sandbox_id="sbx_tiny", tenant_id="t_tiny")
    small = _create(registry, sandbox_id="sbx_small", tenant_id="t_small")
    stamp = utcnow() - timedelta(seconds=900)
    for record in (unconf, tiny, small):
        _make_idle(registry, record, stamp=stamp)

    picked = registry.eviction_candidates(is_admin=True)
    assert [r.sandbox_id for r in picked] == [
        "sbx_small",  # configured max_sandboxes=1 first
        "sbx_tiny",  # configured max_sandboxes=5 second
        "sbx_free",  # no configured limit last
    ]


# -- filtering ------------------------------------------------------------


def test_paused_and_orphaned_records_are_never_candidates(workspace):
    registry = SandboxRegistry(_settings())
    paused = _create(registry, sandbox_id="sbx_paused")
    _make_idle(registry, paused)
    registry.pause(paused)
    orphaned = _create(registry, sandbox_id="sbx_orphan")
    orphaned.node_id = "node_lost"
    registry.save(orphaned)
    registry.mark_orphaned("node_lost")
    _make_idle(registry, orphaned)

    assert registry.eviction_candidates() == []


def test_busy_and_not_yet_idle_records_are_excluded(workspace):
    registry = SandboxRegistry(_settings(sandbox_idle_threshold_s=300))
    busy = _create(registry, sandbox_id="sbx_busy")  # active right now
    _make_idle(registry, busy, seconds=10)  # below the 300 s threshold

    assert registry.eviction_candidates() == []


def test_zero_idle_threshold_disables_all_candidates(workspace):
    registry = SandboxRegistry(_settings(sandbox_idle_threshold_s=0))
    record = _create(registry, sandbox_id="sbx_a")
    _make_idle(registry, record, seconds=900)

    assert registry.eviction_candidates() == []


def test_eviction_disabled_returns_no_candidates(workspace):
    registry = SandboxRegistry(_settings(eviction_enabled=False))
    record = _create(registry, sandbox_id="sbx_a")
    _make_idle(registry, record)

    assert registry.eviction_candidates() == []


def test_limit_truncates_candidates(workspace):
    registry = SandboxRegistry(_settings())
    stamp = utcnow() - timedelta(seconds=900)
    for suffix in ("a", "b", "c", "d"):
        _make_idle(registry, _create(registry, sandbox_id=f"sbx_{suffix}"), stamp=stamp)

    assert [r.sandbox_id for r in registry.eviction_candidates(limit=3)] == [
        "sbx_a",
        "sbx_b",
        "sbx_c",
    ]
    assert [r.sandbox_id for r in registry.eviction_candidates(limit=None)] == [
        "sbx_a",
        "sbx_b",
        "sbx_c",
        "sbx_d",
    ]


# -- cross-tenant protection ---------------------------------------------


def test_default_tenant_scope_never_crosses_tenants(workspace):
    registry = SandboxRegistry(_settings())
    mine = _create(registry, sandbox_id="sbx_mine", tenant_id="t1")
    _create(registry, sandbox_id="sbx_other", tenant_id="t2")
    _create(registry, sandbox_id="sbx_unowned", tenant_id=None)
    _make_idle(registry, mine)
    stamp = utcnow() - timedelta(seconds=900)
    for sid in ("sbx_other", "sbx_unowned"):
        record = registry.get(sid)
        _make_idle(registry, record, stamp=stamp)

    picked = registry.eviction_candidates(tenant_id="t1")
    assert [r.sandbox_id for r in picked] == ["sbx_mine"]

    # A no-tenant (compatible-mode) requester only sees unowned records.
    picked = registry.eviction_candidates(tenant_id=None)
    assert [r.sandbox_id for r in picked] == ["sbx_unowned"]


def test_admin_and_cross_tenant_switch_may_evict_other_tenants(workspace):
    registry = SandboxRegistry(_settings())
    stamp = utcnow() - timedelta(seconds=900)
    for sid, tenant in (("sbx_t1", "t1"), ("sbx_t2", "t2"), ("sbx_none", None)):
        _make_idle(
            registry,
            _create(registry, sandbox_id=sid, tenant_id=tenant),
            stamp=stamp,
        )

    assert {r.sandbox_id for r in registry.eviction_candidates(tenant_id="t1")} == {
        "sbx_t1"
    }
    assert {r.sandbox_id for r in registry.eviction_candidates(tenant_id="t1", is_admin=True)} == {
        "sbx_t1",
        "sbx_t2",
        "sbx_none",
    }
    cross = SandboxRegistry(_settings(eviction_cross_tenant=True))
    for record in (registry.get("sbx_t1"), registry.get("sbx_t2"), registry.get("sbx_none")):
        _create(cross, sandbox_id=record.sandbox_id, tenant_id=record.tenant_id)
        _make_idle(cross, cross.get(record.sandbox_id), stamp=stamp)
    assert {r.sandbox_id for r in cross.eviction_candidates(tenant_id="t1")} == {
        "sbx_t1",
        "sbx_t2",
        "sbx_none",
    }


# -- eviction notices -----------------------------------------------------


def test_notice_round_trip_and_lazy_expiry(workspace):
    registry = SandboxRegistry(_settings(eviction_notice_ttl_s=1))
    registry._eviction_clock = lambda: 1000.0
    registry.record_eviction("sbx_gone", tenant_id="t1")

    notice = registry.eviction_notice("sbx_gone")
    assert notice is not None
    assert notice["reason"] == "evicted-idle"
    assert notice["tenant_id"] == "t1"

    registry._eviction_clock = lambda: 1002.0
    assert registry.eviction_notice("sbx_gone") is None
    assert registry.eviction_notice("sbx_never") is None


def test_notice_round_trip_via_redis_shared_store(workspace):
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    writer = SandboxRegistry(
        _settings(eviction_notice_ttl_s=3600),
        redis_client=fakeredis.FakeRedis(server=server),
    )
    writer.record_eviction("sbx_gone", tenant_id="t1")

    reader = SandboxRegistry(
        _settings(eviction_notice_ttl_s=3600),
        redis_client=fakeredis.FakeRedis(server=server),
    )
    notice = reader.eviction_notice("sbx_gone")
    assert notice is not None
    assert notice["reason"] == "evicted-idle"
    assert notice["tenant_id"] == "t1"
    # The notice key gets a real Redis TTL (not just a lazy in-memory deadline).
    ttl = reader._redis.ttl("e2b:evicted:sbx_gone")
    assert ttl is not None and ttl > 0


def test_in_memory_notice_table_has_a_capacity_cap(workspace, monkeypatch):
    registry = SandboxRegistry(_settings(eviction_notice_ttl_s=3600))
    monkeypatch.setattr(manager_mod, "_MAX_EVICTION_NOTICES", 4)
    for index in range(6):
        registry.record_eviction(f"sbx_{index}")

    # Only the newest notices survive; the oldest are dropped at the cap.
    assert len(registry._eviction_notices) == 4
    for index in (0, 1):
        assert registry.eviction_notice(f"sbx_{index}") is None
    for index in (2, 3, 4, 5):
        assert registry.eviction_notice(f"sbx_{index}") is not None
