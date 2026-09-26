"""E3.1: per-tenant quota reservation, admission and isolation."""

from __future__ import annotations

from datetime import timedelta

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import ResourceUnavailableError, SandboxRegistry
from gateway_common.timeutil import utcnow


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=4096,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry, tenant_id="t1", is_admin=False, **kw):
    kwargs = dict(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
        tenant_id=tenant_id,
        is_admin=is_admin,
    )
    kwargs.update(kw)
    return registry.create(**kwargs)


def test_tenant_sandbox_cap_blocks_tenant_but_not_others(workspace):
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_sandboxes": 1}, "t2": {"max_sandboxes": 1}})
    )
    _create(registry, tenant_id="t1")
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, tenant_id="t1")
    assert str(exc.value) == "tenant quota exceeded"
    _create(registry, tenant_id="t2")  # other tenant unaffected


def test_tenant_memory_cap_admission(workspace):
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_total_memory_mb": 1024}})
    )
    _create(registry, tenant_id="t1")  # 512 MB
    _create(registry, tenant_id="t1")  # 1024 MB -> full
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, tenant_id="t1")
    assert str(exc.value) == "tenant quota exceeded"


def test_tenant_cpu_and_disk_and_process_dims(workspace):
    registry = SandboxRegistry(
        _settings(
            tenant_limits={
                "t1": {
                    "max_total_cpu_percent": 150,
                    "max_total_disk_mb": 1536,
                    "max_total_processes": 100,
                }
            }
        )
    )
    _create(registry, tenant_id="t1")  # cpu 100, disk 1024, processes 64
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, tenant_id="t1")
    assert str(exc.value) == "tenant quota exceeded"


def test_tenant_quota_released_on_delete(workspace):
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_sandboxes": 1}})
    )
    record = _create(registry, tenant_id="t1")
    with pytest.raises(ResourceUnavailableError):
        _create(registry, tenant_id="t1")
    registry.delete(record.sandbox_id)
    _create(registry, tenant_id="t1")  # released


def test_admin_bypasses_tenant_quota(workspace):
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_sandboxes": 1}})
    )
    _create(registry, tenant_id="t1")
    # Admin-created sandboxes are unowned (tenant_id None) and exempt from
    # tenant limits; the global cap still applies.
    _create(registry, tenant_id="t1", is_admin=True)
    assert registry.count() == 2
    tenants = [r.tenant_id for r in registry.list()]
    assert tenants.count("t1") == 1
    assert tenants.count(None) == 1


def test_unconfigured_tenant_uses_global_only(workspace):
    registry = SandboxRegistry(_settings(max_sandboxes=2))
    _create(registry, tenant_id="t1")
    _create(registry, tenant_id="t1")
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, tenant_id="t1")
    assert str(exc.value) == "No resources available"


def test_tenant_quota_redis_mode_isolation():
    server = fakeredis.FakeServer()
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_sandboxes": 1}, "t2": {"max_sandboxes": 1}}),
        redis_client=fakeredis.FakeRedis(server=server),
    )
    _create(registry, tenant_id="t1")
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, tenant_id="t1")
    assert str(exc.value) == "tenant quota exceeded"
    _create(registry, tenant_id="t2")

    # Global reservation is rolled back when the tenant admission fails.
    registry.delete([r for r in registry.list() if r.tenant_id == "t2"][0].sandbox_id)
    registry.delete([r for r in registry.list() if r.tenant_id == "t1"][0].sandbox_id)
    _create(registry, tenant_id="t1")


def test_tenant_usage_aggregation(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, tenant_id="t1")
    _create(registry, tenant_id="t1")
    _create(registry, tenant_id="t2")
    _create(registry, tenant_id=None)  # unowned
    assert registry.tenant_usage() == {
        "t1": {"sandboxes": 2, "memoryMB": 1024, "cpuPercent": 200, "diskMB": 2048, "processes": 128},
        "t2": {"sandboxes": 1, "memoryMB": 512, "cpuPercent": 100, "diskMB": 1024, "processes": 64},
        None: {"sandboxes": 1, "memoryMB": 512, "cpuPercent": 100, "diskMB": 1024, "processes": 64},
    }


# -- N30: the tenant ledger's disk row ------------------------------------
#
# The tenant ledger is the second copy of the same sale (``_tenant_dims``
# beside ``_global_dims``), and it is written only for tenants that configured
# a limit -- which is exactly why a release can forget it while the global row
# still looks right. ``tenant_usage`` is a different question (it counts every
# record, parked ones included); these cases are about the reservation rows.


def _live_tenant_disk_mb(registry, tenant_id: str) -> int:
    return sum(
        r.disk_size_mb
        for r in registry.list()
        if r.tenant_id == tenant_id and not r.quota_released
    )


def test_the_tenant_disk_row_is_the_sum_of_its_live_records(make_record):
    registry = SandboxRegistry(
        _settings(
            tenant_limits={
                "t1": {"max_total_disk_mb": 4096},
                "t2": {"max_total_disk_mb": 4096},
            }
        )
    )
    make_record(registry, disk_size_mb=64, tenant_id="t1")
    make_record(registry, disk_size_mb=128, tenant_id="t1")
    make_record(registry, disk_size_mb=1024, tenant_id="t2")

    assert registry._tenant_reserved["t1"]["disk"] == 192
    assert registry._tenant_reserved["t1"]["disk"] == _live_tenant_disk_mb(
        registry, "t1"
    )
    # The other tenant's row is its own: nothing about t1's rows may move it.
    assert registry._tenant_reserved["t2"]["disk"] == 1024


def test_pause_and_the_delete_after_it_return_the_tenant_disk_row_once(make_record):
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_total_disk_mb": 4096}})
    )
    parked = make_record(registry, disk_size_mb=64, tenant_id="t1")
    other = make_record(registry, disk_size_mb=128, tenant_id="t1")
    assert registry._tenant_reserved["t1"]["disk"] == 192

    registry.pause(parked)
    assert registry._tenant_reserved["t1"]["disk"] == 128
    registry.delete(parked.sandbox_id)
    # 128 rather than 64: the delete of a parked record releases nothing.
    assert registry._tenant_reserved["t1"]["disk"] == 128

    registry.delete(other.sandbox_id)
    assert registry._tenant_reserved["t1"]["disk"] == 0


def test_ttl_expiry_returns_the_tenant_disk_row_once(make_record):
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_total_disk_mb": 4096}})
    )
    record = make_record(registry, disk_size_mb=64, tenant_id="t1")
    record.end_at = utcnow() - timedelta(seconds=10)

    expired = registry.remove_expired()
    assert [r.sandbox_id for r in expired] == [record.sandbox_id]
    assert registry._tenant_reserved["t1"]["disk"] == 0
    assert registry.release_quota(record) is False


def test_the_tenant_disk_row_returns_to_zero_in_the_shared_store(make_record):
    registry = SandboxRegistry(
        _settings(tenant_limits={"t1": {"max_total_disk_mb": 4096}}),
        redis_client=fakeredis.FakeRedis(),
    )
    record = make_record(registry, disk_size_mb=64, tenant_id="t1")
    assert registry._quota_store.get("tenant:t1")["disk"] == 64

    registry.delete(record.sandbox_id)
    assert registry._quota_store.get("tenant:t1")["disk"] == 0


def test_pause_and_the_delete_after_it_return_the_tenant_store_row_once(make_record):
    """The shared-store half of pause: the tenant row has to come back too.

    The store branch writes the tenant row only for a tenant that configured a
    limit, and only ``release_quota`` moves it -- so a release that forgot it
    while the global row looked right is invisible to every in-memory case.
    ``other`` is the instrument, as in the global case: with a second record
    booked, a release that moved the parked record's row twice reads 0 instead
    of 64 (a single record sits at 0 either way).

    Both records are created at ``default_disk_mb`` so the sale helper books
    them without going through ``release_quota`` -- the mutation this case
    exists to catch lives on that path, and a setup that re-booked through it
    would fail there instead of on the path under test.
    """
    registry = SandboxRegistry(
        _settings(
            default_disk_mb=64, tenant_limits={"t1": {"max_total_disk_mb": 4096}}
        ),
        redis_client=fakeredis.FakeRedis(),
    )
    parked = make_record(registry, disk_size_mb=64, tenant_id="t1")
    other = make_record(registry, disk_size_mb=64, tenant_id="t1")
    assert registry._quota_store.get("tenant:t1")["disk"] == 128

    registry.pause(parked)
    assert registry._quota_store.get("tenant:t1")["disk"] == 64
    registry.delete(parked.sandbox_id)
    # 64 rather than 0: the delete of a parked record releases nothing.
    assert registry._quota_store.get("tenant:t1")["disk"] == 64

    registry.delete(other.sandbox_id)
    assert registry._quota_store.get("tenant:t1")["disk"] == 0


def test_ttl_expiry_returns_the_tenant_store_row_once(make_record):
    """The shared-store half of the TTL sweep, for the tenant ledger.

    ``remove_expired`` walks the store and hands each expired record to
    ``_release`` -> ``release_quota``, which is what moves both rows and sets
    the flag; the in-memory case cannot vouch for that path, and on this
    backend the flag is not the only writer of the row.

    ``other`` is the instrument again: with only the expired record booked the
    tenant row reads 0 either way, so a second subtraction of the swept
    record's row would be hidden there -- with two booked, it reads 0 instead
    of 64. Both records are created at ``default_disk_mb`` so the setup does
    not run through the release path under test (see the case above).
    """
    registry = SandboxRegistry(
        _settings(
            default_disk_mb=64, tenant_limits={"t1": {"max_total_disk_mb": 4096}}
        ),
        redis_client=fakeredis.FakeRedis(),
    )
    record = make_record(registry, disk_size_mb=64, tenant_id="t1")
    other = make_record(registry, disk_size_mb=64, tenant_id="t1")
    assert registry._quota_store.get("tenant:t1")["disk"] == 128

    record.end_at = utcnow() - timedelta(seconds=10)
    # The sweep enumerates the shared store, so the deadline has to land there.
    registry.save(record)

    expired = registry.remove_expired()
    assert [r.sandbox_id for r in expired] == [record.sandbox_id]
    assert registry._quota_store.get("tenant:t1")["disk"] == 64
    assert registry.release_quota(expired[0]) is False
    assert registry._quota_store.get("tenant:t1")["disk"] == 64
    assert registry.get(other.sandbox_id).quota_released is False
