"""N25/L2c: the incremental ledger must equal the whole-tree walk, byte for byte.

The point of the ledger is to be *the same number* for less work, so every
test here compares it against ``priv_helpers.dir_size`` -- the walk it replaces
-- after a mutation sequence. An accounting that drifts is worse than a slow
one: nothing notices until a quota is decided on it.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from envd_service.priv_helpers import dir_size
from envd_service.runtime.dir_ledger import (
    DirLedger,
    DirLedgerUnknown,
    scan_subtree,
)


def _tree(root: Path) -> None:
    (root / "a").mkdir(parents=True)
    (root / "a" / "f1.txt").write_bytes(b"x" * 10)
    (root / "a" / "deep").mkdir()
    (root / "a" / "deep" / "f2.txt").write_bytes(b"y" * 200)
    (root / "top.txt").write_bytes(b"z" * 5)


def _assert_matches_walk(root: Path, ledger: DirLedger) -> None:
    assert ledger.ready
    assert ledger.total_bytes == dir_size(root)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    path = tmp_path / "sbx"
    path.mkdir()
    _tree(path)
    return path


def test_scan_subtree_reports_every_directory_once(root):
    found = scan_subtree(root, "")
    assert found.bytes_by_dir == {"": 5, "a": 10, "a/deep": 200}
    assert found == scan_subtree(root, "")


def test_scan_subtree_of_a_leaf(root):
    scanned = scan_subtree(root, "a/deep")
    assert scanned.bytes_by_dir == {"a/deep": 200}
    assert scanned.bytes == 200
    # N31: the same walk answers "how many names", which the byte ledger
    # cannot see on its own.
    assert scanned.files == 1


def test_entries_are_files_plus_directories(root):
    ledger = DirLedger(root)
    ledger.rebuild()
    # `_tree` builds: <root>/a.bin, <root>/a/b.bin, <root>/a/deep/c.bin, plus
    # the three directories themselves.
    assert ledger.total_entries == 6
    assert ledger.directory_count == 3


def test_an_empty_file_moves_entries_and_not_bytes(root):
    # The whole reason the entry cap exists: zero bytes, one name.
    ledger = DirLedger(root)
    ledger.rebuild()
    before_bytes = ledger.total_bytes
    before_entries = ledger.total_entries
    (root / "empty.bin").write_bytes(b"")
    ledger.apply([root])
    assert ledger.total_bytes == before_bytes
    assert ledger.total_entries == before_entries + 1


def test_rebuild_matches_the_walk(root):
    ledger = DirLedger(root)
    assert ledger.rebuild() == dir_size(root)
    _assert_matches_walk(root, ledger)


def test_a_new_file_is_absorbed_by_its_dirty_directory(root):
    ledger = DirLedger(root)
    ledger.rebuild()

    (root / "a" / "new.txt").write_bytes(b"n" * 40)
    ledger.apply([root / "a"])

    _assert_matches_walk(root, ledger)


def test_a_new_deep_directory_is_absorbed_by_its_parent(root):
    ledger = DirLedger(root)
    ledger.rebuild()

    (root / "a" / "deeper").mkdir()
    (root / "a" / "deeper" / "x.bin").write_bytes(b"q" * 300)
    # The mediator marks the parent its `mkdir` resolved to.
    ledger.apply([root / "a"])

    _assert_matches_walk(root, ledger)


def test_a_rename_moves_the_whole_subtree(root):
    ledger = DirLedger(root)
    ledger.rebuild()

    os.rename(root / "a", root / "b")
    ledger.apply([root])  # rename(2) marks both parents

    _assert_matches_walk(root, ledger)
    assert ledger.total_bytes == 215


def test_a_whole_branch_removed_is_absorbed(root):
    ledger = DirLedger(root)
    ledger.rebuild()

    import shutil

    shutil.rmtree(root / "a")
    # `rm -rf` marks every level it removed from.
    ledger.apply([root / "a", root / "a" / "deep", root])

    _assert_matches_walk(root, ledger)
    assert ledger.total_bytes == 5


def test_reducing_the_mark_set_is_the_shallowest_only(root):
    ledger = DirLedger(root)
    ledger.rebuild()
    (root / "a" / "deep" / "f3.txt").write_bytes(b"m" * 20)

    ledger.apply([root / "a", root / "a" / "deep"])

    _assert_matches_walk(root, ledger)
    # Scanned once, at `a`: a double application would have counted the
    # subtree twice and blown the total past the walk.
    assert ledger.total_bytes == 235


def test_marks_outside_the_root_are_ignored(root, tmp_path):
    ledger = DirLedger(root)
    ledger.rebuild()

    ledger.apply([tmp_path / "elsewhere", root / "a" / "f1.txt"])

    _assert_matches_walk(root, ledger)


def test_an_open_writer_that_comes_back_later_is_still_accounted(root):
    """The cluster failure this grace window exists for, as a unit test.

    Measured on the real fleet: a process opened a file, wrote 1000 bytes,
    slept 12 s (longer than the 5 s scan cadence), appended 5000 more and
    closed. The `open` marked the directory once; the later write emits no path
    syscall at all, and an accounting that stopped re-checking as soon as one
    round showed no growth missed all 5000 bytes.
    """
    now = [1000.0]
    ledger = DirLedger(root, grace_s=120.0, clock=lambda: now[0])
    ledger.rebuild()

    log = root / "a" / "log.txt"
    log.write_bytes(b"l" * 1000)
    ledger.apply([root / "a"])  # the mediator saw the open
    assert ledger.total_bytes == dir_size(root)

    now[0] += 12.0  # longer than any single scan interval
    with open(log, "ab") as handle:
        handle.write(b"l" * 5000)
    ledger.apply([])  # no new marks at all
    assert ledger.total_bytes == dir_size(root)

    # ...and the re-checking is bounded: once the window passes, the
    # directory stops being scanned.
    now[0] += 121.0
    ledger.apply([])
    assert ledger.recheck_count == 0


def test_rescan_next_re_checks_a_directory_until_the_window_ends(root):
    """The rebuild window: a write between the drain and the walk's passing."""
    now = [500.0]
    ledger = DirLedger(root, grace_s=60.0, clock=lambda: now[0])
    ledger.rebuild()

    ledger.rescan_next([root / "a"])
    assert ledger.recheck_count == 1

    now[0] += 30.0
    ledger.apply([])
    assert ledger.recheck_count == 1, "still inside the window"
    assert ledger.total_bytes == dir_size(root)

    now[0] += 31.0
    ledger.apply([])
    assert ledger.recheck_count == 0


def test_an_unreadable_subtree_raises_and_invalidates(root):
    ledger = DirLedger(root)
    ledger.rebuild()
    (root / "a" / "deep").chmod(0o000)
    if os.geteuid() == 0:  # pragma: no cover - root ignores the mode
        pytest.skip("root can read a 0000 directory")
    try:
        with pytest.raises(DirLedgerUnknown):
            ledger.apply([root / "a"])
    finally:
        (root / "a" / "deep").chmod(0o755)


def test_random_mutations_stay_equal_to_the_walk(root):
    """The property the design asks for, over sequences rather than cases."""
    rng = random.Random(20260919)
    ledger = DirLedger(root)
    ledger.rebuild()

    dirs = ["", "a", "a/deep", "a/deep/one", "b", "b/two"]
    for step in range(120):
        rel = rng.choice(dirs)
        target = root / rel if rel else root
        target.mkdir(parents=True, exist_ok=True)
        dirty = [str(target)]
        action = rng.choice(["write", "write", "mkdir", "remove", "rename"])
        if action == "write":
            (target / f"f{rng.randrange(5)}.bin").write_bytes(b"x" * rng.randrange(500))
        elif action == "mkdir":
            (target / f"d{rng.randrange(4)}").mkdir(exist_ok=True)
        elif action == "remove":
            victim = target / f"f{rng.randrange(5)}.bin"
            if victim.exists():
                victim.unlink()
        else:
            source = target / f"f{rng.randrange(5)}.bin"
            if source.exists():
                os.rename(source, target / f"moved{rng.randrange(5)}.bin")
        # The mediator marks the directory that contained the change; for a
        # rename it marks both ends' parents, which is the same directory here.
        ledger.apply(dirty)
        assert ledger.total_bytes == dir_size(root), f"diverged at step {step}"
