"""Registry create / get / delete / max-concurrency behavior."""

from __future__ import annotations

import time

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxRecord,
    SandboxRegistry,
    UnknownSandboxError,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=3,
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


def test_create_and_get(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry)
    assert record.sandbox_id.startswith("sbx_")
    assert record.client_id.startswith("cli_")
    assert record.envd_access_token.startswith("tok_")
    assert record.state == "running"
    assert record.envd_version == "0.6.4+sandlock"
    assert registry.get(record.sandbox_id) is record
    assert registry.count() == 1


def test_lookup_missing_raises(workspace):
    registry = SandboxRegistry(_settings())
    with pytest.raises(UnknownSandboxError):
        registry.get("sbx_nonexistent")
    with pytest.raises(UnknownSandboxError):
        registry.get("../etc/passwd")


def test_migration_lock_single_owner_in_memory():
    """Only one migration may hold a sandbox; the token gates the release."""
    registry = SandboxRegistry(_settings())
    first = registry.try_acquire_migration("sbx_migrating")
    assert first is not None
    # A second claim (e.g. another request in the same process) is refused.
    assert registry.try_acquire_migration("sbx_migrating") is None
    # A stale token cannot release the lock held by someone else.
    registry.release_migration("sbx_migrating", "stale-token")
    assert registry.try_acquire_migration("sbx_migrating") is None
    # The owner's token releases it and a fresh claim succeeds.
    registry.release_migration("sbx_migrating", first)
    assert registry.try_acquire_migration("sbx_migrating") is not None


def test_migration_lock_expires_in_memory():
    """A crashed migration holder cannot block migrations forever."""
    registry = SandboxRegistry(_settings())
    token = registry.try_acquire_migration("sbx_expiring", ttl=1)
    assert token is not None
    time.sleep(1.1)
    assert registry.try_acquire_migration("sbx_expiring") is not None


def test_network_field_round_trip_and_detail(workspace):
    registry = SandboxRegistry(_settings())
    network = {"allowOut": ["8.8.8.8"], "allowPublicTraffic": True}
    record = _create(registry, network=network)
    assert record.network == network
    assert record.as_detail()["network"] == network
    restored = SandboxRecord.from_storage_dict(record.to_storage_dict())
    assert restored.network == network


def test_network_field_defaults_to_none(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry)
    assert record.network is None
    assert record.as_detail()["network"] == {}


def test_delete_releases_entry(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry)
    deleted = registry.delete(record.sandbox_id)
    assert deleted is record
    assert registry.count() == 0
    with pytest.raises(UnknownSandboxError):
        registry.delete(record.sandbox_id)


def test_max_sandboxes_rejected(workspace):
    registry = SandboxRegistry(_settings(max_sandboxes=2))
    _create(registry)
    _create(registry)
    with pytest.raises(ResourceUnavailableError):
        _create(registry)
    # Freeing a slot allows creation again.
    registry.remove_expired()
    first = next(iter(registry._sandboxes.values()))
    registry.delete(first.sandbox_id)
    _create(registry)


def test_connect_refreshes_end_at(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, timeout=300)
    old_end = record.end_at
    registry.connect(record.sandbox_id, timeout=600)
    assert record.end_at > old_end


def test_create_with_explicit_sandbox_id(workspace):
    registry = SandboxRegistry(_settings())
    record = registry.create(
        template_id="base",
        sandbox_id="sbx_clientprovided",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    assert record.sandbox_id == "sbx_clientprovided"
    assert registry.get("sbx_clientprovided") is record


def test_create_rejects_invalid_sandbox_id(workspace):
    registry = SandboxRegistry(_settings())
    with pytest.raises(ValueError):
        registry.create(
            template_id="base",
            sandbox_id="../../etc/passwd",
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
        )


def test_pending_claim_get_release(workspace):
    registry = SandboxRegistry(_settings())
    assert registry.claim_pending("sbx_p", {"node": "n1"}, ttl=30) is True
    assert registry.claim_pending("sbx_p", {"node": "n2"}, ttl=30) is False
    assert registry.get_pending("sbx_p") == {"node": "n1"}
    registry.release_pending("sbx_p")
    assert registry.get_pending("sbx_p") is None
    assert registry.claim_pending("sbx_p", {"node": "n2"}, ttl=30) is True
    registry.release_pending(None)  # no-op


def test_pending_expires(workspace):
    registry = SandboxRegistry(_settings())
    assert registry.claim_pending("sbx_e", {"node": "n1"}, ttl=-1)
    assert registry.get_pending("sbx_e") is None
