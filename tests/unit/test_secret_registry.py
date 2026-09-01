"""Secret registry behavior."""

from __future__ import annotations

import json

import pytest

from control_plane.registry.secrets import (
    MAX_SECRET_VALUE_BYTES,
    SecretRegistry,
    UnknownSecretError,
)


def test_create_update_version(workspace):
    registry = SecretRegistry()
    record = registry.create("api_key", "v1", metadata={"env": "test"})
    assert record.secret_id.startswith("sec_")
    assert record.version == 1
    assert registry.get(record.secret_id).value == "v1"

    updated = registry.update(record.secret_id, "v2")
    assert updated.version == 2
    assert updated.value == "v2"
    assert registry.get_by_name("api_key").value == "v2"


def test_duplicate_name_rejected(workspace):
    registry = SecretRegistry()
    registry.create("name", "v")
    with pytest.raises(ValueError):
        registry.create("name", "v2")


def test_delete(workspace):
    registry = SecretRegistry()
    record = registry.create("name", "v")
    registry.delete(record.secret_id)
    with pytest.raises(UnknownSecretError):
        registry.get(record.secret_id)
    with pytest.raises(UnknownSecretError):
        registry.get_by_name("name")


def test_env_refs_resolution(workspace):
    registry = SecretRegistry()
    registry.create("mysecret", "secret-value")
    resolved = registry.resolve_env_refs(
        {"PLAIN": "hello", "FROM_SECRET": "${mysecret}", "MISSING": "${nope}"}
    )
    assert resolved == {
        "PLAIN": "hello",
        "FROM_SECRET": "secret-value",
        "MISSING": "${nope}",
    }


def test_no_master_key_degrades_with_warning(workspace, caplog):
    with caplog.at_level("WARNING", logger="control_plane.registry.secrets"):
        registry = SecretRegistry(workspace / "secrets")
    record = registry.create("plain", "pv")
    payload = json.loads(
        (workspace / "secrets" / record.secret_id / "secret.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload.get("encrypted") is False
    assert payload["value"] == "pv"
    assert any(
        r.levelname == "WARNING"
        and r.message.startswith("E2B_SECRET_MASTER_KEY is not configured")
        for r in caplog.records
    )


def test_encrypted_disk_persistence_roundtrip(workspace):
    registry = SecretRegistry(workspace / "secrets", master_key="master-1")
    record = registry.create("api_key", "v1", metadata={"env": "test"})
    path = workspace / "secrets" / record.secret_id / "secret.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["encrypted"] is True
    assert payload["value"] != record.value
    assert payload["name"] == "api_key"

    restarted = SecretRegistry(workspace / "secrets", master_key="master-1")
    assert restarted.get(record.secret_id).value == "v1"
    assert restarted.get_by_name("api_key").value == "v1"


def test_redis_persistence_survives_restart(workspace):
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    registry = SecretRegistry(
        workspace / "secrets", redis_client=client, master_key="master-1"
    )
    record = registry.create("tok", "s3cr3t")

    raw = client.get(f"e2b:secret:{record.secret_id}")
    payload = json.loads(raw)
    assert payload["encrypted"] is True
    assert payload["value"] != "s3cr3t"

    restarted = SecretRegistry(
        workspace / "secrets-other", redis_client=client, master_key="master-1"
    )
    assert restarted.get_by_name("tok").value == "s3cr3t"


def test_no_master_key_skips_redis_persistence(workspace):
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    registry = SecretRegistry(workspace / "secrets", redis_client=client)
    registry.create("plain", "pv")
    assert client.keys("e2b:secret:*") == []


def test_rotate_master_key_and_legacy_window(workspace):
    registry = SecretRegistry(workspace / "secrets", master_key="old-key")
    record = registry.create("tok", "v1")
    rotated = registry.rotate_master_key("new-key")
    assert rotated == 1

    # The old key alone can no longer decrypt the re-encrypted payload.
    with pytest.raises(UnknownSecretError):
        SecretRegistry(workspace / "secrets", master_key="old-key").get(
            record.secret_id
        )
    # The new key reads it back.
    assert (
        SecretRegistry(workspace / "secrets", master_key="new-key")
        .get(record.secret_id)
        .value
        == "v1"
    )
    # Rotation window: new primary + old key as legacy still resolves.
    window = SecretRegistry(
        workspace / "secrets",
        master_key="new-key",
        legacy_master_keys=("old-key",),
    )
    assert window.get(record.secret_id).value == "v1"


def test_legacy_plaintext_record_upgraded_to_encrypted(workspace):
    legacy = SecretRegistry(workspace / "secrets")
    record = legacy.create("mig", "old-value")

    modern = SecretRegistry(workspace / "secrets", master_key="master-1")
    assert modern.get_by_name("mig").value == "old-value"
    payload = json.loads(
        (workspace / "secrets" / record.secret_id / "secret.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["encrypted"] is True
    assert payload["value"] != "old-value"


def test_secret_value_size_limited(workspace):
    registry = SecretRegistry(workspace / "secrets", master_key="master-1")
    with pytest.raises(ValueError):
        registry.create("big", "x" * (MAX_SECRET_VALUE_BYTES + 1))
    record = registry.create("ok", "y")
    with pytest.raises(ValueError):
        registry.update(record.secret_id, "z" * (MAX_SECRET_VALUE_BYTES + 1))
