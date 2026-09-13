"""Path traversal and sandbox ID safety."""

from __future__ import annotations

import json
import os

import pytest

from gateway_common.paths import (
    PathTraversalError,
    is_sandbox_workspace_dir,
    resolve_under_root,
    validate_sandbox_id,
)


def test_normal_paths_resolve(workspace):
    assert resolve_under_root(workspace, "workspace/a.txt") == workspace / "workspace" / "a.txt"
    assert resolve_under_root(workspace, "a/b/c.txt") == workspace / "a" / "b" / "c.txt"


def test_absolute_path_treated_as_relative(workspace):
    assert resolve_under_root(workspace, "/etc/passwd") == workspace / "etc" / "passwd"


def test_traversal_rejected(workspace):
    for bad in ("../etc/passwd", "a/../../etc/passwd", ".."):
        with pytest.raises(PathTraversalError):
            resolve_under_root(workspace, bad)


def test_null_byte_rejected(workspace):
    with pytest.raises(PathTraversalError):
        resolve_under_root(workspace, "a\x00b")


def test_symlink_escape_rejected(workspace, tmp_path):
    outside = workspace.parent / f"outside-{workspace.name}"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret")
    link = workspace / "escape"
    link.symlink_to(outside)
    with pytest.raises(PathTraversalError):
        resolve_under_root(workspace, "escape/secret.txt")


def test_sandbox_id_validation():
    assert validate_sandbox_id("sbx_abc123")
    assert validate_sandbox_id("sbx-1")
    assert not validate_sandbox_id("")
    assert not validate_sandbox_id("../etc")
    assert not validate_sandbox_id("a/b")
    assert not validate_sandbox_id("a b")


def test_is_sandbox_workspace_dir_separates_by_shape_not_by_name(tmp_path):
    """M1: a reserved prefix only excludes a directory that is *not* a tree.

    ``_`` and ``snap_`` are legal sandbox-id characters and neither prefix is
    reserved on the create side (``X-Sandbox-Id`` goes through
    ``validate_sandbox_id`` alone), so the name cannot decide on its own: a
    prefixed directory carrying its own top-level ``sandbox.json`` is the
    (client-chosen-id) sandbox tree it looks like, and only a prefixed
    directory *without* one — the infrastructure namespaces and the snapshot
    store, whose copied record sits at ``snap_X/fs/sandbox.json`` — stays out.
    """

    def _record(directory, *, name: str | None = None) -> None:
        (directory / (name or "sandbox.json")).write_text(
            json.dumps({"sandbox_id": directory.name}), encoding="utf-8"
        )

    plain = tmp_path / "sbx_plain"
    plain.mkdir()
    plain_tree = tmp_path / "sbx_with_record"
    plain_tree.mkdir()
    _record(plain_tree)
    for reserved in ("_volumes", "_snapshots", "_migrate", "_templates", "_secrets"):
        (tmp_path / reserved).mkdir()
    # The snapshot store: its marker and the copied filesystem, whose sandbox
    # record never sits at the store's top level.
    store = tmp_path / "snap_0040ce7e44f6365f"
    (store / "fs").mkdir(parents=True)
    (store / "snapshot.json").write_text("{}", encoding="utf-8")
    _record(store / "fs")
    # Client-chosen ids under both reserved prefixes: real sandbox trees.
    snap_client = tmp_path / "snap_client1"
    snap_client.mkdir()
    _record(snap_client)
    underscore_client = tmp_path / "_client1"
    underscore_client.mkdir()
    _record(underscore_client)
    # The top-level `_snapshots` store the worker keeps for migration copies is
    # still a directory without a top-level record of its own.
    worker_store = tmp_path / "_snapshots" / "snap_a"
    (worker_store / "fs").mkdir(parents=True)
    _record(worker_store / "fs")
    # Not a sandbox tree at all.
    (tmp_path / "sbx_file").write_text("not a tree", encoding="utf-8")
    (tmp_path / "sbx_link").symlink_to(plain)
    (tmp_path / "snap_link").symlink_to(underscore_client)
    (tmp_path / "bad.name").mkdir()

    assert is_sandbox_workspace_dir(plain) is True
    assert is_sandbox_workspace_dir(plain_tree) is True
    assert is_sandbox_workspace_dir(snap_client) is True
    assert is_sandbox_workspace_dir(underscore_client) is True
    for reserved in (
        "_volumes",
        "_snapshots",
        "_migrate",
        "_templates",
        "_secrets",
        "snap_0040ce7e44f6365f",
        "bad.name",
    ):
        assert is_sandbox_workspace_dir(tmp_path / reserved) is False, reserved
    assert is_sandbox_workspace_dir(tmp_path / "_snapshots" / "snap_a") is False
    assert is_sandbox_workspace_dir(tmp_path / "sbx_file") is False
    assert is_sandbox_workspace_dir(tmp_path / "sbx_link") is False
    # A symlink dressed up under a reserved prefix names no tree of its own
    # either; the scan follows the link's target only through its real name.
    assert is_sandbox_workspace_dir(tmp_path / "snap_link") is False

    # Existence of the record is the shape signal, not this process's ability to
    # read it: a record that cannot be opened must stay *visible* to the scans
    # (reported as `unmaterialised`, never torn down) instead of slipping back
    # into the silent-orphan state the predicate exists to prevent.
    unreadable = tmp_path / "_client_unreadable"
    unreadable.mkdir()
    _record(unreadable)
    os.chmod(unreadable / "sandbox.json", 0)
    assert is_sandbox_workspace_dir(unreadable) is True
    os.chmod(unreadable / "sandbox.json", 0o600)
