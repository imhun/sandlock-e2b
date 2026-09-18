"""Filesystem ops: the reads, and the decisions every write makes (N28).

The operations themselves moved into the sandbox
(``tests/unit/test_sandbox_writer.py`` covers them end to end): what stays here
is the read side and the guards, i.e. the part of a write that decides *which
documented error the caller gets*. Those are asserted verbatim -- the API
contract is the code and the message together.
"""

from __future__ import annotations

import pytest

from envd_service.filesystem.ops import FilesystemOps
from gateway_common.errors import ConnectError


@pytest.fixture()
def ops(workspace):
    root = workspace / "fs-root"
    root.mkdir()
    return FilesystemOps(root)


def test_stat_file(ops):
    (ops.root / "a.txt").write_text("hello")
    entry = ops.stat("a.txt")
    assert entry["name"] == "a.txt"
    assert entry["type"] == "FILE_TYPE_FILE"
    assert entry["path"] == "a.txt"
    assert entry["size"] == "5"
    assert entry["permissions"] == "rw-r--r--"


def test_stat_missing_is_not_found(ops):
    with pytest.raises(ConnectError) as exc:
        ops.stat("missing.txt")
    assert exc.value.code == "not_found"


def test_require_creatable_returns_the_resolved_target(ops):
    assert ops.require_creatable("a/b/c") == ops.root / "a" / "b" / "c"


def test_require_creatable_existing_is_already_exists(ops):
    (ops.root / "a").mkdir()
    with pytest.raises(ConnectError) as exc:
        ops.require_creatable("a")
    assert exc.value.code == "already_exists"
    assert exc.value.message == "Path a already exists"


def test_require_movable_returns_both_resolved_ends(ops):
    (ops.root / "a.txt").write_text("x")
    assert ops.require_movable("a.txt", "dir/b.txt") == (
        ops.root / "a.txt",
        ops.root / "dir" / "b.txt",
    )


def test_require_movable_missing_is_not_found(ops):
    with pytest.raises(ConnectError) as exc:
        ops.require_movable("nope.txt", "b.txt")
    assert exc.value.code == "not_found"
    assert exc.value.message == "Path nope.txt not found"


def test_require_movable_occupied_destination_is_already_exists(ops):
    (ops.root / "a.txt").write_text("x")
    (ops.root / "b.txt").write_text("y")
    with pytest.raises(ConnectError) as exc:
        ops.require_movable("a.txt", "b.txt")
    assert exc.value.code == "already_exists"
    assert exc.value.message == "Path b.txt already exists"


def test_require_removable_returns_the_resolved_target(ops):
    (ops.root / "a.txt").write_text("x")
    assert ops.require_removable("a.txt") == ops.root / "a.txt"


def test_require_removable_missing_is_not_found(ops):
    with pytest.raises(ConnectError) as exc:
        ops.require_removable("a.txt")
    assert exc.value.code == "not_found"
    assert exc.value.message == "Path a.txt not found"


def test_list_dir_depth(ops):
    (ops.root / "a").mkdir()
    (ops.root / "a" / "b.txt").write_text("x")
    (ops.root / "top.txt").write_text("y")
    entries = ops.list_dir("", 1)["entries"]
    assert [e["path"] for e in entries] == ["a", "top.txt"]
    entries = ops.list_dir("", 0)["entries"]
    assert {e["path"] for e in entries} == {"a", "a/b.txt", "top.txt"}


def test_list_dir_missing_is_not_found(ops):
    with pytest.raises(ConnectError) as exc:
        ops.list_dir("missing", 1)
    assert exc.value.code == "not_found"


def test_traversal_rejected(ops):
    with pytest.raises(ConnectError) as exc:
        ops.stat("../outside")
    assert exc.value.code == "invalid_argument"
