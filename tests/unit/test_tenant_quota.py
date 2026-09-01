"""E3.1: per-tenant quota reservation, admission and isolation."""

from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import ResourceUnavailableError, SandboxRegistry


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
