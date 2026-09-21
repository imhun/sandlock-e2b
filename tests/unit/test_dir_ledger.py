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


def _blocks(path: Path) -> int:
    """One directory's cost: its allocation, which is what `du` reports."""
    return os.stat(path).st_blocks * 512


def _directory_bytes(root: Path) -> int:
    """The directories' own cost, probed independently of the ledger.

    ``st_blocks x 512``, not ``st_size``: on the cluster's NAS a directory's
    ``st_size`` (4096 empty, 16384 at 2000 entries) is not the space it
    occupies or what ``du`` reports -- its allocation stayed 512 the whole way
    (``brief_stat.directory_cost``). The expected totals below are spelled as
    ``<file bytes> + this``, so the assertion still says what the *file* part
    is (the number the old, file-only accounting produced) while pinning that
    each directory is charged exactly once (N31 fix 2). ``os.stat``, not the
    implementation's own probe, is the oracle here: a test that computed the
    expectation with the code under test would only prove the sum was
    performed.
    """
    return sum(
        os.stat(dirpath).st_blocks * 512
        for dirpath, _dirs, _files in os.walk(root)
    )


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    path = tmp_path / "sbx"
    path.mkdir()
    _tree(path)
    return path


def test_scan_subtree_reports_every_directory_once(root):
    """One entry per directory, and that entry is the directory's own cost.

    ``walk`` is post-order as well as pre-order (FTS reports ``FTS_DP`` after a
    directory's contents), so "once" is a property the fixture has to hold: a
    double visit would charge every directory twice.
    """
    found = scan_subtree(root, "")
    assert found.bytes_by_dir == {
        "": 5 + _blocks(root),
        "a": 10 + _blocks(root / "a"),
        "a/deep": 200 + _blocks(root / "a" / "deep"),
    }
    assert found == scan_subtree(root, "")


def test_scan_subtree_of_a_leaf(root):
    scanned = scan_subtree(root, "a/deep")
    assert scanned.bytes_by_dir == {"a/deep": 200 + _blocks(root / "a" / "deep")}
    assert scanned.bytes == 200 + _blocks(root / "a" / "deep")
    # N31: the same walk answers "how many names", which the byte ledger
    # cannot see on its own.
    assert scanned.files == 1


def test_a_tree_of_empty_directories_is_charged_what_it_allocates(tmp_path):
    """N31 fix 2, on the shape that motivated it (and its exact limit).

    Measured on the cluster: 2000 empty entries moved the platform number by
    **0 bytes** while the tree plainly existed. The names are the entry cap's
    job (fix 1); this fix charges what the directories *allocate*, so the
    number is the one `du` reports -- on the cluster's NAS 512 bytes per
    directory, measured unchanged from 0 to 2000 entries, where the
    directory's `st_size` (4096 -> 16384) is not space at all.

    The assertion is deliberately the contract rather than a positive number:
    on a filesystem whose small directories are held in the inode (XFS
    shortform: `st_blocks` 0, `du` 0) there is genuinely nothing to charge, and
    a test that demanded bytes would be asserting the storage's shape instead
    of the accounting's.
    """
    root = tmp_path / "sbx"
    (root / "empty" / "deeper").mkdir(parents=True)

    expected = _directory_bytes(root)
    assert dir_size(root) == expected

    ledger = DirLedger(root)
    assert ledger.rebuild() == expected
    assert ledger.total_bytes == expected
    _assert_matches_walk(root, ledger)


def test_entries_are_files_plus_directories(root):
    ledger = DirLedger(root)
    ledger.rebuild()
    # `_tree` builds: <root>/a.bin, <root>/a/b.bin, <root>/a/deep/c.bin, plus
    # the three directories themselves.
    assert ledger.total_entries == 6
    assert ledger.directory_count == 3


def test_an_empty_file_moves_entries_and_not_bytes(root):
    # The whole reason the entry cap exists: zero bytes, one name. The only
    # thing that may move the byte number here is the directory's own growth
    # (a filesystem that needs another block to hold the name), which is why
    # the assertion is the delta of its `st_size` rather than a constant.
    ledger = DirLedger(root)
    ledger.rebuild()
    before_bytes = ledger.total_bytes
    before_entries = ledger.total_entries
    before_dir_bytes = _blocks(root)
    (root / "empty.bin").write_bytes(b"")
    ledger.apply([root])
    assert ledger.total_bytes == before_bytes + (
        _blocks(root) - before_dir_bytes
    )
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
    assert ledger.total_bytes == 215 + _directory_bytes(root)


def test_a_whole_branch_removed_is_absorbed(root):
    ledger = DirLedger(root)
    ledger.rebuild()

    import shutil

    shutil.rmtree(root / "a")
    # `rm -rf` marks every level it removed from.
    ledger.apply([root / "a", root / "a" / "deep", root])

    _assert_matches_walk(root, ledger)
    assert ledger.total_bytes == 5 + _directory_bytes(root)


def test_reducing_the_mark_set_is_the_shallowest_only(root):
    ledger = DirLedger(root)
    ledger.rebuild()
    (root / "a" / "deep" / "f3.txt").write_bytes(b"m" * 20)

    ledger.apply([root / "a", root / "a" / "deep"])

    _assert_matches_walk(root, ledger)
    # Scanned once, at `a`: a double application would have counted the
    # subtree twice and blown the total past the walk.
    assert ledger.total_bytes == 235 + _directory_bytes(root)


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
    """The property the design asks for, over sequences rather than cases.

    The marks are the mediator's: the directory that *contained* the change.
    Since N31 fix 2 that discipline is load-bearing -- a directory's own
    ``st_size`` is part of the number, so a change that adds or removes a name
    *inside* a directory must mark that directory (the mediator does: every
    entry-changing handler records the parent of the path it resolved). The
    fixture therefore builds the whole `dirs` skeleton up front instead of
    letting `mkdir(parents=True)` create an intermediate directory mid-round
    that nothing ever marks, which would be a producer this ledger is not
    supposed to be able to follow.
    """
    rng = random.Random(20260919)
    dirs = ["", "a", "a/deep", "a/deep/one", "b", "b/two"]
    for rel in dirs:
        (root / rel if rel else root).mkdir(parents=True, exist_ok=True)

    ledger = DirLedger(root)
    ledger.rebuild()

    for step in range(120):
        rel = rng.choice(dirs)
        target = root / rel if rel else root
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
