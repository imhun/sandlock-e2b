"""Path safety -- and shared-record publishing -- for both services."""

from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any


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
#: * ``snap_`` — the snapshot store's own trees. ``SnapshotRegistry`` is built
#:   on the *shared export root* (``control_plane/app.py``: ``platform_root`` =
#:   ``settings.shared_workspace_root``), **not** on the workspace base, so a
#:   snapshot is ``<export>/_snapshots/snap_<hex>``
#:   (``control_plane/registry/snapshots.py``): it holds ``snapshot.json`` and
#:   the copied filesystem under ``fs/``, and the sandbox record that copy
#:   carries sits at ``snap_X/fs/sandbox.json``, never at the top level. (The
#:   pre-OBS-9 root-level ``<export>/snap_<hex>`` is still read; the worker's
#:   payload endpoints hard-code ``<workspace_base>/_snapshots`` --
#:   ``envd_service/agent.py``.)
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
#: It sits under the **state base** (:func:`resolve_state_base`), which today
#: defaults to the workspace base and can be moved out from under it entirely
#: with ``E2B_STATE_BASE`` (N27): the namespace name is unchanged either way,
#: and both bases carry it while a migration is in flight, so the name stays
#: reserved on the workspace side (see
#: :data:`RESERVED_PLATFORM_NAMESPACES`).
#:
#: The sandbox owns its tree directory (``0770 <sandbox uid>:<worker gid>``,
#: E3.2), and owning a directory means being able to unlink from it: measured
#: on the live cluster, a sandbox can ``rm sandbox.json`` and write its own
#: back even though the file itself is read-only to it. Anything the platform
#: must trust therefore cannot live there -- that is why the fleet-level uid
#: allocation moved to Redis (OBS-9) and why these files move here, where the
#: sandbox has no access at all.
RUNTIME_DIR_NAME = "_runtime"

#: The per-sandbox root of the pure shape (N16): an empty skeleton for a
#: sandbox whose shape has no base image. The sandbox's own mount namespace
#: binds the host's system directories, the workspace and the volumes into
#: ``<base>/_pure_rootfs/<id>`` (see
#: ``envd_service.executors.sandlock._synthetic_rootfs_mounts``), so the pure
#: shape can use the fork's ``real_root`` as well, with
#: ``E2B_PURE_ROOTFS_DIR`` overriding the base. Never created when the switch
#: is off: the pure shape then keeps N15's identity translation.
#:
#: It has to be traversable by the sandbox's own host uid -- the binds and the
#: ``chdir`` into the root run inside the sandbox's *user* namespace, as the
#: sandbox itself -- which rules out both the sandbox's own tree (it owns that
#: one and could unlink its own root) and ``_runtime/<id>`` (``0700`` and
#: worker-owned: a ``0700`` parent cannot be traversed by the sandbox).
#:
#: That is why the executor's ``mkdir``/``chmod`` of this directory and of
#: ``<this>/<id>`` is **deliberate**: both layers are created at ``0755``, and
#: healed back to ``0755`` when an older run or a hostile umask left them
#: ``0700`` (the heal stops at the anchor, so a misconfigured
#: ``E2B_PURE_ROOTFS=/`` never ``chmod``s ``/``). An operator seeing this
#: directory's mode change in a worker log is watching the synthesis work, not
#: a bug.
#:
#: Enabled only *after* the teardown that removes ``<base>/_pure_rootfs/<id>``
#: has landed -- the two are separate steps of
#: ``docs/superpowers/plans/2026-09-26-pure-shape-synthetic-rootfs.md``, and
#: until the cleanup side runs, every destroyed sandbox would leave its root
#: behind. Reserved like the rest of the platform's namespaces, below.
PURE_ROOTFS_DIR_NAME = "_pure_rootfs"

#: The directory name of the **state base** inside the shared export: the
#: platform's own tree (``_runtime``, ``.route-b``, the uid pool's lock and
#: reservations) living *beside* the sunk root of the sandbox trees
#: (``<export>/workspaces/<id>`` + ``<export>/state``), so a sandbox walking up
#: from its own tree reaches the workspace root and nothing else.
#:
#: Neither ``paths`` nor the worker creates it: the deploy-side init container
#: owns its mode and ownership, exactly like the other platform namespaces. Its
#: name is here because it is a platform namespace like the rest -- see
#: :data:`RESERVED_PLATFORM_NAMESPACES` for why a bare ``state`` directory must
#: not be read as a sandbox tree (it spells a legal id, the same M1 shape).
STATE_DIR_NAME = "state"

#: Environment variable naming the base the platform's own files live under
#: (record, command log, checkpoint images). Unset = the workspace base, which
#: is exactly today's layout -- committing this switch is what makes "the
#: sandbox cannot reach platform state" independent of the sandbox shape
#: (docs/pure-shape-decision.md §4, N27).
STATE_BASE_ENV = "E2B_STATE_BASE"

#: The sandbox's command output log (JSONL), written by the worker.
COMMAND_LOG_NAME = "command-logs.jsonl"

#: The checkpoint store, **beside** the per-sandbox runtime dirs rather than
#: inside them (see :func:`sandbox_checkpoint_dir` for why the split exists).
#: The leading dot keeps it out of the sandbox-id namespace, exactly like
#: :data:`UNTRUSTED_TREE_DIR`.
CHECKPOINT_ROOT_NAME = ".checkpoints"


def resolve_state_base(
    workspace_base: str | Path, state_base: str | Path | None = None
) -> Path:
    """The base the platform's own files live under.

    ``state_base`` when given, the workspace base otherwise -- which is exactly
    today's layout, so every helper below is unchanged until a deployment sets
    ``E2B_STATE_BASE`` (:data:`STATE_BASE_ENV`). ``None``, or an empty value --
    which is what the environment reads an unset variable as -- means the
    workspace base.
    """
    return Path(state_base) if state_base else Path(workspace_base)


def sandbox_runtime_dir(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    state_base: str | Path | None = None,
) -> Path:
    """``<state base>/_runtime/<id>`` -- the platform's directory for one sandbox.

    Defaults to ``<workspace_base>/_runtime/<id>``, i.e. today's path; see
    :func:`resolve_state_base`.
    """
    return (
        resolve_state_base(workspace_base, state_base) / RUNTIME_DIR_NAME / sandbox_id
    )


def sandbox_record_path(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    legacy: bool = False,
    state_base: str | Path | None = None,
) -> Path:
    """Where a sandbox's runtime record lives.

    ``legacy=True`` returns the pre-split location inside the sandbox's own
    tree, which readers still fall back to and writers migrate away from; see
    ``RuntimeRegistry.adopt_legacy_records``. That location is a statement
    about the *workspace* base and stays there whatever the state base is.
    """
    if legacy:
        return Path(workspace_base) / sandbox_id / _SANDBOX_RECORD_NAME
    return (
        sandbox_runtime_dir(workspace_base, sandbox_id, state_base=state_base)
        / _SANDBOX_RECORD_NAME
    )


def sandbox_command_log_path(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    legacy: bool = False,
    state_base: str | Path | None = None,
) -> Path:
    """Where a sandbox's command log lives (see :func:`sandbox_record_path`)."""
    if legacy:
        return Path(workspace_base) / sandbox_id / COMMAND_LOG_NAME
    return (
        sandbox_runtime_dir(workspace_base, sandbox_id, state_base=state_base)
        / COMMAND_LOG_NAME
    )


def sandbox_checkpoint_dir(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    state_base: str | Path | None = None,
) -> Path:
    """``<state base>/_runtime/.checkpoints/<id>`` -- a sandbox's checkpoint images.

    Platform state, held under ``_runtime`` (never inside the tree the sandbox
    owns, and on the shared volume so another node can resume it), but as a
    **sibling** of the runtime dir rather than a child of it. That split is what
    makes the capture possible at all: the image is written *by the sandbox's own
    slot* -- route B runs it as the sandbox's pooled uid, and that is the only
    process that owns the address space being captured -- so the directory has to
    belong to that uid, while ``_runtime/<id>`` itself holds the runtime record
    and the command log, which are the worker's own files and stay ``0700``
    worker-owned (a ``0700`` parent cannot be traversed by the slot, so "images
    inside the record's directory" would only work by opening the record's
    directory up).

    The store's own gate is :data:`CHECKPOINT_ROOT_NAME` at ``0711``: traverse,
    no listing. Each ``<id>`` inside it is ``0700`` for that sandbox's uid, so
    one sandbox can neither enumerate the store nor read another's image; the
    worker still measures and removes them (as root, or through ``e2b-maint``).

    It is also, deliberately, *outside* the tree the per-sandbox quota measures
    (``<base>/<id>``). That is why it has an account of its own -- see
    :mod:`envd_service.runtime.platform_disk`.

    Follows the state base like the record and the command log; unset, that is
    the workspace base and this is ``<workspace_base>/_runtime/.checkpoints/<id>``
    (see :func:`resolve_state_base`).
    """
    return (
        resolve_state_base(workspace_base, state_base)
        / RUNTIME_DIR_NAME
        / CHECKPOINT_ROOT_NAME
        / sandbox_id
    )

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
        #: The pure shape's per-sandbox roots, a platform layer rather than a
        #: tenant tree: see :data:`PURE_ROOTFS_DIR_NAME`. Listed here so the
        #: walks that consult this list -- the park offer and the fail-safe
        #: quota scan's second stage -- skip it, and so the create path refuses
        #: the id.
        PURE_ROOTFS_DIR_NAME,
        #: The state base's own directory name, a sibling of the sunk tree root
        #: (see :data:`STATE_DIR_NAME`). Under the committed shape it never
        #: appears *under* this base, so this entry costs nothing there; it is
        #: what keeps the transitional config (old base still in use, state
        #: base already created) from reading a whole platform tree as one
        #: sandbox tree -- ``state`` is a legal sandbox id.
        STATE_DIR_NAME,
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


def write_text_atomically(path: str | Path, text: str) -> None:
    """Publish ``text`` at ``path`` so a reader never sees half a file.

    Every record under the shared volume is read by a *different process*
    while it is written -- the other control-plane replica
    (``control_plane/registry/``: builds, templates, snapshots, volumes,
    secrets) and the other worker (``envd_service/runtime/registry.py``).
    ``Path.write_text`` truncates the target and then writes it, so a reader
    arriving in that window reads an empty or half-written document: for a
    template build that was ``404 Template build … not found`` on a build that
    was running fine (measured on k0s 2026-09-26, and the e2b SDK does not
    retry), and for a sandbox record it is the uid pool handing one host uid
    out twice -- E3.2's per-sandbox isolation, gone silently, because
    ``uid_pool._recorded_uid`` reads an unparsable record as "no record".

    Writing a sibling file and renaming it over the target makes the update
    appear in one step: a reader sees the previous complete document or the
    new one, never a mixture. The staged file is a sibling on purpose -- the
    rename has to stay inside one filesystem to be atomic -- and it is created
    ``0o666 & ~umask`` (what a plain ``write_text`` produces) rather than
    ``mkstemp``'s ``0o600``, so who may read the published file does not
    change. A failure leaves the target as it was and no partial file behind.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f".{path.name}.{os.urandom(8).hex()}.tmp"
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            # Commit the bytes before publishing the name: on the shared NFS
            # volume another node must never open the new name and find the
            # write still in flight.
            os.fsync(handle.fileno())
        os.replace(staged, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(staged)
        raise


def write_json_atomically(path: str | Path, payload: Any) -> None:
    """``write_text_atomically`` for the JSON records they all are."""
    write_text_atomically(path, json.dumps(payload, separators=(",", ":")))
