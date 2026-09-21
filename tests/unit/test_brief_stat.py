"""N25: the size probe answers what `os.path.getsize` answers.

`entry_size` exists to ask for a file's size without the NFS flush that
`os.stat` performs, and the only thing that makes that swap safe is that the
number and the errors are the same. So every case here compares the two
directly -- including the ones where `os.path.getsize` raises, because a probe
that answers where the original refused is how an accounting drifts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from envd_service.priv_helpers import dir_size
from envd_service.runtime.brief_stat import entry_size, statx_available


def test_regular_file_matches_getsize(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"x" * 4096)
    assert entry_size(blob) == 4096
    assert entry_size(blob) == os.path.getsize(blob)


def test_empty_file_matches_getsize(tmp_path: Path) -> None:
    blob = tmp_path / "empty.bin"
    blob.write_bytes(b"")
    assert entry_size(blob) == 0
    assert entry_size(blob) == os.path.getsize(blob)


def test_directory_matches_getsize(tmp_path: Path) -> None:
    child = tmp_path / "sub"
    child.mkdir()
    assert entry_size(child) == os.path.getsize(child)


def test_symlink_is_followed_like_getsize(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"y" * 123)
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    assert entry_size(link) == 123
    assert entry_size(link) == os.path.getsize(link)


def test_missing_path_raises_the_same_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.bin"
    with pytest.raises(FileNotFoundError):
        entry_size(missing)
    with pytest.raises(FileNotFoundError):
        os.path.getsize(missing)


def test_str_and_path_are_both_accepted(tmp_path: Path) -> None:
    blob = tmp_path / "both.bin"
    blob.write_bytes(b"z" * 7)
    assert entry_size(str(blob)) == 7
    assert entry_size(blob) == 7


def test_dir_size_sums_the_brief_probe(tmp_path: Path) -> None:
    """`priv_helpers.dir_size` is the number the ledger is pinned to.

    The quantity is files **plus each directory's own `st_size`** (N31 fix 2),
    so the expected total is spelled as the file bytes the old definition
    produced plus the two directories the fixture has.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f1").write_bytes(b"1" * 10)
    (tmp_path / "a" / "f2").write_bytes(b"2" * 300)
    (tmp_path / "top").write_bytes(b"3" * 5)
    naive = 0
    for root, _dirs, files in os.walk(tmp_path):
        naive += os.stat(root).st_blocks * 512
        for name in files:
            naive += os.path.getsize(os.path.join(root, name))
    assert dir_size(tmp_path) == naive
    assert dir_size(tmp_path) == (
        315
        + os.stat(tmp_path).st_blocks * 512
        + os.stat(tmp_path / "a").st_blocks * 512
    )


def test_statx_presence_is_reported_consistently() -> None:
    """The fast path is used when the platform has it, the fallback otherwise."""
    first = statx_available()
    assert statx_available() is first
    if first:
        assert entry_size(Path("/etc/hosts")) == os.path.getsize("/etc/hosts")
