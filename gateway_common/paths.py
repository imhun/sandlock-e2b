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

#: A node id is a StatefulSet pod name (k8s), a compose service / container
#: name, or the in-process ``local`` node: leading alphanumeric, then dots,
#: dashes and underscores. Deliberately a *shape* check, like
#: :data:`_SANDBOX_ID_RE` -- what it has to stop is an id that changes the
#: meaning of a path segment (``/``, ``..``), a DNS name, or a log line.
#: ``\Z`` (not ``$``): this id is interpolated into an API path and a DNS
#: lookup, where a trailing newline is a different name, not a formality.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_node_id(node_id: str) -> bool:
    """Reject a node id that could not be a pod / service name.

    Used before a node id is interpolated into a k8s API path
    (``.../pods/<node_id>``) or handed to ``getaddrinfo``. Both are places
    where an id carrying a slash, a scheme or a control character would change
    the request's meaning; an id that fails this is "no address" (fail closed),
    never an exception out of the resolver.
    """
    return bool(node_id) and bool(_NODE_ID_RE.match(node_id))


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

#: Environment variable naming the **node-local** base the platform's
#: short-lived, same-node files live under -- the create's ``.creating``
#: marker, the ``statfs(2)`` accounting seed, ``.route-b``'s slot documents
#: and the uid pool's lock and reservation markers (N57 / Task 4).
#:
#: Why it is a *third* base rather than a second flavour of
#: ``E2B_STATE_BASE``: the record (``_runtime/<id>/sandbox.json``) and the
#: checkpoint store are read by **other nodes** -- every worker's uid ledger
#: enumerates the records -- while those four chips are read by this node's
#: own worker and slot processes only. On this deployment the shared base is
#: NFS, where one metadata round trip measures ~13 ms, and the create's
#: ``prepare`` phase paid it for each chip. Unset means "those files live
#: under the state base", i.e. exactly the pre-Task-4 layout: every deployment
#: that does not name this base (compose, tests, ``local://``) is byte-for-byte
#: unchanged, the same rule every other base in this module follows.
NODE_STATE_BASE_ENV = "E2B_NODE_STATE_BASE"

#: The snapshot store's directory name, under the **platform namespace root**
#: (:func:`platform_namespace_root`) -- one directory per snapshot id, holding
#: the control plane's ``snapshot.json`` next to the agent-written payload
#: (``fs/`` today, ``fs.tar`` after the tar task).
#:
#: It is spelled here because it is the one name two components write into from
#: opposite ends (record side: ``control_plane``; payload side:
#: ``envd_service.agent``), and before this constant they derived it from
#: different bases -- which is how the store ended up as two namespaces holding
#: the same ids.
SNAPSHOT_STORE_DIR_NAME = "_snapshots"

#: The snapshot payload's two spellings inside ``_snapshots/<id>/``. The
#: **writer** emits the tar (Task 2: one sequential file instead of one NAS
#: round trip per entry); the **reader** still accepts the exploded directory,
#: because every snapshot taken before the tar is one and a reader that only
#: knew tars would break all of them. Both are spelled here because three
#: modules (``envd_service.agent``, ``c3_agent.materialize``,
#: ``control_plane.file_ops``) name them and a drift between two of them is
#: a create-from-snapshot that answers "not a directory".
SNAPSHOT_PAYLOAD_TAR_NAME = "fs.tar"
SNAPSHOT_PAYLOAD_DIR_NAME = "fs"

#: The migration staging directory, also under the platform namespace root. Its
#: reader is the **target** node's agent (``_import_sandbox_archive``), which is
#: exactly why it cannot live under the tree root once the trees are node-local:
#: the target node cannot see another node's tree root.
MIGRATE_STAGING_DIR_NAME = "_migrate"

#: The control plane's own pass-through copy inside ``_migrate``. It is a
#: **subdirectory** on purpose: the node-side landing is
#: ``_migrate/<id>.tar.gz`` -- the source agent writes it and streams it back,
#: the target agent writes the body it receives to the same name and unpacks it
#: -- so a control plane that staged under the bare name would truncate the very
#: file the source agent is still streaming (the copy became a stream-to-disk
#: write in Task 3; before that it happened to be sequential and invisible).
MIGRATE_TRANSFER_DIR_NAME = "control-plane"

#: The sandbox's command output log (JSONL), written by the worker.
COMMAND_LOG_NAME = "command-logs.jsonl"

#: The checkpoint store, **beside** the per-sandbox runtime dirs rather than
#: inside them (see :func:`sandbox_checkpoint_dir` for why the split exists).
#: The leading dot keeps it out of the sandbox-id namespace, exactly like
#: :data:`UNTRUSTED_TREE_DIR`.
CHECKPOINT_ROOT_NAME = ".checkpoints"

#: How many bytes of a sandbox id may become a route-B **instance name**
#: (:func:`route_b_instance_name`). Longer ids are replaced by their hash: a
#: filename is bounded by ``NAME_MAX``, and the name is also the slot's
#: unix-socket path component in the registered transport.
ROUTE_B_INSTANCE_NAME_MAX_BYTES = 64


def route_b_instance_name(sandbox_id: str) -> str:
    """The route-B **instance name** for a sandbox -- one rule, two consumers.

    It is the slot's identity in the pool (``W1SlotPool.acquire_sync``'s
    ``name``) *and* the leaf of the directory that holds the slot's
    ``policy.json`` / ``program.json`` (``<route-b root>/<uid>/<name>/``). Two
    derivations of that one name existed until C3 Task 4's second review: the
    worker's executor computed it here and the control plane guessed
    ``rb-<sandbox_id>``, so the ``scope-slot-document`` op pointed at a
    directory that does not exist -- on the document that carries the
    egress-proxy credentials. Ruling D20: the rule lives here, in the module the
    control plane and envd both already share (``gateway_common``), and both
    sides call it.

    The >``ROUTE_B_INSTANCE_NAME_MAX_BYTES`` case is part of the rule, not an
    implementation detail of the worker: an id long enough to matter is replaced
    by ``sbx_<sha256(id)[:16]>``, and a control plane that did not know that
    would derive the wrong directory for exactly those sandboxes.
    """
    if len(sandbox_id.encode()) <= ROUTE_B_INSTANCE_NAME_MAX_BYTES:
        return sandbox_id
    import hashlib

    return "sbx_" + hashlib.sha256(sandbox_id.encode()).hexdigest()[:16]


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


def resolve_node_state_base(
    workspace_base: str | Path,
    state_base: str | Path | None = None,
    node_state_base: str | Path | None = None,
) -> Path:
    """The base the platform's **node-local** files live under.

    ``node_state_base`` when given (:data:`NODE_STATE_BASE_ENV`), the state
    base otherwise -- and that in turn defaults to the workspace base. Unset is
    therefore exactly the layout every deployment had until Task 4, which is
    what makes naming the base the whole of the migration: no code path can end
    up with a *different* answer about where a marker lives when the base is
    not named.

    Deliberately not folded into :func:`resolve_state_base`: the two answer
    questions with different readers. The record and the checkpoint store under
    the state base are read across nodes (the fleet-wide uid ledger), while the
    marker, the accounting seed and the pool's own lock and markers are read by
    this node only -- see :data:`NODE_STATE_BASE_ENV`.
    """
    if node_state_base:
        return Path(node_state_base)
    return resolve_state_base(workspace_base, state_base)


def platform_namespace_root(
    workspace_base: str | Path,
    *,
    shared_root: str | Path | None = None,
) -> Path:
    """The base the platform's own *namespaces* hang off (``_snapshots``, ``_migrate``).

    The shared root when the deployment names one, the workspace base otherwise
    -- which is the pre-N57 shape, unchanged.

    Why this is not the same question as "where do the sandbox trees live":

    * the snapshot **record** is written by the control plane at the export root
      (``control_plane/app.py``: ``platform_root``), not at the tree root;
    * the snapshot **payload** is written by the agent, and the node that
      restores it may be any node;
    * the migration staging directory is read by the **target** node's agent.

    All three readers are "not the tree's own node". Today the workspace base
    happens to be a directory *under* the shared root, so deriving them from the
    tree root worked by accident -- and that accident is what makes "move the
    trees to node-local disk" quietly move these three with them. Naming the
    root explicitly is what lets the two move independently.
    """
    return Path(shared_root) if shared_root else Path(workspace_base)


def snapshot_payload_dir(
    workspace_base: str | Path,
    snapshot_id: str,
    *,
    shared_root: str | Path | None = None,
) -> Path:
    """``<platform namespace root>/_snapshots/<id>`` -- one snapshot, both halves."""
    return (
        platform_namespace_root(workspace_base, shared_root=shared_root)
        / SNAPSHOT_STORE_DIR_NAME
        / snapshot_id
    )


def snapshot_payload(snapshot_dir: str | Path) -> tuple[str, Path] | None:
    """``(shape, path)`` for one snapshot's payload, or ``None`` when it has none.

    ``shape`` is ``"tar"`` for what the writer emits (``fs.tar``) and ``"dir"``
    for the pre-tar exploded ``fs/`` directory. When both are on disk the tar
    wins: it is the shape the writer produces and the one the create path's
    ``copy_from`` names, so "both" means a leftover, not a second payload.

    Here rather than in each reader because the two halves of this store are
    read by code in three different images (the control plane, the per-node
    agents, and the read-only acceptance probes); a fourth spelling of ``fs``
    is how a probe ends up calling a healthy ``fs.tar`` snapshot "record only"
    (review round 1, 2026-10-02).
    """
    directory = Path(snapshot_dir)
    tar = directory / SNAPSHOT_PAYLOAD_TAR_NAME
    if tar.is_file():
        return "tar", tar
    legacy = directory / SNAPSHOT_PAYLOAD_DIR_NAME
    if legacy.is_dir():
        return "dir", legacy
    return None


def migrate_staging_dir(
    workspace_base: str | Path,
    *,
    shared_root: str | Path | None = None,
) -> Path:
    """``<platform namespace root>/_migrate`` -- the cross-node transfer's landing zone."""
    return (
        platform_namespace_root(workspace_base, shared_root=shared_root)
        / MIGRATE_STAGING_DIR_NAME
    )


def migrate_transfer_path(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    shared_root: str | Path | None = None,
) -> Path:
    """``<platform namespace root>/_migrate/control-plane/<id>.tar.gz``.

    The control plane's copy of one migration's archive: it is what the export
    streams into and what the import streams out of, and it is deliberately not
    the node-side landing path (see :data:`MIGRATE_TRANSFER_DIR_NAME`).
    """
    return (
        migrate_staging_dir(workspace_base, shared_root=shared_root)
        / MIGRATE_TRANSFER_DIR_NAME
        / f"{sandbox_id}.tar.gz"
    )


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


def sandbox_node_runtime_dir(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    state_base: str | Path | None = None,
    node_state_base: str | Path | None = None,
) -> Path:
    """``<node state base>/_runtime/<id>`` -- the node-local half of a sandbox's
    platform directory.

    Same ``_runtime/<id>`` shape as :func:`sandbox_runtime_dir`, on the other
    base: the record directory (``sandbox.json``, the command log) is shared,
    and the two chips the create writes there for *this node's own readers* --
    ``.creating`` and ``disk-stats`` -- are not. Keeping the same interior shape
    is what lets every reader keep addressing ``_runtime/<id>/<name>`` instead
    of learning a second layout; the base is the only thing that moved.
    """
    return (
        resolve_node_state_base(workspace_base, state_base, node_state_base)
        / RUNTIME_DIR_NAME
        / sandbox_id
    )


#: The create-in-flight marker's name, inside the **node state base**'s
#: ``_runtime/<id>`` (see :func:`sandbox_creating_marker`). A dot-file so no
#: reader that enumerates a runtime directory mistakes it for a record, and so
#: a ``sandbox.json`` reader can never see it as one.
CREATING_MARKER_NAME = ".creating"


def sandbox_creating_marker(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    state_base: str | Path | None = None,
    node_state_base: str | Path | None = None,
) -> Path:
    """``<node state base>/_runtime/<id>/.creating`` -- "a create is in flight".

    Two invariants are readable off the disk because of this file, and they are
    what makes moving the record write off the create's response path safe:

    * the **marker** exists ⇒ a create is running (a teardown must wait);
    * the **record** exists ⇒ that create finished (``register`` is the last
      thing it does).

    A crashed create leaves the marker and no record, which is exactly the
    input the existing orphan path already reclaims.
    """
    return (
        sandbox_node_runtime_dir(
            workspace_base,
            sandbox_id,
            state_base=state_base,
            node_state_base=node_state_base,
        )
        / CREATING_MARKER_NAME
    )


_DISK_STATS_NAME = "disk-stats"


def sandbox_disk_stats_path(
    workspace_base: str | Path,
    sandbox_id: str,
    *,
    state_base: str | Path | None = None,
    node_state_base: str | Path | None = None,
) -> Path:
    """The host's disk accounting for one sandbox's ``statfs(2)``.

    ``<node state base>/_runtime/<id>/disk-stats`` holding ``<total_bytes>
    <used_bytes>``. It lives beside the sandbox's record rather than in its
    tree: the sandbox must not be able to write the numbers it is shown, and
    the supervisor (which reads it on each ``statfs``) cannot reach the
    sandbox's own mount namespace.

    Node-local, like the marker: the writer is this node's worker and the
    reader is this node's slot process, so a shared-volume write here is one
    NAS round trip bought for no cross-node reader (Task 4). The *record* it
    sits next to stays shared -- that is the half the fleet reads.

    The **reader is not the writer**: in the route-B shape the supervisor is the
    slot process at the sandbox's own host uid, while the file is written by the
    worker. The directory therefore has to stay traversable by name (``0711``)
    and the file readable (``0644``) -- see
    :meth:`envd_service.runtime.registry.RuntimeRegistry._ensure_runtime_dir`
    and ``envd_service.agent._write_disk_stats``, which pin both against the
    ambient umask. A ``0700`` directory here looks stricter and is in fact a
    silent outage: the supervisor's read fails and every ``statfs`` answers with
    the node's volume again (measured 2026-10-01).
    """
    return (
        sandbox_node_runtime_dir(
            workspace_base,
            sandbox_id,
            state_base=state_base,
            node_state_base=node_state_base,
        )
        / _DISK_STATS_NAME
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

    What this promises, and what it does not: the guarantee is *visibility*
    atomicity -- a reader sees the whole previous document or the whole new
    one, which is the property every reader above depends on. It is **not** a
    durability promise for the rename: the staged bytes are ``fsync``-ed (so
    they are on the shared volume before the name is published), but the
    containing directory is not, so a machine dying in the instant after
    ``os.replace`` can come back with the previous name. Falling back to the
    previous whole record is the safe direction, and the ``.<name>.<hex>.tmp``
    such a crash leaves behind is garbage that nothing has to collect for
    correctness: no scan matches it (records are read by their exact name, and
    ``_meta/*.json`` requires the ``.json`` suffix).
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
