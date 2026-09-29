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
| ``scope-slot-document`` | ``chown`` | ``<route-B root>/<uid>/<instance name>/<name>``, where the leaf comes from :func:`gateway_common.paths.route_b_instance_name` -- the **same** function the worker's executor names the slot with (ruling D20) |

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
    route_b_instance_name,
    sandbox_checkpoint_dir,
    sandbox_runtime_dir,
    validate_sandbox_id,
)


class FileOpRefusal(Exception):
    """A named, fail-closed refusal with the status the worker should see."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FileOpSpec:
    """One named op: its verb and the parameters it accepts."""

    op: str
    verb: str
    params: frozenset[str] = frozenset()


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
}

#: Keys a worker must never send: the whole point of the vocabulary is that
#: the *control plane* names the target (C3 §14.4). Refused by name rather than
#: ignored, so a caller cannot believe its value was considered.
FORBIDDEN_KEYS: tuple[str, ...] = ("path", "uid", "gid", "target", "worker")

#: The route-B slot documents. A closed set: they are the only names
#: ``W1SlotPool._write_slot_documents`` writes.
SLOT_DOCUMENTS: frozenset[str] = frozenset({"policy.json", "program.json"})

#: A secret file name is a policy entry's name. The same shape the executor's
#: own paths accept (no separators, no traversal).
_SECRET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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
    image_cache_dir: Path | None = None
    shared_volume_root: Path | None = None
    route_b_tmp_root: Path | None = None
    volume_paths: Mapping[str, Path] = field(default_factory=dict)

    def roots(self) -> tuple[Path, ...]:
        """The four-root discipline, in ``priv_common.c``'s order."""
        roots: list[Path] = [self.workspace_base]
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
    shared = getattr(settings, "shared_volume_root", None) or getattr(
        settings, "shared_workspace_root", None
    )
    route_b = getattr(settings, "route_b_tmp_root", "") or ""
    return ControlPaths(
        workspace_base=workspace_base,
        state_base=state_base,
        image_cache_dir=Path(settings.image_cache_dir)
        if getattr(settings, "image_cache_dir", None)
        else None,
        shared_volume_root=Path(shared) if shared else None,
        route_b_tmp_root=Path(route_b) if route_b else None,
        volume_paths=volume_paths,
    )


def spec_for(op: Any) -> FileOpSpec:
    """The named op, or a refusal that names the op it did not know."""
    if not isinstance(op, str) or op not in FILE_OPS:
        raise FileOpRefusal(
            f"unknown file op {op!r}: the surface is "
            + ", ".join(sorted(FILE_OPS)),
            status_code=400,
        )
    return FILE_OPS[op]


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
    if paths.route_b_tmp_root is None:
        raise FileOpRefusal(
            "this control plane names no route-B scratch root "
            "(E2B_ROUTE_B_TMP_ROOT), so it cannot derive a slot document's "
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
        paths.route_b_tmp_root
        / str(host_uid)
        / route_b_instance_name(sandbox_id)
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
