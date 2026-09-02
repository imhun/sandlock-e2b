"""E9.3: evict_for_capacity action paths, throttle and quota effects.

The registry executes the record-level action (pause keeps the record and
returns its reservation; kill removes it through the delete chain) and
invokes injected callbacks so the API layer can park node capacity / tear
down the runtime without the registry knowing about nodes.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxRegistry,
)
from gateway_common.timeutil import utcnow


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=1024,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
        sandbox_idle_threshold_s=600,
        eviction_min_interval_s=0,
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


def _make_idle(registry, record, *, seconds=1200):
    record.last_active_at = utcnow() - timedelta(seconds=seconds)
    registry.save(record)


def _pool(registry) -> int:
    return registry._reserved_memory


def _full_registry(**overrides):
    """Two running sandboxes (one idle) filling a 1024 MB pool."""
    registry = SandboxRegistry(_settings(**overrides))
    victim = _create(registry, sandbox_id="sbx_victim")
    _create(registry, sandbox_id="sbx_other")
    _make_idle(registry, victim)
    with pytest.raises(ResourceUnavailableError):
        _create(registry, sandbox_id="sbx_third")
    return registry, victim


# -- kill path ------------------------------------------------------------


def test_kill_action_removes_victim_records_notice_and_releases_quota(workspace):
    registry, victim = _full_registry()
    assert _pool(registry) == 1024

    results = registry.evict_for_capacity(now=utcnow())
    assert len(results) == 1
    result = results[0]
    assert result.sandbox_id == "sbx_victim"
    assert result.action == "killed"
    assert result.record is victim

    assert registry.count() == 1
    assert registry.get("sbx_other").state == "running"
    assert _pool(registry) == 512
    notice = registry.eviction_notice("sbx_victim")
    assert notice is not None
    assert notice["reason"] == "evicted-idle"

    _create(registry, sandbox_id="sbx_third")  # room again
    assert _pool(registry) == 1024


def test_kill_action_callback_is_invoked_with_the_victim(workspace):
    registry, victim = _full_registry()
    killed: list[str] = []

    results = registry.evict_for_capacity(
        kill_action=lambda record: killed.append(record.sandbox_id)
    )
    assert [r.action for r in results] == ["killed"]
    assert killed == ["sbx_victim"]
    assert registry.count() == 1


# -- prefer-pause path ----------------------------------------------------


def test_prefer_pause_keeps_record_and_releases_its_reservation(workspace):
    registry, victim = _full_registry()
    paused_hook: list[str] = []

    results = registry.evict_for_capacity(
        prefer_pause=True,
        pause_action=lambda record: paused_hook.append(record.sandbox_id),
    )
    assert len(results) == 1
    assert results[0].action == "paused"
    assert results[0].sandbox_id == "sbx_victim"

    # The victim stays addressable (metadata untouched, no kill notice) but
    # its reservation is back in the pool (same assertion as E9.2).
    record = registry.get("sbx_victim")
    assert record.state == "paused"
    assert record.quota_released is True
    assert _pool(registry) == 512
    assert registry.eviction_notice("sbx_victim") is None
    assert paused_hook == ["sbx_victim"]

    _create(registry, sandbox_id="sbx_third")
    assert _pool(registry) == 1024


def test_pause_path_never_touches_metadata(workspace):
    registry, victim = _full_registry()
    victim.metadata = {"team": "data-science"}
    registry.save(victim)

    registry.evict_for_capacity(prefer_pause=True)
    assert registry.get("sbx_victim").metadata == {"team": "data-science"}


# -- bounds / throttle / failures -----------------------------------------


def test_max_victims_caps_one_round(workspace):
    registry = SandboxRegistry(
        _settings(
            max_total_memory_mb=4096,
            sandbox_idle_threshold_s=600,
        )
    )
    stamp = utcnow() - timedelta(seconds=900)
    victims = []
    for suffix in ("a", "b", "c", "d"):
        record = _create(registry, sandbox_id=f"sbx_{suffix}")
        record.last_active_at = stamp
        registry.save(record)
        victims.append(record)

    results = registry.evict_for_capacity(max_victims=3)
    assert [r.sandbox_id for r in results] == ["sbx_a", "sbx_b", "sbx_c"]
    assert registry.count() == 1
    assert registry.get("sbx_d").state == "running"


def test_min_interval_throttles_eviction(workspace):
    registry = SandboxRegistry(
        _settings(
            eviction_min_interval_s=60,
            max_total_memory_mb=4096,
        )
    )
    victim = _create(registry, sandbox_id="sbx_victim")
    _create(registry, sandbox_id="sbx_other")
    _make_idle(registry, victim)
    victim2 = _create(registry, sandbox_id="sbx_victim2")
    _create(registry, sandbox_id="sbx_other2")
    _make_idle(registry, victim2)

    first = registry.evict_for_capacity(max_victims=1)
    assert [r.sandbox_id for r in first] == ["sbx_victim"]
    # Still inside the minimum interval: the second round must not evict.
    assert registry.evict_for_capacity(max_victims=1) == []
    assert registry.count() == 3


def test_no_candidates_leaves_quotas_untouched(workspace):
    registry = SandboxRegistry(_settings(sandbox_idle_threshold_s=600))
    _create(registry, sandbox_id="sbx_active_a")
    _create(registry, sandbox_id="sbx_active_b")
    before = _pool(registry)

    assert registry.evict_for_capacity() == []
    assert _pool(registry) == before
    assert registry.count() == 2


def test_eviction_disabled_executes_nothing(workspace):
    registry = SandboxRegistry(
        _settings(eviction_enabled=False)
    )
    victim = _create(registry, sandbox_id="sbx_victim")
    _create(registry, sandbox_id="sbx_other")
    _make_idle(registry, victim)

    assert registry.evict_for_capacity() == []
    assert registry.get("sbx_victim").state == "running"
    assert _pool(registry) == 1024
