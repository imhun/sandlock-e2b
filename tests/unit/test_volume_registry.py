"""Volume registry behavior."""

from __future__ import annotations

from datetime import timedelta

import pytest

from control_plane.registry.volumes import (
    UnknownVolumeError,
    VolumeRecord,
    VolumeRegistry,
)
from gateway_common.timeutil import to_iso_z, utcnow


def test_create_get_list_delete(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    assert record.volume_id.startswith("vol_")
    assert record.token.startswith("tok_")
    assert record.path.is_dir()

    assert registry.get(record.volume_id).name == "data"
    assert [v.volume_id for v in registry.list()] == [record.volume_id]

    registry.delete(record.volume_id)
    with pytest.raises(UnknownVolumeError):
        registry.get(record.volume_id)
    assert not record.path.exists()


def test_duplicate_name_allowed(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    first = registry.create("same")
    second = registry.create("same")
    assert first.volume_id != second.volume_id


def test_token_verification(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    assert registry.verify_token(record.volume_id, record.token).volume_id == record.volume_id
    with pytest.raises(UnknownVolumeError):
        registry.verify_token(record.volume_id, "wrong")


def test_token_ttl_zero_is_never_expiring(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    assert record.token_expires_at is None
    assert record.is_token_valid() is True
    assert "tokenExpiresAt" not in record.as_volume_and_token()


def test_token_ttl_sets_expiry_and_verification_fails_after(workspace):
    registry = VolumeRegistry(workspace / "volumes", token_ttl_seconds=3600)
    record = registry.create("data")
    assert record.token_expires_at is not None
    assert record.as_volume_and_token()["tokenExpiresAt"].endswith("Z")
    assert record.is_token_valid() is True
    assert record.is_token_valid(utcnow() + timedelta(hours=2)) is False

    record.token_expires_at = utcnow() - timedelta(seconds=1)
    registry.save(record)
    with pytest.raises(UnknownVolumeError):
        registry.verify_token(record.volume_id, record.token)


def test_revoked_token_rejected_but_volume_remains(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    registry.revoke_token(record.volume_id)
    reloaded = registry.get(record.volume_id)
    assert reloaded.token_revoked is True
    assert reloaded.is_token_valid() is False
    with pytest.raises(UnknownVolumeError):
        registry.verify_token(record.volume_id, record.token)
    # Revocation is idempotent.
    registry.revoke_token(record.volume_id)
    assert registry.get(record.volume_id).token_revoked is True


def test_token_expiry_and_revocation_persist_across_restart(workspace):
    registry = VolumeRegistry(workspace / "volumes", token_ttl_seconds=3600)
    record = registry.create("data")
    registry.revoke_token(record.volume_id)

    restarted = VolumeRegistry(workspace / "volumes")
    loaded = restarted.get(record.volume_id)
    assert loaded.token == record.token
    # Storage serializes with millisecond precision (to_iso_z).
    assert to_iso_z(loaded.token_expires_at) == to_iso_z(record.token_expires_at)
    assert loaded.token_revoked is True
    with pytest.raises(UnknownVolumeError):
        restarted.verify_token(record.volume_id, record.token)


def test_legacy_storage_dict_without_expiry_is_valid(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    payload = record.to_storage_dict()
    payload.pop("token_expires_at")
    payload.pop("token_revoked")

    legacy = VolumeRecord.from_storage_dict(payload, record.path)
    assert legacy.token_expires_at is None
    assert legacy.token_revoked is False
    assert legacy.is_token_valid() is True


def test_redis_mode_backfills_legacy_disk_records(workspace):
    """E3.3 review I1: enabling Redis must not hide existing volumes."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    # Pre-Redis volume: created by a disk-only registry, never in Redis.
    legacy = VolumeRegistry(workspace / "volumes")
    record = legacy.create("legacy")

    upgraded = VolumeRegistry(
        workspace / "volumes",
        redis_client=fakeredis.FakeRedis(server=server),
    )
    loaded = upgraded.get(record.volume_id)
    assert loaded.volume_id == record.volume_id
    assert loaded.token == record.token
    # Legacy records keep the never-expiring token semantics.
    assert loaded.token_expires_at is None
    assert loaded.is_token_valid() is True
    assert (
        upgraded.verify_token(record.volume_id, record.token).volume_id
        == record.volume_id
    )
    assert [v.volume_id for v in upgraded.list()] == [record.volume_id]

    # The record was mirrored into Redis: a fresh replica with its own
    # empty disk can verify the token through the shared store alone.
    replica = VolumeRegistry(
        workspace / "volumes-replica",
        redis_client=fakeredis.FakeRedis(server=server),
    )
    assert (
        replica.verify_token(record.volume_id, record.token).volume_id
        == record.volume_id
    )


def test_redis_backfill_is_idempotent_and_preserves_revocation(workspace):
    """Backfill never overwrites an existing Redis record (e.g. a
    revocation written by another replica)."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    legacy = VolumeRegistry(workspace / "volumes")
    record = legacy.create("legacy")

    client = fakeredis.FakeRedis(server=server)
    upgraded = VolumeRegistry(workspace / "volumes", redis_client=client)
    assert upgraded.get(record.volume_id).volume_id == record.volume_id

    # A second replica revokes the token in the shared store.
    replica = VolumeRegistry(
        workspace / "volumes-replica",
        redis_client=fakeredis.FakeRedis(server=server),
    )
    replica.revoke_token(record.volume_id)

    # A fresh process over the same disk must not resurrect the stale
    # disk copy: existing Redis records win over backfill.
    reupgraded = VolumeRegistry(workspace / "volumes", redis_client=client)
    assert reupgraded.get(record.volume_id).token_revoked is True
    with pytest.raises(UnknownVolumeError):
        reupgraded.verify_token(record.volume_id, record.token)


def test_redis_shared_record_and_revocation_visibility(workspace):
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    registry = VolumeRegistry(
        workspace / "volumes",
        redis_client=fakeredis.FakeRedis(server=server),
        token_ttl_seconds=3600,
    )
    record = registry.create("data")

    replica = VolumeRegistry(
        workspace / "volumes-replica",
        redis_client=fakeredis.FakeRedis(server=server),
    )
    loaded = replica.get(record.volume_id)
    assert loaded.token == record.token
    assert to_iso_z(loaded.token_expires_at) == to_iso_z(record.token_expires_at)
    assert loaded.token_revoked is False
    assert replica.verify_token(record.volume_id, record.token).volume_id == record.volume_id

    replica.revoke_token(record.volume_id)
    with pytest.raises(UnknownVolumeError):
        registry.verify_token(record.volume_id, record.token)

    registry.delete(record.volume_id)
    with pytest.raises(UnknownVolumeError):
        replica.get(record.volume_id)


def test_create_with_per_sandbox_quota_mb(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data", per_sandbox_quota_mb=1024)
    assert record.per_sandbox_quota_mb == 1024
    assert record.as_volume()["perSandboxQuotaMb"] == 1024
    assert record.as_volume_and_token()["perSandboxQuotaMb"] == 1024


def test_default_per_sandbox_quota_is_zero(workspace):
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    assert record.per_sandbox_quota_mb == 0
    assert record.as_volume()["perSandboxQuotaMb"] == 0


@pytest.mark.parametrize("quota", [-1, "1024", 1.5, True, None])
def test_invalid_per_sandbox_quota_rejected(workspace, quota):
    registry = VolumeRegistry(workspace / "volumes")
    with pytest.raises(ValueError):
        registry.create("data", per_sandbox_quota_mb=quota)
