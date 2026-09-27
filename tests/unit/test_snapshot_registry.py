"""T3: snapshot stores copied into a snapshot nest exponentially
(``snap_X/fs/snap_X/fs/...`` until ENAMETOOLONG).

Guards (see docs/superpowers/plans/2026-09-04-sandlock-remaining-goals.md
Task 0.2): ``create_from_sandbox`` / ``expand_to`` refuse a destination that
is inside the copy source, and both prune embedded snapshot-store roots from
copies so a snapshot never carries the registry store into itself.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.registry.snapshots import SnapshotRegistry
from control_plane.api.snapshots import _copy_local_payload


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


def test_the_local_async_payload_copy_is_a_no_op_when_it_is_already_there(tmp_path):
    """The async shape's local branch: copy once, and never into itself.

    The reserved capture copies the payload itself (the registry's
    `create_from_sandbox` is the sync path), so the two guards that matter are
    pinned here: a retried id whose payload is already on disk copies nothing,
    and a payload that would land inside its own source is refused rather than
    copied into itself (the G2 self-nesting incident).
    """
    registry = SnapshotRegistry(tmp_path / "control")
    state = SimpleNamespace(snapshots=registry)
    workspace = tmp_path / "sbx_local"
    (workspace / "workspace").mkdir(parents=True)
    (workspace / "workspace" / "f.txt").write_text("one", encoding="utf-8")

    _copy_local_payload(state, workspace, "snap_local")
    payload = registry.payload_path("snap_local")
    assert (payload / "workspace" / "f.txt").read_text(encoding="utf-8") == "one"

    # A second call (the retry) must not re-copy -- it would also have to
    # tolerate the existing destination, which the strict copytree does not.
    (workspace / "workspace" / "f.txt").write_text("two", encoding="utf-8")
    _copy_local_payload(state, workspace, "snap_local")
    assert (payload / "workspace" / "f.txt").read_text(encoding="utf-8") == "one"

    # The guard is about the *destination* being under the source: point a
    # snapshot id at the workspace itself by making the store live there.
    nested = SnapshotRegistry(workspace)
    with pytest.raises(ValueError, match="inside its source"):
        _copy_local_payload(
            SimpleNamespace(snapshots=nested), workspace, "snap_self"
        )


def test_a_snapshot_record_is_published_in_one_step(tmp_path, publish_spy):
    """The ``creating`` record is the half another replica polls from (F11.3).

    ``get()`` re-reads a ``creating`` record from the shared volume on purpose
    -- its owner flips it when the bytes are in -- so the copy going the other
    way is a reader of the file being rewritten. In the write window it has to
    find the previous *whole* record: half a file only raises, and that raise
    is a pole answering "no such snapshot" about one that is being copied
    right now.
    """
    base = tmp_path / "control"
    registry = SnapshotRegistry(base)
    record = registry.reserve_from_sandbox(
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        source_sandbox_id=None,
        snapshot_id="snap_00000000000000ff",
    )
    record_path = registry._snapshot_dir(record.snapshot_id) / "snapshot.json"
    publish_spy.reset()

    with publish_spy.hold_next_publish() as in_window:
        writer = threading.Thread(
            target=registry.mark_completed, args=(record.snapshot_id,), daemon=True
        )
        writer.start()
        publish_spy.await_publish(in_window, "a snapshot record")
        on_disk = json.loads(record_path.read_text(encoding="utf-8"))
        assert on_disk["status"] == "creating"
        assert SnapshotRegistry(base).get(record.snapshot_id).status == "creating"
    writer.join(timeout=10)
    assert not writer.is_alive()

    assert SnapshotRegistry(base).get(record.snapshot_id).status == "completed"
