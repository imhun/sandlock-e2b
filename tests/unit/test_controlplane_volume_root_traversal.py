"""W4/2: the control plane's own volume roots need the A5 traversal bits.

``VolumeRegistry.create`` makes ``<volume root>`` in the control-plane
process and shares it with sandboxes through ``chmod 1777``, but the sandbox
opens that host path *as its own uid* (E3.2 / own-identity slot), so DAC needs
``o+x`` on **every** ancestor as well -- the root alone is not enough (A5,
``docs/production-deployment-requirements.md`` §2.4.2). A stack deployment
gets that from the worker's mount path (``envd_service.volumes``); the
combined ("合体") node creates the root here and never runs that half, so the
control plane has to apply the same rule to the same path.

Assertions are on **mode bits**, never ``os.access``: these lanes run as
root, where ``os.access`` says "yes" for a 0700 directory the process does
not own. The widening is best-effort and only ever *adds* x bits.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import control_plane.registry.volumes as volumes_module
from control_plane.registry.volumes import VolumeRegistry

#: group-x + other-x: a tenant uid owns nothing here and has no supplementary
#: groups (single-entry userns), so the other bit is the one that matters.
TRAVERSAL_BITS = 0o011


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_traversable(path: Path) -> None:
    mode = _mode(path)
    assert mode & TRAVERSAL_BITS == TRAVERSAL_BITS, (
        f"{path} is mode {mode:04o}: a tenant uid cannot traverse it"
    )


def _hardened_base(tmp_path: Path) -> tuple[Path, Path]:
    """``<tmp>/root/_volumes`` with both levels left at 0700 (umask 077)."""
    root = tmp_path / "root"
    base = root / "_volumes"
    base.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(base, 0o700)
    return root, base


def test_volume_creation_covers_the_whole_chain(tmp_path):
    """A new volume root is 1777 *and* reachable from ``/`` downwards."""
    root, base = _hardened_base(tmp_path)

    record = VolumeRegistry(base).create(name="data")

    assert _mode(record.path) == 0o1777
    for level in (record.path, base, root):
        _assert_traversable(level)
    # Only x bits are added: a level that was already wider keeps its mode.
    assert _mode(base) == 0o711
    assert _mode(root) == 0o711


def test_volume_creation_keeps_the_record_and_leaves_the_root_writable(
    tmp_path,
):
    """The widening must not disturb the volume itself (sticky shared root)."""
    _root, base = _hardened_base(tmp_path)

    registry = VolumeRegistry(base)
    record = registry.create(name="data", per_sandbox_quota_mb=64)

    assert record.per_sandbox_quota_mb == 64
    assert registry.get(record.volume_id).name == "data"
    assert _mode(record.path) == 0o1777


def test_the_widening_never_touches_a_per_sandbox_slice(tmp_path):
    """It walks upward only: a slice keeps its own 0700 shape."""
    root, base = _hardened_base(tmp_path)
    registry = VolumeRegistry(base)
    record = registry.create(name="data")
    slice_dir = record.path / "sbx_w4"
    slice_dir.mkdir()
    os.chmod(slice_dir, 0o700)

    # A second volume must not loosen the first one's slice.
    registry.create(name="data2")

    assert _mode(slice_dir) == 0o700
    _assert_traversable(root)


def test_creation_without_the_widening_step_fails_the_same_assertion(
    tmp_path, monkeypatch
):
    """Mutation control: the pre-W4 control plane (no widening) is RED here."""
    monkeypatch.setattr(
        volumes_module, "_widen_ancestors_for_tenant_uids", lambda _path: None
    )
    root, base = _hardened_base(tmp_path)

    record = VolumeRegistry(base).create(name="data")

    # The shared-root model is still applied; only the chain fixup is missing.
    assert _mode(record.path) == 0o1777
    assert _mode(base) == 0o700
    with pytest.raises(AssertionError):
        _assert_traversable(base)
    with pytest.raises(AssertionError):
        _assert_traversable(root)
