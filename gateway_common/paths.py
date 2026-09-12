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


#: Top-level namespaces under the workspace base that hold infrastructure, not
#: sandboxes. Their names are spelled like legal sandbox ids (``_`` is an id
#: character), so :func:`validate_sandbox_id` alone does not separate them from
#: the ``sbx_*`` trees and they must never be read, quota-scanned or deleted as
#: one:
#:
#: * ``_`` — the reserved root namespace: ``_snapshots`` / ``_migrate`` /
#:   ``_cow`` / ``_volumes`` / ``_templates`` / ``_secrets`` / ``_builds`` /
#:   ``_images``.
#: * ``snap_`` — the snapshot store's own trees. ``SnapshotRegistry``'s base
#:   *is* the workspace base (``control_plane/app.py``), so a snapshot is a
#:   top-level ``snap_<hex>`` directory sitting right next to the sandbox trees
#:   (``control_plane/registry/snapshots.py``). On today's shape it holds only
#:   ``snapshot.json``, so the caller's "no readable record" fail-safe happens
#:   to spare it — but a snapshot carrying a top-level ``sandbox.json`` (the
#:   whole-tree copy shape) must never be folded into the GC's candidate set.
#:
#: This is an *exclusion* list rather than an ``sbx_`` allow-list on purpose.
#: A workspace tree is ``<base>/<sandbox_id>``, and a client may hand the
#: control plane its own ``sandbox_id`` through ``X-Sandbox-Id``, which is
#: validated with :func:`validate_sandbox_id` alone
#: (``control_plane/api/sandboxes.py``) — the ``sbx_`` prefix is a documented
#: client contract (``docs/SCALING.md``), not an enforced server invariant.
#: An allow-list would therefore drop a live tree with a client-chosen
#: non-``sbx_`` id out of the worker's GC candidate set and out of the quota
#: scan's projid map; excluding only the namespaces that are infrastructure by
#: construction keeps the change to names that are provably not sandboxes.
_INFRASTRUCTURE_PREFIXES = ("_", "snap_")


def is_sandbox_workspace_dir(entry: Path) -> bool:
    """Whether ``entry`` is a top-level sandbox workspace directory.

    The single filter every workspace scan shares (quota orphan
    reconciliation and the worker's orphan-tree GC): a real directory — never
    a symlink — whose name is a valid sandbox id and is not one of the
    infrastructure namespaces (:data:`_INFRASTRUCTURE_PREFIXES`).
    """
    return (
        entry.is_dir()
        and not entry.is_symlink()
        and not entry.name.startswith(_INFRASTRUCTURE_PREFIXES)
        and validate_sandbox_id(entry.name)
    )


def safe_join(root: str | Path, *parts: str) -> Path:
    """Join parts under root and return the resolved path."""
    path = Path(root).resolve()
    for part in parts:
        path = path.joinpath(part)
    return path.resolve(strict=False)
