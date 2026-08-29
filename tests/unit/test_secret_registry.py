"""Secret registry behavior."""

from __future__ import annotations

import pytest

from control_plane.registry.secrets import SecretRegistry, UnknownSecretError


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

