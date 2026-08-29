"""Total resource admission: reservation, 503-equivalent errors, release."""

from __future__ import annotations

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxRegistry,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=1024,
        max_total_cpu_percent=200,
        max_total_disk_mb=2048,
        max_total_processes=128,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry):
    return registry.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )


def test_memory_admission_and_release(workspace):
    registry = SandboxRegistry(_settings(max_total_memory_mb=1024))
    _create(registry)  # 512 MB
    _create(registry)  # 1024 MB -> full
    with pytest.raises(ResourceUnavailableError):
        _create(registry)
    first = registry.list(limit=None)[0]
    registry.delete(first.sandbox_id)
    _create(registry)  # released quota allows a new sandbox


def test_cpu_admission(workspace):
    registry = SandboxRegistry(_settings(max_total_cpu_percent=150))
    _create(registry)  # 100%
    with pytest.raises(ResourceUnavailableError):
        _create(registry)  # 200% > 150%


def test_disk_admission(workspace):
    registry = SandboxRegistry(_settings(max_total_disk_mb=1536))
    _create(registry)  # 1024
    with pytest.raises(ResourceUnavailableError):
        _create(registry)  # 2048 > 1536


def test_process_admission(workspace):
    registry = SandboxRegistry(_settings(max_total_processes=100))
    _create(registry)  # 64
    with pytest.raises(ResourceUnavailableError):
        _create(registry)  # 128 > 100


def test_zero_limit_disables_dimension(workspace):
    registry = SandboxRegistry(
        _settings(
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        )
    )
    _create(registry)
    _create(registry)
    _create(registry)
    assert registry.count() == 3


def test_kill_and_ttl_release_quota(workspace):
    registry = SandboxRegistry(_settings(max_total_memory_mb=1024))
    _create(registry)
    _create(registry)
    with pytest.raises(ResourceUnavailableError):
        _create(registry)
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    for record in registry.list(limit=None):
        record.end_at = now - datetime.timedelta(seconds=1)
    expired = registry.remove_expired()
    assert len(expired) == 2
    _create(registry)  # quota released by TTL reap
    _create(registry)
    assert registry.count() == 2
