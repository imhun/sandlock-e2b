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

