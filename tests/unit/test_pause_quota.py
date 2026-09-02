"""E9.2: pause releases the admission reservation, resume has to buy it back.

The eviction strategy ("hibernate the idle ones to make room") only pays off
if a parked sandbox really frees capacity, so these pin the accounting:
release on pause, re-admission on resume, rollback on refusal, and no
double-release when a parked sandbox is later deleted or reaped.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxRegistry,
    SandboxStateConflictError,
)
from gateway_common.timeutil import utcnow


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=0,
        default_disk_mb=0,
        default_max_processes=0,
        max_total_memory_mb=1024,
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


def _pool(registry) -> int:
    return registry._reserved_memory


# -- in-memory registries -------------------------------------------------


def test_pause_frees_capacity_for_a_new_sandbox(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")  # pool now full (1024 MB)
    with pytest.raises(ResourceUnavailableError):
        _create(registry, sandbox_id="sbx_c")

    registry.pause(registry.get("sbx_a"))
    assert registry.get("sbx_a").state == "paused"
    assert registry.get("sbx_a").quota_released is True
    assert _pool(registry) == 512

    _create(registry, sandbox_id="sbx_c")  # room again
    assert _pool(registry) == 1024


def test_resume_reacquires_capacity(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    registry.delete("sbx_b")

    registry.resume(registry.get("sbx_a"))
    record = registry.get("sbx_a")
    assert record.state == "running"
    assert record.quota_released is False
    assert _pool(registry) == 512


def test_resume_when_full_keeps_the_sandbox_paused(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    _create(registry, sandbox_id="sbx_c")  # takes the freed slot

    with pytest.raises(ResourceUnavailableError) as exc:
        registry.resume(registry.get("sbx_a"))
    assert str(exc.value) == "No resources available"

    record = registry.get("sbx_a")
    assert record.state == "paused"
    assert record.quota_released is True
    assert _pool(registry) == 1024  # nothing booked, nothing lost


def test_delete_of_paused_sandbox_does_not_release_twice(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    assert _pool(registry) == 512

    registry.delete("sbx_a")
    assert _pool(registry) == 512  # only sbx_b still holds reservation
    _create(registry, sandbox_id="sbx_c")
    assert _pool(registry) == 1024


def test_concurrency_cap_ignores_paused_sandboxes(workspace):
    registry = SandboxRegistry(_settings(max_total_memory_mb=0, max_sandboxes=1))
    _create(registry, sandbox_id="sbx_a")
    with pytest.raises(ResourceUnavailableError):
        _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    _create(registry, sandbox_id="sbx_b")


def test_pause_rejects_double_pause_without_touching_the_ledger(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_a")
    registry.pause(record)
    assert _pool(registry) == 0

    with pytest.raises(SandboxStateConflictError):
        registry.pause(registry.get("sbx_a"))
    assert _pool(registry) == 0
    assert registry.get("sbx_a").quota_released is True


def test_resume_of_running_sandbox_leaves_reservation_alone(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    with pytest.raises(SandboxStateConflictError):
        registry.resume(registry.get("sbx_a"))
    assert _pool(registry) == 512
    assert registry.get("sbx_a").quota_released is False


def test_ttl_reaps_running_but_parks_paused(workspace):
    registry = SandboxRegistry(_settings())
    running = _create(registry, sandbox_id="sbx_run")
    parked = _create(registry, sandbox_id="sbx_park")
    registry.pause(parked)
    for record in (running, parked):
        record.end_at = utcnow() - timedelta(seconds=10)

    expired = registry.remove_expired()
    assert [r.sandbox_id for r in expired] == ["sbx_run"]
    assert registry.get("sbx_park").state == "paused"
    assert _pool(registry) == 0


# -- tenant ledger --------------------------------------------------------


def test_pause_releases_tenant_quota_and_resume_rebooks_it(workspace):
    settings = _settings(
        tenant_limits={"t1": {"max_sandboxes": 1, "max_total_memory_mb": 1024}},
    )
    registry = SandboxRegistry(settings)
    _create(registry, sandbox_id="sbx_t1", tenant_id="t1")
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, sandbox_id="sbx_t1b", tenant_id="t1")
    assert str(exc.value) == "tenant quota exceeded"

    registry.pause(registry.get("sbx_t1"))
    assert registry._tenant_reserved["t1"]["sandboxes"] == 0
    _create(registry, sandbox_id="sbx_t1b", tenant_id="t1")
    assert registry._tenant_reserved["t1"]["sandboxes"] == 1


def test_resume_reports_tenant_quota_exhaustion(workspace):
    settings = _settings(
        max_total_memory_mb=0,
        tenant_limits={"t1": {"max_sandboxes": 1}},
    )
    registry = SandboxRegistry(settings)
    _create(registry, sandbox_id="sbx_t1", tenant_id="t1")
    registry.pause(registry.get("sbx_t1"))
    _create(registry, sandbox_id="sbx_t1b", tenant_id="t1")

    with pytest.raises(ResourceUnavailableError) as exc:
        registry.resume(registry.get("sbx_t1"))
    assert str(exc.value) == "tenant quota exceeded"
    assert registry.get("sbx_t1").state == "paused"


# -- shared store (multi-replica) ----------------------------------------


def test_shared_store_pause_frees_capacity_for_another_replica(workspace):
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)
    replica_a = SandboxRegistry(_settings(), redis_client=client_a)
    replica_b = SandboxRegistry(_settings(), redis_client=client_b)

    _create(replica_a, sandbox_id="sbx_r1")
    _create(replica_a, sandbox_id="sbx_r2")
    with pytest.raises(ResourceUnavailableError):
        _create(replica_b, sandbox_id="sbx_r3")

    replica_a.pause(replica_a.get("sbx_r1"))
    assert replica_a.get("sbx_r1").quota_released is True
    _create(replica_b, sandbox_id="sbx_r3")  # room visible across replicas
    with pytest.raises(ResourceUnavailableError):
        _create(replica_b, sandbox_id="sbx_r4")

    # The released flag is durable: a resume still has to win admission again.
    with pytest.raises(ResourceUnavailableError):
        replica_b.resume(replica_b.get("sbx_r1"))
    assert replica_b.get("sbx_r1").state == "paused"


def test_shared_store_record_round_trips_quota_released(workspace):
    client = fakeredis.FakeRedis()
    registry = SandboxRegistry(_settings(), redis_client=client)
    _create(registry, sandbox_id="sbx_q")
    registry.pause(registry.get("sbx_q"))
    stored = registry._record_store.get("sbx_q")
    assert stored["quota_released"] is True
    assert registry.get("sbx_q").quota_released is True
