"""Path safety helpers shared by both services."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath


def resolve_under_root(root: str | Path, user_path: str) -> Path:
    """Resolve ``user_path`` inside ``root``, rejecting traversal.

    Absolute user paths are treated as relative to the root. ``..`` segments,
    symlink escapes and empty paths raise :class:`PathTraversalError`.
    """
    root_path = Path(root).resolve()
    if user_path is None:
        raise PathTraversalError("path is required")
    if not isinstance(user_path, str):
        raise PathTraversalError("path must be a string")
    if "\x00" in user_path:
        raise PathTraversalError("path contains a null byte")

    if user_path == "" or user_path == "/":
        return root_path
    cleaned = user_path.lstrip("/")
    parts = PurePosixPath(cleaned).parts
    if ".." in parts or (parts and parts[0] == ".."):
        raise PathTraversalError("path escapes the sandbox root")

    candidate = root_path.joinpath(*parts) if parts else root_path
    # Reject symlink escapes by checking the resolved parent chain.
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as e:  # pragma: no cover - defensive
        raise PathTraversalError(f"cannot resolve path: {e}") from e
    if resolved != root_path and root_path not in resolved.parents:
        raise PathTraversalError("path escapes the sandbox root")
    return resolved


def is_within(parent: Path, child: Path) -> bool:
    parent = parent.resolve()
    child = child.resolve()
    return child == parent or parent in child.parents


class PathTraversalError(ValueError):
    """Raised when a user-supplied path attempts to escape a sandbox root."""


_SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_sandbox_id(sandbox_id: str) -> bool:
    """Reject malicious sandbox IDs before they reach path or process lookups."""
    return bool(sandbox_id) and bool(_SANDBOX_ID_RE.match(sandbox_id))


def is_sandbox_workspace_dir(entry: Path) -> bool:
    """Whether ``entry`` is a top-level sandbox workspace directory.

    The single filter every workspace scan shares (quota orphan
    reconciliation and the worker's orphan-tree GC): a real directory — never
    a symlink — whose name is a valid sandbox id.

    The workspace root's underscore namespace is infrastructure, not
    sandboxes: ``_snapshots`` / ``_migrate`` / ``_cow`` / ``_volumes`` /
    ``_templates`` / ``_secrets`` are spelled like legal sandbox ids (``_``
    is an id character), so ``validate_sandbox_id`` alone does not separate
    them from ``sbx_*`` trees and they must never be read, quota-scanned or
    deleted as one.
    """
    return (
        entry.is_dir()
        and not entry.is_symlink()
        and not entry.name.startswith("_")
        and validate_sandbox_id(entry.name)
    )


def safe_join(root: str | Path, *parts: str) -> Path:
    """Join parts under root and return the resolved path."""
    path = Path(root).resolve()
    for part in parts:
        path = path.joinpath(part)
    return path.resolve(strict=False)
