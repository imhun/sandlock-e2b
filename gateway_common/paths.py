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
    """Reject malicious sandbox IDs before they reach path or process lookups.

    Shape only, on purpose: interior scans (the orphan GC, the quota scan) run
    this over *existing* top-level names, and a reserved-looking name that
    carries a record has to stay visible to them (the M1 leak was exactly a
    client-chosen ``snap_*`` tree dropped by a name filter). Rejecting the
    platform's own namespace names belongs on the create path, where an id is
    first *chosen* -- :func:`is_reserved_platform_namespace` is the check.
    """
    return bool(sandbox_id) and bool(_SANDBOX_ID_RE.match(sandbox_id))


#: Top-level namespaces under the workspace base that hold infrastructure, not
#: sandboxes. Their names are spelled like legal sandbox ids (``_`` and
#: ``snap_`` are both id characters), so :func:`validate_sandbox_id` alone does
#: not separate them from the ``sbx_*`` trees:
#:
#: * ``_`` — the reserved root namespace: ``_snapshots`` / ``_migrate`` /
#:   ``_cow`` / ``_volumes`` / ``_templates`` / ``_secrets`` / ``_builds`` /
#:   ``_images``.
#: * ``snap_`` — the snapshot store's own trees. ``SnapshotRegistry``'s base
#:   *is* the workspace base (``control_plane/app.py``), so a snapshot is a
#:   top-level ``snap_<hex>`` directory sitting right next to the sandbox trees
#:   (``control_plane/registry/snapshots.py``): it holds ``snapshot.json`` and
#:   the copied filesystem under ``fs/``, and the sandbox record that copy
#:   carries sits at ``snap_X/fs/sandbox.json``, never at the top level.
#:
#: Neither prefix is reserved on the create side — ``X-Sandbox-Id`` is checked
#: with :func:`validate_sandbox_id` alone (``control_plane/api/sandboxes.py``)
#: and the snapshot id is generated server-side — so the prefix *cannot* decide
#: anything on its own. ``snap_client1`` is a legal sandbox id whose tree lands
#: at ``<base>/snap_client1``; excluding prefixed names unconditionally stranded
#: such a tree outside the GC candidate set *and* outside the quota scan's
#: projid map while ``_recorded_projids`` kept pinning its row through the
#: surviving ``sandbox.json``: a silent, permanent leak (review M1).
#:
#: The separator is the content shape, not the name: infrastructure namespaces
#: carry no top-level ``sandbox.json``, every sandbox tree has one. A prefixed
#: directory that carries its own record is therefore the sandbox tree it looks
#: like, and only a prefixed directory without one stays excluded.
#:
#: This is an *exclusion* list rather than an ``sbx_`` allow-list on purpose:
#: the ``sbx_`` prefix is a documented client contract (``docs/SCALING.md``),
#: not an enforced server invariant, so an allow-list would drop every live tree
#: with a client-chosen non-``sbx_`` id out of the same two scans.
#:
#: Residual shape (accepted): a whole-tree copy of a sandbox landing at
#: ``<base>/snap_X`` *and* carrying a record rewritten to name ``snap_X`` itself
#: is indistinguishable from a real tree with that id and is treated as one. The
#: shape a plain copy produces — the record still naming the original sandbox —
#: is refused by the teardown guards (``agent._gc_teardown_plan``) and reported
#: as ``untrusted_records``.
_INFRASTRUCTURE_PREFIXES = ("_", "snap_")

#: The record every sandbox workspace tree keeps at its root
#: (``envd_service/runtime/registry.py``). Its presence at the top level is the
#: shape signal that separates a sandbox tree from an infrastructure namespace.
_SANDBOX_RECORD_NAME = "sandbox.json"

#: Top-level namespace holding each sandbox's **platform** files (its runtime
#: record and its command log) -- deliberately *next to*, never inside, the
#: sandbox's own tree.
#:
#: The sandbox owns its tree directory (``0770 <sandbox uid>:<worker gid>``,
#: E3.2), and owning a directory means being able to unlink from it: measured
#: on the live cluster, a sandbox can ``rm sandbox.json`` and write its own
#: back even though the file itself is read-only to it. Anything the platform
#: must trust therefore cannot live there -- that is why the fleet-level uid
#: allocation moved to Redis (OBS-9) and why these files move here, where the
#: sandbox has no access at all.
RUNTIME_DIR_NAME = "_runtime"

#: The sandbox's command output log (JSONL), written by the worker.
COMMAND_LOG_NAME = "command-logs.jsonl"

#: Where a sandbox's checkpoint images live, inside its runtime dir.
CHECKPOINT_DIR_NAME = "checkpoint"


def sandbox_runtime_dir(workspace_base: str | Path, sandbox_id: str) -> Path:
    """``<base>/_runtime/<id>`` -- the platform's directory for one sandbox."""
    return Path(workspace_base) / RUNTIME_DIR_NAME / sandbox_id


def sandbox_record_path(
    workspace_base: str | Path, sandbox_id: str, *, legacy: bool = False
) -> Path:
    """Where a sandbox's runtime record lives.

    ``legacy=True`` returns the pre-split location inside the sandbox's own
    tree, which readers still fall back to and writers migrate away from; see
    ``RuntimeRegistry.adopt_legacy_records``.
    """
    if legacy:
        return Path(workspace_base) / sandbox_id / _SANDBOX_RECORD_NAME
    return sandbox_runtime_dir(workspace_base, sandbox_id) / _SANDBOX_RECORD_NAME


def sandbox_command_log_path(
    workspace_base: str | Path, sandbox_id: str, *, legacy: bool = False
) -> Path:
    """Where a sandbox's command log lives (see :func:`sandbox_record_path`)."""
    if legacy:
        return Path(workspace_base) / sandbox_id / COMMAND_LOG_NAME
    return sandbox_runtime_dir(workspace_base, sandbox_id) / COMMAND_LOG_NAME


def sandbox_checkpoint_dir(workspace_base: str | Path, sandbox_id: str) -> Path:
    """``<base>/_runtime/<id>/checkpoint`` -- a sandbox's checkpoint images.

    Inside the runtime dir on purpose, and for the same reason the record and the
    command log are: this is platform state that holds the sandbox's **whole
    process image**, so the sandbox itself must never be able to read it (see
    :func:`sandbox_runtime_dir`), while the deployment still needs it across nodes
    (the base is a shared volume).

    It is also, deliberately, *outside* the tree the per-sandbox quota measures
    (``<base>/<id>``). That is why it has an account of its own -- see
    :mod:`envd_service.runtime.platform_disk`.
    """
    return sandbox_runtime_dir(workspace_base, sandbox_id) / CHECKPOINT_DIR_NAME

#: Where a worker parks a tree it refuses to act on (review W7 / W7-3): such a
#: tree is never *deleted* (its record may be describing a bind-mounted other
#: tenant's tree) and is never acted on from the record's claims either, but
#: leaving it among the sandbox namespaces leaves its quota row pinned. The
#: name carries a dot on purpose: ``_SANDBOX_ID_RE`` rejects it, so the
#: quarantine can never be read as a sandbox tree nor be created inside a live
#: sandbox's workspace.
UNTRUSTED_TREE_DIR = "_untrusted.trees"

#: Top-level names the platform itself owns under the workspace base, listed
#: explicitly. Unlike :data:`_INFRASTRUCTURE_PREFIXES` these are the
#: platform's *own* storage (the R1/R2 review's "infrastructure directories"),
#: not a name-based statement about sandbox ids: a directory spelled like one
#: of them is still a sandbox tree when it carries its own top-level
#: ``sandbox.json`` (the shape rule above decides that, unchanged).
#:
#: They are never the target of the disk-truth rules (the worker's park
#: surface and the fail-safe quota scan's second stage), for two reasons:
#:
#: * nothing ever assigns a project id to them -- ``provision_project`` is
#:   called on sandbox trees (``agent.create_sandbox_runtime``) and on volume
#:   slices (``volumes.provision_sandbox_volume_mount``), never on the base, so
#:   the only way one of these could report a project id is the base itself
#:   carrying ``PROJINHERIT``;
#: * they are not parkable: moving ``_volumes`` or ``_snapshots`` into the
#:   quarantine would take a whole namespace (and the live tenants' data in
#:   it) off the workspace.
RESERVED_PLATFORM_NAMESPACES = frozenset(
    {
        "_builds",
        "_cow",
        "_images",
        "_migrate",
        #: Per-sandbox *platform* files (runtime record + command log), live
        #: beside the sandbox's tree: see :data:`RUNTIME_DIR_NAME`. Reserved
        #: like the rest -- it must never be listed as an untrusted tree, never
        #: reaped as an orphan and never parked.
        RUNTIME_DIR_NAME,
        "_secrets",
        "_snapshots",
        "_templates",
        "_volumes",
        #: The quarantine itself is never an asset; the trees parked inside it
        #: are covered one level down (the fail-safe scan's second stage).
        UNTRUSTED_TREE_DIR,
    }
)


def is_reserved_platform_namespace(name: str) -> bool:
    """Whether ``name`` is one of the platform's own top-level namespaces."""
    return name in RESERVED_PLATFORM_NAMESPACES


def is_sandbox_workspace_dir(entry: Path) -> bool:
    """Whether ``entry`` is a top-level sandbox workspace directory.

    The single filter every workspace scan shares (quota orphan
    reconciliation and the worker's orphan-tree GC): a real directory — never
    a symlink — whose name is a valid sandbox id and which either lies outside
    the infrastructure namespaces (:data:`_INFRASTRUCTURE_PREFIXES`) or carries
    its own top-level ``sandbox.json`` into the bargain.

    Existence of that record, not readability, is the shape signal on purpose:
    a tree whose record exists but cannot be read has to stay visible to the
    scans — the worker reports it as ``unmaterialised`` and never tears it down
    — instead of slipping back into the silent-orphan state this predicate
    exists to prevent.
    """
    if not entry.is_dir() or entry.is_symlink():
        return False
    if not validate_sandbox_id(entry.name):
        return False
    if not entry.name.startswith(_INFRASTRUCTURE_PREFIXES):
        return True
    # A prefixed name is a sandbox tree when the *content* says so: its record
    # exists. The record lives in ``_runtime/<name>/`` since the platform/
    # workspace split, so that is where to look; the in-tree copy is the
    # pre-split location and still counts so a rolling upgrade does not strand
    # an existing client-chosen ``snap_*``/``_*`` tree.
    if (entry / _SANDBOX_RECORD_NAME).is_file():
        return True
    return sandbox_record_path(entry.parent, entry.name).is_file()


def safe_join(root: str | Path, *parts: str) -> Path:
    """Join parts under root and return the resolved path."""
    path = Path(root).resolve()
    for part in parts:
        path = path.joinpath(part)
    return path.resolve(strict=False)
