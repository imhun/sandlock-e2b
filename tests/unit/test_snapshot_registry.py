"""T3: snapshot stores copied into a snapshot nest exponentially
(``snap_X/fs/snap_X/fs/...`` until ENAMETOOLONG).

Guards (see docs/superpowers/plans/2026-09-04-sandlock-remaining-goals.md
Task 0.2): ``create_from_sandbox`` / ``expand_to`` refuse a destination that
is inside the copy source, and both prune embedded snapshot-store roots from
copies so a snapshot never carries the registry store into itself.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from control_plane.registry.snapshots import SnapshotRegistry


def _registry(base: Path) -> SnapshotRegistry:
    return SnapshotRegistry(base)


def _snapshot_from(reg: SnapshotRegistry, workspace: Path, sid: str):
    return reg.create_from_sandbox(
        workspace_dir=workspace,
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        snapshot_id=sid,
    )


def _seed(path: Path) -> Path:
    (path / "workspace").mkdir(parents=True)
    (path / "workspace" / "f.txt").write_text("seed", encoding="utf-8")
    return path


def _store_dir(reg: SnapshotRegistry, sid: str) -> Path:
    """The registry's own on-disk directory for one snapshot.

    These tests copy a whole *store directory* into a workspace to reproduce
    the nesting incident, so they need the real path rather than a hard-coded
    one -- it moved under ``_snapshots/`` when the control plane started
    mounting the shared volume read-only (OBS-9).
    """
    return reg._snapshot_dir(sid)


def test_expand_to_rejects_destination_inside_source(tmp_path):
    reg = _registry(tmp_path / "store")
    src = tmp_path / "sbx_1"
    (src / "workspace").mkdir(parents=True)
    (src / "workspace" / "a.txt").write_text("x", encoding="utf-8")
    rec = _snapshot_from(reg, src, "snap_a")

    with pytest.raises(ValueError, match="destination .* inside its source"):
        reg.expand_to(rec, Path(rec.fs_path) / "nested")


def test_snapshot_of_workspace_containing_store_prunes_store(tmp_path):
    """工作区里混进快照存储时，复制必须剪掉存储，不得自我嵌套。"""
    store = tmp_path / "snapshots"
    reg = _registry(store)
    victim = _snapshot_from(reg, _seed(tmp_path / "sbx_victim"), "snap_victim")

    # 事故形态：某沙箱工作区把存储目录整个带了进来
    ws = tmp_path / "sbx_2"
    (ws / "workspace").mkdir(parents=True)
    (ws / "workspace" / "keep.txt").write_text("keep", encoding="utf-8")
    shutil.copytree(
        _store_dir(reg, "snap_victim"),
        ws / "snapshots" / "snap_victim",
        symlinks=True,
    )
    (ws / "data").mkdir()  # 普通目录必须原样保留：守卫不得过度剪枝
    (ws / "data" / "keep.bin").write_bytes(b"1")

    out = _snapshot_from(reg, ws, "snap_2")
    assert (out.fs_path / "workspace" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "keep"
    assert (out.fs_path / "data" / "keep.bin").read_bytes() == b"1"
    assert not (out.fs_path / "snapshots").exists()
    assert list(out.fs_path.rglob("snapshot.json")) == []


def test_store_named_dir_without_snapshot_root_is_preserved(tmp_path):
    """正例：只叫 ``snapshots``、内部没有快照根的目录不是存储，不得剪枝。"""
    store = tmp_path / "snapshots"
    reg = _registry(store)
    ws = tmp_path / "sbx_3"
    (ws / "workspace").mkdir(parents=True)
    (ws / "workspace" / "a.txt").write_text("x", encoding="utf-8")
    # A plain directory that merely shares the store's name.
    (ws / "snapshots" / "notes").mkdir(parents=True)
    (ws / "snapshots" / "notes" / "keep.txt").write_text("n", encoding="utf-8")

    out = _snapshot_from(reg, ws, "snap_3")
    assert (out.fs_path / "snapshots" / "notes" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "n"

    # And a normal expand round-trip copies content into a fresh workspace.
    expanded = reg.expand_to(out, tmp_path / "sbx_restored")
    assert (expanded / "workspace" / "a.txt").read_text(encoding="utf-8") == "x"
    assert (expanded / "snapshots" / "notes" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "n"


def test_nested_store_markers_are_pruned_within_bounded_depth(tmp_path):
    """#14: a store carried in nested below the direct-child shape (markers
    intact) is pruned by the bounded-depth scan — the whole container
    directory goes instead of copying store bytes into the snapshot."""
    store = tmp_path / "snapshots"
    reg = _registry(store)
    victim = _snapshot_from(reg, _seed(tmp_path / "sbx_victim"), "snap_victim")

    ws = tmp_path / "sbx_nested"
    (ws / "workspace").mkdir(parents=True)
    (ws / "workspace" / "keep.txt").write_text("keep", encoding="utf-8")
    # The container directory is not itself a store; the snapshot root sits
    # two levels below it (mirror/snap_victim/snapshot.json).
    shutil.copytree(
        _store_dir(reg, "snap_victim"),
        ws / "cache" / "mirror" / "snap_victim",
        symlinks=True,
    )

    out = _snapshot_from(reg, ws, "snap_nested")
    assert (out.fs_path / "workspace" / "keep.txt").read_text(
        encoding="utf-8"
    ) == "keep"
    assert not (out.fs_path / "cache").exists()
    assert list(out.fs_path.rglob("snapshot.json")) == []
