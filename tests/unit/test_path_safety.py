"""Path traversal and sandbox ID safety."""

from __future__ import annotations

import pytest

from gateway_common.paths import (
    PathTraversalError,
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

