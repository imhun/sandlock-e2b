"""Volume registry behavior."""

from __future__ import annotations

import pytest

from control_plane.registry.volumes import UnknownVolumeError, VolumeRegistry


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
