"""Face B's create-path materialization (design §4.3).

The create path's materialization -- make the tree, copy a snapshot into it,
hand it to the sandbox's uid, and make the volume slices -- used to be the
**worker's** work plus one relayed ``chown-workspace`` per create. It is now
one signed plan (:mod:`gateway_common.create_grant`) that the worker carries
straight to its own node's agent, so the control plane stops *relaying* while
it keeps *deciding*.

Two rules shape this module, and both are about what it may **not** do:

* **It derives nothing.** Every path, uid, gid and mode in the plan was
  derived by the control plane from its own records and then signed; this
  module only re-checks that each path lands inside *this* agent's own four
  roots (the independent second layer, C3 §14.4 -- the two do not replace each
  other) and then does exactly what the plan says.
* **It never chowns itself.** Ownership is handed over by the audited
  ``e2b-maint`` binary, through :func:`c3_agent.fileops.run_file_op`, so the
  uid-pool gate, the ``realpath`` discipline and the group gate stay in the
  one place they were audited. The gid is therefore written into the child's
  environment: ``maint.c`` checks ``--gid`` against the worker's own gid.

Refusals are *named* (:class:`MaterializeRefusal`) because the caller's
behaviour depends on which one it was: a path outside the roots is a broken
plan the control plane must fix, while a partially-copied tree is a step that
ran and failed and must never read as success (design §4.3.1 hard requirement
3).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from c3_agent.fileops import (
    FileOpInstruction,
    MaintRunner,
    run_file_op,
)

#: The refusal reasons callers branch on. Spelling them once keeps the agent's
#: HTTP mapping and the tests' expectations from drifting apart.
PATH_OUTSIDE_ROOTS = "path-outside-roots"
DESTINATION_IS_A_SYMLINK = "destination-is-a-symlink"
PARTIAL_COPY = "partial-copy"
ALREADY_EXISTS_AS_A_FILE = "already-exists-as-a-file"
BAD_PLAN = "bad-plan"


class MaterializeRefusal(Exception):
    """A named, fail-closed refusal from one materialization."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def agent_roots(settings) -> tuple[Path, ...]:
    """This agent's own four roots, resolved -- ``priv_common.c``'s order.

    The same values :func:`c3_agent.fileops.maint_env` writes into the child's
    environment, read from the same settings object: a path this module accepts
    is a path the binary would accept, and a deployment that re-points one of
    them moves both together.
    """
    roots: list[Path] = [Path(settings.workspace_base).resolve()]
    state = Path(settings.state_base or settings.workspace_base).resolve()
    if state != roots[0]:
        roots.append(state)
    for extra in (settings.shared_volume_root, settings.image_cache_dir):
        if not extra:
            continue
        resolved = Path(extra).resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def resolve_inside(path: str, *, roots: tuple[Path, ...]) -> Path:
    """``realpath(path)``, or a named refusal when it leaves the four roots."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise MaterializeRefusal(
            PATH_OUTSIDE_ROOTS, f"{path!r} is not an absolute path"
        )
    try:
        resolved = Path(path).resolve()
    except OSError as exc:  # pragma: no cover - a path that cannot be resolved
        raise MaterializeRefusal(PATH_OUTSIDE_ROOTS, f"{path!r}: {exc}") from exc
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise MaterializeRefusal(
        PATH_OUTSIDE_ROOTS,
        f"{path!r} resolves to {resolved}, outside "
        + ", ".join(str(root) for root in roots),
    )


def _plan_int(plan: Mapping[str, Any], key: str) -> int:
    value = plan.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MaterializeRefusal(BAD_PLAN, f"{key} must be an integer")
    return value


def _plan_subdir(plan: Mapping[str, Any]) -> str:
    """The one path component the sandbox's files live under.

    A single component on purpose: the plan is a signed derivation, but a
    caller that could name ``..`` here could still steer the mkdir out of the
    tree the root check just approved.
    """
    subdir = plan.get("subdir")
    if (
        not isinstance(subdir, str)
        or not subdir
        or subdir in (".", "..")
        or "/" in subdir
    ):
        raise MaterializeRefusal(
            BAD_PLAN, f"subdir {subdir!r} is not one path component"
        )
    return subdir


def _plan_mode(plan: Mapping[str, Any]) -> int:
    mode = plan.get("mode")
    if not isinstance(mode, str) or not mode or any(
        character not in "01234567" for character in mode
    ):
        raise MaterializeRefusal(BAD_PLAN, f"mode {mode!r} is not octal")
    return int(mode, 8)


def materialize_tree(
    plan: Mapping[str, Any], *, settings, runner: MaintRunner
) -> dict[str, Any]:
    """Do everything one plan names: make the tree, then hand it over.

    The order is deliberate: the tree exists (and is ``chmod``-ed *before* the
    ``chown``, the ordering this repo keeps everywhere it matters) before any
    privilege is spent on it.
    """
    if not isinstance(plan, Mapping):
        raise MaterializeRefusal(BAD_PLAN, "the plan is not an object")
    tree = plan.get("tree")
    if not isinstance(tree, Mapping):
        raise MaterializeRefusal(BAD_PLAN, "the plan names no tree")
    roots = agent_roots(settings)
    root = resolve_inside(str(tree.get("path", "")), roots=roots)
    subdir = _plan_subdir(tree)
    uid = _plan_int(tree, "uid")
    gid = _plan_int(tree, "gid")
    mode = _plan_mode(tree)
    target = root / subdir
    # ``realpath`` of a path whose parent does not exist yet is the lexical
    # normalisation, so re-check the *joined* path: a ``root`` that is itself
    # fine must not become a doorway.
    resolve_inside(str(target), roots=roots)
    existed = root.is_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MaterializeRefusal(BAD_PLAN, f"cannot create {target}: {exc}") from exc
    # Every *directory* in the tree carries the same mode (the worker's own
    # ``apply_sandbox_ownership`` rule): the tree root, and the sandbox's
    # ``workspace/`` one level down.
    for directory in (root, target):
        try:
            os.chmod(directory, mode)
        except OSError as exc:
            raise MaterializeRefusal(
                BAD_PLAN, f"cannot set {directory} to {mode:04o}: {exc}"
            ) from exc
    # Ownership goes through the audited binary (never ``os.chown``): the same
    # pool gate, the same realpath discipline, the same walk. ``worker_gid``
    # is what ``--gid`` is checked against in the child.
    instruction = FileOpInstruction(
        sandbox_id=str(plan.get("sandbox_id") or ""),
        path=str(root),
        uid=uid,
        gid=gid,
        recursive=True,
        worker_gid=gid,
    )
    answer = run_file_op("chown", instruction, runner=runner, settings=settings)
    return {
        "tree": {
            "path": str(root),
            "subdir": subdir,
            "mode": f"{mode:04o}",
            "uid": uid,
            "gid": gid,
            "created": not existed,
        },
        "chown": answer,
    }
