"""The control plane's file-operation vocabulary (C3 Task 4).

The worker may not name a path or a uid (hard rules 1/3, C3 §14.4): its
requests carry ``{sandbox_id, op}`` -- "which sandbox, what to do" -- and the
**control plane derives the target from its own records and settings** before
instructing the agent. This module is that derivation, and nothing else: it
reads no disk, executes nothing, and holds no state, so the table below is
readable as the complete list of what the platform may ask a node to do.

Every op maps onto one ``e2b-maint`` verb (D18.3 -- the verbs are reused, not
re-invented) and onto one target:

| op | verb | target |
|---|---|---|
| ``chown-workspace`` | ``chown`` | ``<workspace base>/<id>`` |
| ``remove-workspace`` | ``rm`` | ``<workspace base>/<id>`` |
| ``walk-workspace`` | ``walk`` | ``<workspace base>/<id>`` |
| ``remove-runtime`` | ``rm`` | ``<state base>/_runtime/<id>`` |
| ``chown-checkpoint`` | ``chown`` | ``<state base>/_runtime/.checkpoints/<id>`` |
| ``remove-checkpoint`` | ``rm`` | ``<state base>/_runtime/.checkpoints/<id>`` (**the store**, not ``<store>/latest``: one image per sandbox, and the teardown's hook means the store -- see ``envd_service.runtime.checkpoint_store._remove_image``) |
| ``walk-checkpoint`` | ``walk`` | ``<state base>/_runtime/.checkpoints/<id>`` |
| ``chown-volume-slice`` | ``chown`` | ``<volume>/<id>`` |
| ``chown-volume-root`` | ``chown`` | ``<volume>`` |
| ``remove-volume-slice`` | ``rm`` | ``<volume>/<id>`` |
| ``chown-secret`` | ``chown`` | ``<image cache>/secrets/<id>/<name>.secret`` |
| ``scope-slot-document`` | ``chown`` | ``<own-identity root>/<uid>/<instance name>/<name>``, where the leaf comes from :func:`gateway_common.paths.own_identity_instance_name` -- the **same** function the worker's executor names the slot with (ruling D20) |
| ``remove-orphan-workspace`` | ``rm`` | ``<workspace base>/<id>`` (**self-heal only**, C3 Task 6): the tree the control plane's records claim nowhere. It is the one op with no record to derive a uid from -- that is its definition -- and the worker surface refuses it by name. |
| ``materialize-tree`` | ``materialize`` | ``<workspace base>/<id>`` (+ the snapshot copy source and the per-sandbox volume slices). The **create path's** one privileged step, and the only row with **no caller surface at all**: the control plane derives it for itself (`control_plane/api/sandboxes.py::_materialize_remote`) and sends it to the node's agent directly, so no worker request can ever name it. The row lives here because this table is the complete list of what the platform may ask a node to do, and because :func:`derive_materialize` uses it for the root-check refusal. |

The last row is the only op whose ``callers`` set is not ``{"worker"}``: it is
the agent-shape sweep's removal (``control_plane/self_heal.py``), listed here
so this table stays the complete, reviewable list of what the platform can ask
a node to do, while the worker's own vocabulary stays exactly the worker's.

Two rules are enforced *here* as well as in the agent, on purpose (C3 §14.4:
"两道，不互相替代" -- the agent still resolves and whitelists independently):

* every derived path has to land inside the control plane's own roots, or the
  op is refused by name;
* the two string parameters that end up in a path (a volume name, a secret or
  slot-document file name) are checked -- the volume against the volume
  registry, the names against a closed shape -- so a request cannot steer the
  derivation out of the namespace it belongs to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from gateway_common.paths import (
    SNAPSHOT_PAYLOAD_TAR_NAME,
    is_reserved_platform_namespace,
    own_identity_instance_name,
    sandbox_checkpoint_dir,
    sandbox_runtime_dir,
    snapshot_payload_dir,
    validate_sandbox_id,
)


class FileOpRefusal(Exception):
    """A named, fail-closed refusal with the status the worker should see."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FileOpSpec:
    """One named op: its verb, the parameters it accepts, and who may ask.

    ``callers`` exists because the table is the *complete* list of privileged
    file actions the platform can ask a node to do -- reviewing it is how one
    audits the agent's surface -- while the request vocabulary of the worker
    (``POST /internal/nodes/{node}/file-op``) must stay exactly the worker's.
    C3 Task 6's self-heal removal is in this table (so there is no second,
    unreviewable path) and is **not** in the worker's set: a worker may not ask
    the platform to delete an unrecorded tree, which is the whole point of the
    sweep being the control plane's decision.
    """

    op: str
    verb: str
    params: frozenset[str] = frozenset()
    callers: frozenset[str] = frozenset({"worker"})


#: The op whitelist (D18.2). ``sandbox_id`` is implicit -- every op acts on one
#: sandbox -- and is not listed as a parameter.
FILE_OPS: dict[str, FileOpSpec] = {
    "chown-workspace": FileOpSpec("chown-workspace", "chown", frozenset({"recursive"})),
    "remove-workspace": FileOpSpec("remove-workspace", "rm"),
    "walk-workspace": FileOpSpec("walk-workspace", "walk"),
    "remove-runtime": FileOpSpec("remove-runtime", "rm"),
    "chown-checkpoint": FileOpSpec(
        "chown-checkpoint", "chown", frozenset({"recursive"})
    ),
    "remove-checkpoint": FileOpSpec("remove-checkpoint", "rm"),
    "walk-checkpoint": FileOpSpec("walk-checkpoint", "walk"),
    "chown-volume-slice": FileOpSpec(
        "chown-volume-slice", "chown", frozenset({"volume", "recursive"})
    ),
    "chown-volume-root": FileOpSpec(
        "chown-volume-root", "chown", frozenset({"volume"})
    ),
    "remove-volume-slice": FileOpSpec(
        "remove-volume-slice", "rm", frozenset({"volume"})
    ),
    "chown-secret": FileOpSpec("chown-secret", "chown", frozenset({"name"})),
    "scope-slot-document": FileOpSpec(
        "scope-slot-document", "chown", frozenset({"name"})
    ),
    # Task 6: the self-heal sweep's removal. Same verb as ``remove-workspace``
    # (D18.3 -- no new verb), a *different* caller, and no record to derive a
    # uid from (that is the definition of the tree it acts on), so it is the
    # one op that needs no ``host_uid``.
    "remove-orphan-workspace": FileOpSpec(
        "remove-orphan-workspace", "rm", callers=frozenset({"self-heal"})
    ),
    # The create path's single materialization (design §4.3): tree + snapshot
    # copy + chown + volume slices, done in one agent call. `callers` is empty
    # on purpose -- the control plane derives this for itself and instructs the
    # agent (``c3_agent/materialize.py``); it is not reachable from any request
    # surface, and an empty set is what says so (``spec_for`` refuses it for
    # every caller). It is not in the worker's ``file-op`` vocabulary either:
    # that surface forwards one verb at a time, and ``node_file_op`` would
    # dispatch an unknown verb to ``walk``.
    "materialize-tree": FileOpSpec(
        "materialize-tree", "materialize", callers=frozenset()
    ),
}

#: Keys a worker must never send: the whole point of the vocabulary is that
#: the *control plane* names the target (C3 §14.4). Refused by name rather than
#: ignored, so a caller cannot believe its value was considered.
FORBIDDEN_KEYS: tuple[str, ...] = ("path", "uid", "gid", "target", "worker")

#: The own-identity slot documents. A closed set: they are the only names
#: ``W1SlotPool._write_slot_documents`` writes.
SLOT_DOCUMENTS: frozenset[str] = frozenset({"policy.json", "program.json"})

#: A secret file name is a policy entry's name. The same shape the executor's
#: own paths accept (no separators, no traversal).
_SECRET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: The create path's materialization shape (design §4.1). The sandbox's own
#: files live one level down, in ``<tree>/workspace``, so that the mount view
#: (``/workspace``) is a directory rather than the tree's root.
WORKSPACE_SUBDIR = "workspace"

#: ``0770 <sandbox uid>:<worker gid>`` -- the same permission model
#: ``envd_service.priv_helpers.WORKSPACE_MODE`` names: the sandbox owns the
#: tree, the worker (the data-plane owner) is the group, and the ``other`` bits
#: are 0 so cross-sandbox isolation stays a plain kernel DAC check.
TREE_MODE = "0770"

@dataclass(frozen=True)
class ControlPaths:
    """The control plane's own view of where things live.

    Built from the *app's* state (its effective workspace/state bases) plus the
    settings, mirroring ``_remove_local_tree_confirming``'s use of
    ``state.workspace_base``: a registry handed a different base in a test or
    by an embedder must not disagree with the paths this module derives.
    """

    workspace_base: Path
    state_base: Path
    #: Node-local platform state (``E2B_NODE_STATE_BASE``): the create marker,
    #: the disk-stat seed, ``.route-b`` and the uid pool's local files. Named
    #: here as its own root because it is *not* a reader of the shared volume --
    #: a path under it must not be refused for being outside the shared root,
    #: and a path under the shared root must not be mistaken for it.
    node_state_base: Path | None = None
    image_cache_dir: Path | None = None
    shared_volume_root: Path | None = None
    slot_tmp_root: Path | None = None
    volume_paths: Mapping[str, Path] = field(default_factory=dict)
    #: Per-sandbox quota (MB) per volume id, read from the *same* records as
    #: ``volume_paths``. :func:`derive_materialize` needs it: a volume with no
    #: per-sandbox quota mounts its root, so it has no slice to create.
    volume_quota_mb: Mapping[str, int] = field(default_factory=dict)

    def roots(self) -> tuple[Path, ...]:
        """The root discipline, in ``priv_common.c``'s order.

        Five entries once a deployment names them all -- the tree root, the
        node-local state base, the shared state base, the shared export root and
        the node-local image cache. The dedupe rules are unchanged (each entry
        is compared against the *tree root*, the shared root and the image cache
        exactly as before), so every shape that predates the reslice keeps its
        exact list -- and the C side in ``c3_agent/priv/priv_common.c`` mirrors
        this loop statement for statement.
        """
        roots: list[Path] = [self.workspace_base]
        node_state = self.node_state_base
        if node_state is not None and node_state != roots[0]:
            roots.append(node_state)
        state = self.state_base
        if state != roots[0]:
            roots.append(state)
        if self.shared_volume_root is not None:
            roots.append(self.shared_volume_root)
        if self.image_cache_dir is not None and all(
            self.image_cache_dir != root for root in roots
        ):
            roots.append(self.image_cache_dir)
        return tuple(roots)

    def contains(self, path: Path) -> bool:
        return any(_inside(path, root) for root in self.roots())


def _inside(path: Path, root: Path) -> bool:
    """``path`` under ``root`` -- resolved on both sides, like ``realpath``."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved
        return False
    return resolved == base or resolved.is_relative_to(base)


def control_paths(state, settings) -> ControlPaths:
    """The paths this deployment's file ops may touch."""
    workspace_base = Path(state.workspace_base)
    state_base = Path(getattr(state, "state_base", None) or settings.state_base or workspace_base)
    volumes = getattr(state, "volumes", None)
    volume_paths: dict[str, Path] = {}
    volume_quota_mb: dict[str, int] = {}
    if volumes is not None:
        for record in volumes.list():
            if record.path is None:  # pragma: no cover - defensive
                continue
            # Keyed by the **volume id** (``vol_…``), not the display name: the
            # mount payload's ``name`` field is the id -- the control plane's own
            # create path resolves it with ``volumes.get(name)`` and
            # ``VolumeRegistry.get`` takes an id -- so a map keyed by the display
            # name could never resolve an op and 404'd every volume operation
            # (review Task 4 slice A, Important 1).
            volume_paths[record.volume_id] = Path(record.path)
            volume_quota_mb[record.volume_id] = int(record.per_sandbox_quota_mb)
    shared = getattr(settings, "shared_volume_root", None) or getattr(
        settings, "shared_workspace_root", None
    )
    node_state = getattr(settings, "node_state_base", None) or getattr(
        state, "node_state_base", None
    )
    slot_root = getattr(settings, "slot_tmp_root", "") or ""
    return ControlPaths(
        workspace_base=workspace_base,
        state_base=state_base,
        node_state_base=Path(node_state) if node_state else None,
        image_cache_dir=Path(settings.image_cache_dir)
        if getattr(settings, "image_cache_dir", None)
        else None,
        shared_volume_root=Path(shared) if shared else None,
        slot_tmp_root=Path(slot_root) if slot_root else None,
        volume_paths=volume_paths,
        volume_quota_mb=volume_quota_mb,
    )


def spec_for(op: Any, *, caller: str = "worker") -> FileOpSpec:
    """The named op for ``caller``, or a refusal that names what it did not know."""
    if not isinstance(op, str) or op not in FILE_OPS:
        allowed = sorted(
            name for name, spec in FILE_OPS.items() if caller in spec.callers
        )
        # The list is the *caller's* vocabulary, not the whole table: an
        # unknown op is refused by naming what this surface actually offers
        # (the self-heal removal is in the table and not in the worker's set).
        raise FileOpRefusal(
            f"unknown file op {op!r}: the surface is "
            + ", ".join(allowed),
            status_code=400,
        )
    spec = FILE_OPS[op]
    if caller not in spec.callers:
        # Named, not ignored: a worker asking for the sweep's removal would
        # otherwise either succeed (an unreviewable privilege) or be told the
        # op does not exist (which would be a lie about this table).
        if not spec.callers:
            # An op with no caller surface at all (the create path's
            # materialization: the control plane derives it for itself and
            # instructs the agent directly). Saying "it is in the  set" would be
            # a hole where the reason should be.
            raise FileOpRefusal(
                f"{spec.op} has no request surface: it is derived and sent by "
                "the control plane itself, so no caller may ask for it",
                status_code=400,
            )
        raise FileOpRefusal(
            f"the {caller} surface may not ask for {spec.op} (it is in the "
            + ", ".join(sorted(spec.callers))
            + " set): refusing",
            status_code=400,
        )
    return spec


def validate_params(spec: FileOpSpec, body: Mapping[str, Any]) -> None:
    """Refuse a body that names a target, or a parameter the op has no use for."""
    for key in FORBIDDEN_KEYS:
        if key in body:
            raise FileOpRefusal(
                f"a {spec.op} report carries no {key}: the target comes from "
                "the control plane's records",
                status_code=400,
            )
    extra = set(body) - set(spec.params) - {"op", "sandbox_id"}
    if extra:
        raise FileOpRefusal(
            f"a {spec.op} report carries no "
            + ", ".join(sorted(extra))
            + ": the target comes from the control plane's records",
            status_code=400,
        )
    for name in spec.params:
        if name == "recursive":
            continue
        if name not in body:
            raise FileOpRefusal(
                f"a {spec.op} report must name {name}", status_code=400
            )


def derive_materialize(
    record,
    *,
    paths: ControlPaths,
    node_id: str,
    worker_gid: int | None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    """The one plan a create grant carries: the tree, and the volume slices.

    Everything here is derived from the control plane's own record and settings
    (§14.4 hard rule 2): the caller named a sandbox, never a path, never a uid.
    The two root checks are this half of the "two layers, neither replaces the
    other" discipline -- the agent re-derives and re-checks every path against
    its own four roots before it touches anything.
    """
    spec = FILE_OPS["materialize-tree"]
    sandbox_id = getattr(record, "sandbox_id", None)
    if not isinstance(sandbox_id, str) or not validate_sandbox_id(sandbox_id):
        raise FileOpRefusal("sandbox_id must be a valid sandbox id", status_code=400)
    if is_reserved_platform_namespace(sandbox_id):
        raise FileOpRefusal(
            f"{sandbox_id!r} is one of the platform's own namespaces, not a "
            "sandbox tree: refusing",
            status_code=400,
        )
    host_uid = getattr(record, "host_uid", None)
    if host_uid is None:
        raise FileOpRefusal(
            f"sandbox {sandbox_id} has no allocated host uid: refusing to "
            "materialize a tree nothing owns",
            status_code=503,
        )
    if worker_gid is None:
        raise FileOpRefusal(
            f"node {node_id} has not reported the worker's own gid: refusing "
            "to hand a tree to a uid without the group it belongs to",
            status_code=503,
        )
    tree_path = _workspace(paths, sandbox_id)
    _require_in_roots(paths, tree_path, spec)
    tree: dict[str, Any] = {
        "path": str(tree_path),
        "subdir": WORKSPACE_SUBDIR,
        "mode": TREE_MODE,
        "uid": int(host_uid),
        "gid": int(worker_gid),
    }
    if snapshot_id is not None:
        # N57/N58: the payload lives under the **platform namespace root** (the
        # shared export root), beside the control plane's own `snapshot.json`
        # -- the same directory `envd_service/agent.py` writes it into through
        # the helper below. Deriving it from the tree root is what put the
        # record and the payload in two namespaces, and after N58 moved the
        # live payloads it is the difference between a working restore and
        # `502 partial-copy: the snapshot source … is not a directory`
        # (measured on the cluster 2026-10-02, the first snapshot create after
        # the reslice).
        #
        # Task 2: the payload is one ``fs.tar`` (the writer in
        # ``envd_service/agent.py``). The agent reads it, and falls back to the
        # pre-tar ``fs/`` directory when the tar is not there -- every live
        # snapshot was an ``fs/`` directory on the day this shipped.
        copy_from = (
            snapshot_payload_dir(
                paths.workspace_base,
                snapshot_id,
                shared_root=paths.shared_volume_root,
            )
            / SNAPSHOT_PAYLOAD_TAR_NAME
        )
        _require_in_roots(paths, copy_from, spec)
        tree["copy_from"] = str(copy_from)
    return {"tree": tree, "slices": _volume_slices(paths, record, spec, sandbox_id, host_uid, worker_gid)}


def _volume_slices(
    paths: ControlPaths,
    record,
    spec: FileOpSpec,
    sandbox_id: str,
    host_uid: int,
    worker_gid: int,
) -> list[dict[str, Any]]:
    """One slice per mounted volume that has a per-sandbox quota (E2.5).

    A volume created with ``per_sandbox_quota_mb <= 0`` keeps the pre-E2.5
    shape -- the sandbox mounts the *root* -- so there is no slice to create
    and listing one would have the agent make a directory nothing mounts.
    """
    slices: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for mount in getattr(record, "volume_mounts", None) or []:
        name = mount.get("name") if isinstance(mount, Mapping) else None
        if not isinstance(name, str):
            continue
        volume_path = paths.volume_paths.get(name)
        if volume_path is None:
            # Fail closed rather than skip: a mount this control plane cannot
            # resolve is a record it cannot derive a target from, and silently
            # materializing the rest would hide that.
            raise FileOpRefusal(
                f"sandbox {sandbox_id} mounts volume {name!r}, which this "
                "control plane does not record: refusing to derive its slice",
                status_code=503,
            )
        if int(paths.volume_quota_mb.get(name, 0)) <= 0:
            continue
        slice_path = volume_path / sandbox_id
        if slice_path in seen:
            continue
        seen.add(slice_path)
        _require_in_roots(paths, slice_path, spec)
        slices.append(
            {
                "volume": name,
                "path": str(slice_path),
                "uid": int(host_uid),
                "gid": int(worker_gid),
            }
        )
    slices.sort(key=lambda entry: (entry["volume"], entry["path"]))
    return slices


@dataclass(frozen=True)
class FileOpInstruction:
    """What the agent is told: one verb, one CP-derived path, one uid."""

    op: str
    verb: str
    path: str
    uid: int | None = None
    gid: int | None = None
    recursive: bool = False
    worker_owned: bool = False


def derive(
    spec: FileOpSpec,
    body: Mapping[str, Any],
    *,
    paths: ControlPaths,
    host_uid: int | None,
    node_id: str,
    worker_gid: int | None,
) -> FileOpInstruction:
    """The instruction for one op, from the control plane's own records."""
    sandbox_id = body.get("sandbox_id")
    if not isinstance(sandbox_id, str) or not validate_sandbox_id(sandbox_id):
        raise FileOpRefusal("sandbox_id must be a valid sandbox id", status_code=400)
    if spec.op == "remove-orphan-workspace":
        # The sweep's removal: the tree no record claims, so there is no
        # ``host_uid`` to hand over and no worker gid to use -- and the same
        # two containment layers apply (this one, then ``maint.c``'s realpath +
        # four roots on the agent).
        if is_reserved_platform_namespace(sandbox_id):
            # ``state`` spells a legal sandbox id: the platform's own
            # namespaces are not orphans, whatever the disk scan says.
            raise FileOpRefusal(
                f"{sandbox_id!r} is one of the platform's own namespaces, not "
                "a sandbox tree: refusing",
                status_code=400,
            )
        path = _workspace(paths, sandbox_id)
        _require_in_roots(paths, path, spec)
        return FileOpInstruction(
            op=spec.op, verb=spec.verb, path=str(path), recursive=False
        )
    if host_uid is None:
        raise FileOpRefusal(
            f"sandbox {sandbox_id} has no allocated host uid: refusing to "
            "instruct the agent",
            status_code=503,
        )
    if spec.verb == "chown" and worker_gid is None:
        raise FileOpRefusal(
            f"node {node_id} has not reported the worker's own gid: refusing "
            "to hand a tree to a uid without the group it belongs to",
            status_code=503,
        )
    recursive = bool(body.get("recursive", True))
    uid: int | None = host_uid
    gid: int | None = worker_gid
    worker_owned = False
    if spec.op in ("chown-workspace", "chown-checkpoint"):
        path = _workspace(paths, sandbox_id) if spec.op == "chown-workspace" else _checkpoints(
            paths, sandbox_id
        )
        if spec.op == "chown-checkpoint":
            # Same alignment as ``chown-secret`` below: the pre-C3 shape handed
            # the checkpoint store over as ``<uid>:<uid>``.
            gid = host_uid
    elif spec.op in ("remove-workspace", "walk-workspace"):
        path = _workspace(paths, sandbox_id)
        recursive = False
    elif spec.op in ("remove-runtime",):
        path = _runtime(paths, sandbox_id)
        recursive = False
    elif spec.op in ("remove-checkpoint", "walk-checkpoint"):
        path = _checkpoints(paths, sandbox_id)
        recursive = False
    elif spec.op in ("chown-volume-slice", "remove-volume-slice"):
        path = _volume_slice(paths, body, sandbox_id)
        recursive = spec.op == "chown-volume-slice"
    elif spec.op == "chown-volume-root":
        path = _volume_root(paths, body)
        recursive = False
    elif spec.op == "chown-secret":
        path = _secret(paths, body, sandbox_id)
        recursive = False
        # The pre-C3 hand-over for a secret file was ``chown <uid>:<uid>``
        # (``executors/sandlock.py``), and the checkpoint path agrees
        # (``checkpoint_store._hand_to_sandbox``). Only the *workspace tree*
        # keeps the worker's gid as the group -- that group is what makes the
        # worker the data-plane owner of the tree it must write (fourth review,
        # minor: the two shapes used to disagree on owner metadata here).
        gid = host_uid
    elif spec.op == "scope-slot-document":
        path = _slot_document(paths, body, sandbox_id, host_uid)
        recursive = False
        # ``--worker`` replaces ``--uid`` rather than joining it (the agent
        # refuses a chown that names both): the owner stays the worker and only
        # the *group* moves, to the slot's own uid.
        worker_owned = True
        uid = None
        gid = host_uid
    else:  # pragma: no cover - the table above is exhaustive
        raise FileOpRefusal(f"unknown file op {spec.op!r}", status_code=400)
    # The second, independent layer: ``maint.c`` resolves and whitelists on the
    # agent's side (C3 §14.4 -- the two do not replace each other), and this is
    # the control plane refusing to *send* a target it did not derive into its
    # own roots.
    _require_in_roots(paths, path, spec)
    return FileOpInstruction(
        op=spec.op,
        verb=spec.verb,
        path=str(path),
        uid=uid,
        gid=gid,
        recursive=recursive,
        worker_owned=worker_owned,
    )


def _workspace(paths: ControlPaths, sandbox_id: str) -> Path:
    return paths.workspace_base / sandbox_id


def _runtime(paths: ControlPaths, sandbox_id: str) -> Path:
    return sandbox_runtime_dir(
        paths.workspace_base, sandbox_id, state_base=paths.state_base
    )


def _checkpoints(paths: ControlPaths, sandbox_id: str) -> Path:
    return sandbox_checkpoint_dir(
        paths.workspace_base, sandbox_id, state_base=paths.state_base
    )


def _volume_root(paths: ControlPaths, body: Mapping[str, Any]) -> Path:
    name = body.get("volume")
    if not isinstance(name, str) or name not in paths.volume_paths:
        raise FileOpRefusal(
            f"volume {name!r} is not a volume id this control plane records",
            status_code=404,
        )
    return paths.volume_paths[name]


def _volume_slice(
    paths: ControlPaths, body: Mapping[str, Any], sandbox_id: str
) -> Path:
    return _volume_root(paths, body) / sandbox_id


def _secret(paths: ControlPaths, body: Mapping[str, Any], sandbox_id: str) -> Path:
    name = body.get("name")
    if not isinstance(name, str) or not _SECRET_NAME.match(name):
        raise FileOpRefusal(
            f"secret name {name!r} is not a valid secret name", status_code=400
        )
    if paths.image_cache_dir is None:
        raise FileOpRefusal(
            "this control plane names no image cache (E2B_IMAGE_CACHE_DIR), so "
            "it cannot derive a sandbox's secret path: refusing",
            status_code=503,
        )
    return paths.image_cache_dir / "secrets" / sandbox_id / f"{name}.secret"


def _slot_document(
    paths: ControlPaths,
    body: Mapping[str, Any],
    sandbox_id: str,
    host_uid: int,
) -> Path:
    name = body.get("name")
    if name not in SLOT_DOCUMENTS:
        raise FileOpRefusal(
            f"slot document {name!r} is not one of "
            + ", ".join(sorted(SLOT_DOCUMENTS)),
            status_code=400,
        )
    if paths.slot_tmp_root is None:
        raise FileOpRefusal(
            "this control plane names no slot-pool scratch root "
            "(E2B_SLOT_TMP_ROOT), so it cannot derive a slot document's "
            "path: refusing",
            status_code=503,
        )
    # D20: the leaf is the worker's **instance name**, and it is computed by
    # the shared rule rather than guessed here. ``rb-<id>`` was a second copy of
    # that rule, and it was wrong for every production caller (the executor
    # always names the slot), so this op pointed at a directory that exists
    # nowhere -- the slot's own ``policy.json``/``program.json``, which is the
    # document carrying the egress-proxy credentials.
    return (
        paths.slot_tmp_root
        / str(host_uid)
        / own_identity_instance_name(sandbox_id)
        / name
    )


def _require_in_roots(paths: ControlPaths, path: Path, spec: FileOpSpec) -> None:
    if not paths.contains(path):
        raise FileOpRefusal(
            f"the derived path for {spec.op} is outside this control plane's "
            f"roots ({', '.join(str(root) for root in paths.roots())}): "
            "refusing",
            status_code=503,
        )
