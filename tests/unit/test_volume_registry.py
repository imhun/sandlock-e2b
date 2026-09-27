"""Volume registry behavior."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest

from control_plane.registry.volumes import (
    UnknownVolumeError,
    VolumeRecord,
    VolumeRegistry,
)
from gateway_common.timeutil import to_iso_z, utcnow


def _make_pre_e33_disk_record(registry, record):
    """Rewrite a record's meta file as a genuine pre-E3.3 payload.

    Current ``to_storage_dict()`` always writes the E3.3 token fields
    (even when unset), so simulating a legacy disk record requires
    dropping those keys, exactly as E3.2-era code did.
    """
    payload = record.to_storage_dict()
    payload.pop("token_expires_at", None)
    payload.pop("token_revoked", None)
    path = registry._record_path(record.volume_id)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
    _make_pre_e33_disk_record(legacy, record)

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
    _make_pre_e33_disk_record(legacy, record)

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


def test_redis_delete_survives_restart_with_stale_replica_disk_copy(workspace):
    """E3.3 fix2: replica A deletes a volume; replica B, which wrote a
    disk copy earlier, must not resurrect it after restarting (its stale
    disk copy must never be backfilled into the shared store)."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    base_a = workspace / "replica-a"
    base_b = workspace / "replica-b"
    registry_a = VolumeRegistry(
        base_a, redis_client=fakeredis.FakeRedis(server=server)
    )
    registry_b = VolumeRegistry(
        base_b, redis_client=fakeredis.FakeRedis(server=server)
    )
    record = registry_a.create("data")
    # Replica B saved the shared record, leaving its own disk copy.
    registry_b.save(registry_b.get(record.volume_id))
    assert (base_b / "_meta" / f"{record.volume_id}.json").is_file()

    # Replica A deletes the volume: Redis key tombstoned, A's disk copy
    # removed, B's stale disk copy untouched.
    registry_a.delete(record.volume_id)
    with pytest.raises(UnknownVolumeError):
        registry_a.get(record.volume_id)

    # Replica B restarts with its stale disk copy: volume invisible and
    # its token invalid.
    restarted_b = VolumeRegistry(
        base_b, redis_client=fakeredis.FakeRedis(server=server)
    )
    with pytest.raises(UnknownVolumeError):
        restarted_b.get(record.volume_id)
    assert [v.volume_id for v in restarted_b.list()] == []
    with pytest.raises(UnknownVolumeError):
        restarted_b.verify_token(record.volume_id, record.token)


def test_redis_backfill_skips_redis_mode_disk_copies(workspace):
    """Only pre-E3.3 disk records (without token_expires_at/token_revoked)
    qualify for backfill; a Redis-mode replica's disk copy is a stale cache
    and must never be mirrored back on a Redis miss."""
    fakeredis = pytest.importorskip("fakeredis")
    base = workspace / "volumes"
    replica = VolumeRegistry(
        base, redis_client=fakeredis.FakeRedis(server=fakeredis.FakeServer())
    )
    record = replica.create("data")
    disk_copy = replica._record_path(record.volume_id)
    assert disk_copy.is_file()
    assert "token_expires_at" in json.loads(disk_copy.read_text(encoding="utf-8"))

    # A fresh process with the same disk and an empty shared store (e.g.
    # Redis reset) must not resurrect the record from the disk copy.
    fresh = VolumeRegistry(
        base, redis_client=fakeredis.FakeRedis(server=fakeredis.FakeServer())
    )
    with pytest.raises(UnknownVolumeError):
        fresh.get(record.volume_id)
    assert [v.volume_id for v in fresh.list()] == []


def test_redis_tombstone_blocks_backfill_of_legacy_disk_record(workspace):
    """A deletion by another replica (Redis tombstone) keeps even a
    qualifying pre-E3.3 disk record deleted after a restart."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    base = workspace / "volumes"
    # Pre-Redis legacy record (no token fields) on this replica's disk.
    legacy = VolumeRegistry(base)
    record = legacy.create("legacy")
    _make_pre_e33_disk_record(legacy, record)

    upgraded = VolumeRegistry(
        base, redis_client=fakeredis.FakeRedis(server=server)
    )
    assert upgraded.get(record.volume_id).volume_id == record.volume_id

    # Another replica deletes the volume; this replica's legacy disk copy
    # is left untouched.
    deleter = VolumeRegistry(
        workspace / "deleter", redis_client=fakeredis.FakeRedis(server=server)
    )
    deleter.delete(record.volume_id)

    # Fresh process over the same disk: the Redis tombstone wins over the
    # legacy disk copy.
    restarted = VolumeRegistry(
        base, redis_client=fakeredis.FakeRedis(server=server)
    )
    with pytest.raises(UnknownVolumeError):
        restarted.get(record.volume_id)
    assert [v.volume_id for v in restarted.list()] == []
    with pytest.raises(UnknownVolumeError):
        restarted.verify_token(record.volume_id, record.token)


def test_disk_tombstone_keeps_restored_meta_deleted(workspace):
    """The disk tombstone marker survives restarts even if the record file
    reappears (partial delete / restored copy), so a deleted volume cannot
    come back in disk-only mode either."""
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    registry.delete(record.volume_id)
    assert not registry._record_path(record.volume_id).exists()

    stale = record.to_storage_dict()
    path = registry._record_path(record.volume_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stale), encoding="utf-8")

    restarted = VolumeRegistry(workspace / "volumes")
    with pytest.raises(UnknownVolumeError):
        restarted.get(record.volume_id)
    assert [v.volume_id for v in restarted.list()] == []


def test_redis_list_reflects_cross_replica_delete(workspace):
    """list() in Redis mode re-checks the shared store, so a volume
    deleted by another replica disappears even without a restart."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    registry_a = VolumeRegistry(
        workspace / "a", redis_client=fakeredis.FakeRedis(server=server)
    )
    registry_b = VolumeRegistry(
        workspace / "b", redis_client=fakeredis.FakeRedis(server=server)
    )
    record = registry_a.create("data")
    assert registry_b.get(record.volume_id).volume_id == record.volume_id

    registry_a.delete(record.volume_id)
    assert [v.volume_id for v in registry_b.list()] == []


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


def test_a_volume_record_is_published_in_one_step(workspace, publish_spy):
    """A volume record is read while it is written (peers, and the worker).

    Revoking a token is a rewrite of ``_meta/<id>.json``, and a replica or a
    worker that reads the file inside that window gets half a document rather
    than "revoked": the disk copy is what a Redis-less deployment resolves the
    volume from, and what a Redis-mode replica backfills from.
    """
    registry = VolumeRegistry(workspace / "volumes")
    record = registry.create("data")
    path = registry._record_path(record.volume_id)
    before = json.loads(path.read_text(encoding="utf-8"))
    publish_spy.reset()

    record.token_revoked = True
    with publish_spy.hold_next_publish() as in_window:
        writer = threading.Thread(target=registry.save, args=(record,), daemon=True)
        writer.start()
        publish_spy.await_publish(in_window, "a volume record")
        assert json.loads(path.read_text(encoding="utf-8")) == before
    writer.join(timeout=10)
    assert not writer.is_alive()

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["token_revoked"] is True
