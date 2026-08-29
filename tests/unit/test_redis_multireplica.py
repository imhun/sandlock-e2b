"""Phase 3: Redis-backed shared registries give atomic quotas across replicas."""

from __future__ import annotations

import time

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxRegistry,
)
from control_plane.registry.nodes import NodeRegistry


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


def test_redis_sandbox_quota_atomic_across_replicas():
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)
    registry_a = SandboxRegistry(_settings(), redis_client=client_a)
    registry_b = SandboxRegistry(_settings(), redis_client=client_b)

    _create(registry_a)  # 512 MB on replica A
    _create(registry_b)  # 1024 MB on replica B
    with pytest.raises(ResourceUnavailableError):
        _create(registry_a)  # 1536 > 1024, shared ledger

    # Replica B sees the record created by replica A.
    first_id = registry_a.list(limit=None)[0].sandbox_id
    assert registry_b.get(first_id).sandbox_id == first_id

    registry_b.delete(first_id)
    _create(registry_a)  # quota released through the shared ledger


def test_redis_node_quota_atomic_across_replicas():
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)
    nodes_a = NodeRegistry(redis_client=client_a)
    nodes_b = NodeRegistry(redis_client=client_b)
    for registry in (nodes_a, nodes_b):
        registry.register(
            node_id="node_r",
            address="http://r:49983",
            total_memory_mb=1024,
            total_cpu_percent=200,
            total_disk_mb=2048,
            total_processes=128,
        )

    first = nodes_a.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert first is not None
    second = nodes_b.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert second is not None
    third = nodes_a.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert third is None  # shared node ledger is full

    nodes_b.release_quota(
        "node_r",
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    again = nodes_a.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert again is not None


def test_redis_expired_reaped_across_replicas():
    import datetime
    import time

    server = fakeredis.FakeServer()
    registry_a = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    registry_b = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    record = _create(registry_a, timeout=1)
    time.sleep(1.5)
    expired = registry_b.remove_expired()
    assert [r.sandbox_id for r in expired] == [record.sandbox_id]
    _create(registry_a)  # quota released


def test_redis_migration_lock_excludes_other_replicas():
    """The SETNX migration marker is shared: one replica's claim blocks the
    other, and only the owning token releases it."""
    server = fakeredis.FakeServer()
    registry_a = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    registry_b = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )

    token = registry_a.try_acquire_migration("sbx_race")
    assert token is not None
    assert registry_b.try_acquire_migration("sbx_race") is None

    # A wrong token cannot clear the marker held by replica A.
    registry_b.release_migration("sbx_race", "stale-token")
    assert registry_b.try_acquire_migration("sbx_race") is None

    registry_a.release_migration("sbx_race", token)
    assert registry_b.try_acquire_migration("sbx_race") is not None


def test_redis_migration_lock_expires_after_ttl():
    """An owner that crashes (never releases) is unblocked by the TTL."""
    server = fakeredis.FakeServer()
    registry = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    token = registry.try_acquire_migration("sbx_crash", ttl=1)
    assert token is not None
    time.sleep(1.1)
    assert registry.try_acquire_migration("sbx_crash") is not None
