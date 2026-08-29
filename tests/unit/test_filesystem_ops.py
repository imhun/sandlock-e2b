"""Filesystem ops: stat / list / move / remove / makedir."""

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


def test_make_dir_recursive(ops):
    entry = ops.make_dir("a/b/c")
    assert entry["type"] == "FILE_TYPE_DIRECTORY"
    assert (ops.root / "a" / "b" / "c").is_dir()


def test_make_dir_existing_is_already_exists(ops):
    ops.make_dir("a")
    with pytest.raises(ConnectError) as exc:
        ops.make_dir("a")
    assert exc.value.code == "already_exists"


def test_move_file(ops):
    (ops.root / "a.txt").write_text("x")
    entry = ops.move("a.txt", "dir/b.txt")
    assert entry["path"] == "dir/b.txt"
    assert (ops.root / "dir" / "b.txt").is_file()


def test_move_missing_is_not_found(ops):
    with pytest.raises(ConnectError) as exc:
        ops.move("nope.txt", "b.txt")
    assert exc.value.code == "not_found"


def test_remove_file_and_missing(ops):
    (ops.root / "a.txt").write_text("x")
    ops.remove("a.txt")
    assert not (ops.root / "a.txt").exists()
    with pytest.raises(ConnectError) as exc:
        ops.remove("a.txt")
    assert exc.value.code == "not_found"


def test_remove_directory_recursive(ops):
    (ops.root / "dir" / "sub").mkdir(parents=True)
    (ops.root / "dir" / "f.txt").write_text("x")
    ops.remove("dir")
    assert not (ops.root / "dir").exists()


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

