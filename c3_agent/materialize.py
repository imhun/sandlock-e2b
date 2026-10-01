"""Face B's create-path materialization (design v2 §4.2/§4.3).

The create path's materialization -- make the tree, copy a snapshot into it,
hand it to the sandbox's uid, and make the volume slices -- used to be the
**worker's** work plus one relayed ``chown-workspace`` per create. It is now
one instruction the **control plane** sends on the existing authenticated
CP→agent channel (the same one ``chown``/``rm``/``walk`` use): the control plane
stops *relaying* while it keeps *deciding*, and the worker is handed a ready
tree.

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

import errno
import os
import stat
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
    copy_from = tree.get("copy_from")
    existed = root.is_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MaterializeRefusal(BAD_PLAN, f"cannot create {target}: {exc}") from exc
    # ``mkdir`` follows a symlink, so the mode is set through a descriptor
    # whose whole chain was opened ``O_NOFOLLOW``: a tree (or a ``workspace/``)
    # that the previous incarnation left as a link is refused here rather than
    # chmod-ed *through*. Both directories carry the same mode -- the worker's
    # own ``apply_sandbox_ownership`` rule, which is why the sandbox can write
    # its tree while the worker (the group) still can too.
    for directory in (root, target):
        fd = _open_dir_chain(directory)
        try:
            os.fchmod(fd, mode)
        except OSError as exc:
            raise MaterializeRefusal(
                BAD_PLAN, f"cannot set {directory} to {mode:04o}: {exc}"
            ) from exc
        finally:
            os.close(fd)
    if copy_from is not None:
        source = resolve_inside(str(copy_from), roots=roots)
        if not source.is_dir():
            raise MaterializeRefusal(
                PARTIAL_COPY, f"the snapshot source {source} is not a directory"
            )
        # Into the **tree root**, not ``<root>/<subdir>``: the snapshot store's
        # ``fs/`` is a copy of the tree root itself (``agent_create_snapshot``
        # copies ``workspace_base/<id>`` into it; the control plane's in-process
        # shape expands it into ``workspace_dir`` the same way), so a merge one
        # level down would put the whole sandbox at ``workspace/workspace/...``
        # -- a tree no other path produces. ``subdir`` is only for the no-
        # snapshot case, where the tree starts empty.
        copy_tree(str(source), str(root), dir_mode=mode)
    # ``existed`` is the gate, not "did we copy something": a tree this op
    # just created gets its mode from the two ``fchmod``s above and from the
    # merge as it makes each directory, so there is nothing left over to fix
    # -- and this is a second full walk of a tree that can be large. A tree
    # that was already there can hold directories no snapshot names (the
    # residue of a previous, failed attempt), which is exactly the hole
    # ``uid_pool._prepare_directory_modes`` closes with its own ``os.walk``.
    if existed:
        root_fd = _open_dir_chain(root)
        try:
            _enforce_directory_modes(root_fd, mode)
        finally:
            os.close(root_fd)
    # Every volume slice the plan names, made the same way and *before* any
    # privilege is spent: a slice is materialization too (design §4.3 step ③),
    # and the same two layers apply -- the control plane derived the path from
    # its volume records, and this agent re-checks it against its own roots.
    slices = _plan_slices(plan, roots=roots, mode=mode)
    # Ownership goes through the audited binary (never ``os.chown``): the same
    # pool gate, the same realpath discipline, the same walk. ``worker_gid``
    # is what ``--gid`` is checked against in the child.
    sandbox_id = str(plan.get("sandbox_id") or "")
    # The instruction carries the worker's own verified identity (the CP→agent
    # body's ``worker`` block). It is what the binary's ``--gid`` gate compares
    # against, and it is deliberately *not* derived from the tree's gid -- the
    # two happen to be equal today, and the day they are not, the gate must
    # still see the worker.
    worker = plan.get("worker") if isinstance(plan.get("worker"), Mapping) else {}
    worker_uid = worker.get("uid") if isinstance(worker.get("uid"), int) else None
    worker_gid = worker.get("gid") if isinstance(worker.get("gid"), int) else gid
    handed_over = [
        run_file_op(
            "chown",
            FileOpInstruction(
                sandbox_id=sandbox_id,
                path=str(path),
                uid=uid,
                gid=gid,
                recursive=True,
                worker_uid=worker_uid,
                worker_gid=worker_gid,
            ),
            runner=runner,
            settings=settings,
        )
        for path in [root, *(entry["path"] for entry in slices)]
    ]
    return {
        "tree": {
            "path": str(root),
            "subdir": subdir,
            "mode": f"{mode:04o}",
            "uid": uid,
            "gid": gid,
            "created": not existed,
        },
        "slices": slices,
        "chown": handed_over,
    }


def _plan_slices(
    plan: Mapping[str, Any], *, roots: tuple[Path, ...], mode: int
) -> list[dict[str, Any]]:
    """Make the plan's volume slices; returns them resolved.

    The plan's own order is kept (the control plane sorts it), so two creates
    against the same volumes hand the trees over in the same order -- an
    observable property that makes a stuck slice reproducible rather than
    arbitrary.
    """
    entries = plan.get("slices") or []
    if not isinstance(entries, list):
        raise MaterializeRefusal(BAD_PLAN, "slices is not a list")
    made: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise MaterializeRefusal(BAD_PLAN, "a slice is not an object")
        path = resolve_inside(str(entry.get("path", "")), roots=roots)
        uid = _plan_int(entry, "uid")
        gid = _plan_int(entry, "gid")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MaterializeRefusal(
                PARTIAL_COPY, f"cannot create the slice {path}: {exc}"
            ) from exc
        fd = _open_dir_chain(path)
        try:
            os.fchmod(fd, mode)
        except OSError as exc:
            raise MaterializeRefusal(
                BAD_PLAN, f"cannot set {path} to {mode:04o}: {exc}"
            ) from exc
        finally:
            os.close(fd)
        made.append(
            {
                "volume": str(entry.get("volume") or ""),
                "path": path,
                "uid": uid,
                "gid": gid,
            }
        )
    return made


# --------------------------------------------------------------- the copy

#: Every directory this module opens is opened with all three: read-only, "it
#: must be a directory", and "do not follow a symlink for the last component".
#: The third is the whole destination-side discipline (design §4.3.1).
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _is_symlink(name: str, parent_fd: int) -> bool:
    """``lstat`` one name relative to a descriptor, without following anything."""
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode)
    except OSError:
        return False


def _open_dir_at(name: str, parent_fd: int, *, where: str) -> int:
    """Open one directory component no-follow; a link there is refused by name.

    The errno a platform reports for "``O_NOFOLLOW`` met a symlink" is not
    portable (Linux says ``ELOOP``, Darwin says ``ENOTDIR`` for the
    ``O_DIRECTORY`` form), so the *meaning* is established with an explicit
    ``lstat`` rather than inferred from a number: the caller's next move
    depends on knowing this was a link, not merely that the open failed.
    """
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if _is_symlink(name, parent_fd):
            raise MaterializeRefusal(
                DESTINATION_IS_A_SYMLINK,
                f"{where}: {name!r} is a symbolic link: refusing to write "
                "through it",
            ) from exc
        raise


def _open_dir_chain(path: Path) -> int:
    """Open ``path`` as a directory fd, resolving **every** segment no-follow.

    Built from the root down with each step relative to the previous
    descriptor, so a component that is a symlink (or that is swapped for one
    between two components) is an ``ELOOP`` rather than a step out of the tree.
    The caller owns the returned descriptor.
    """
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = _open_dir_at(part, fd, where=f"opening {path}")
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_child_dir(name: str, parent_fd: int, mode: int) -> int:
    """Open (or create, then open) ``name`` inside ``parent_fd``, no-follow."""
    try:
        return _open_dir_at(name, parent_fd, where="the destination tree")
    except FileNotFoundError:
        os.mkdir(name, mode, dir_fd=parent_fd)
        return _open_dir_at(name, parent_fd, where="the destination tree")


def _splice(src_fd: int, dst_fd: int) -> None:
    """Copy one file's bytes between two descriptors."""
    while True:
        chunk = os.read(src_fd, 1 << 20)
        if not chunk:
            return
        offset = 0
        while offset < len(chunk):
            offset += os.write(dst_fd, chunk[offset:])


def _refusal_for(err: OSError, path: str, *, doing: str) -> MaterializeRefusal:
    """Name the failure the way the caller has to branch on it."""
    if err.errno == errno.ELOOP:
        # ``O_NOFOLLOW`` on the last component, or a symlink somewhere in the
        # chain: the destination already holds a link where this entry goes.
        return MaterializeRefusal(
            DESTINATION_IS_A_SYMLINK, f"{doing} {path}: {err.strerror}"
        )
    if err.errno in (errno.ENOTDIR, errno.EISDIR, errno.EEXIST):
        return MaterializeRefusal(
            ALREADY_EXISTS_AS_A_FILE, f"{doing} {path}: {err.strerror}"
        )
    # Everything else is "the step ran and did not finish". It must never read
    # as success: a half-copied tree is handed to the orphan path instead
    # (§4.3.1 requirement 3).
    return MaterializeRefusal(PARTIAL_COPY, f"{doing} {path}: {err.strerror}")


def _copy_regular_file(src: str, name: str, dst_fd: int) -> None:
    """Copy one regular file into ``dst_fd``, following nothing on either side."""
    src_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(src_fd)
        if not stat.S_ISREG(info.st_mode):
            # A race between ``scandir`` and this open (the source is the
            # platform's own snapshot store, but "it changed under me" must be
            # a refusal, not a surprise copy).
            raise MaterializeRefusal(
                ALREADY_EXISTS_AS_A_FILE, f"{src} is not a regular file"
            )
        try:
            child_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                stat.S_IMODE(info.st_mode),
                dir_fd=dst_fd,
            )
        except OSError as exc:
            if _is_symlink(name, dst_fd):
                raise MaterializeRefusal(
                    DESTINATION_IS_A_SYMLINK,
                    f"{src} would be written through the link {name}: refusing",
                ) from exc
            raise
        try:
            # ``umask`` masks the mode above; the snapshot's own bits are what
            # the worker's ``copytree`` used to reproduce, so set them exactly.
            os.fchmod(child_fd, stat.S_IMODE(info.st_mode))
            _splice(src_fd, child_fd)
        finally:
            os.close(child_fd)
    finally:
        os.close(src_fd)


def _write_symlink(link: str, name: str, dst_fd: int) -> None:
    """Recreate a link -- never dereference it.

    A link already sitting at ``name`` is replaced (the snapshot is the newer
    truth for that entry); anything else there is a type conflict and is
    refused rather than deleted, because the tree being merged into belongs to
    the sandbox.
    """
    try:
        os.symlink(link, name, dir_fd=dst_fd)
        return
    except FileExistsError:
        pass
    info = os.lstat(name, dir_fd=dst_fd)
    if not stat.S_ISLNK(info.st_mode):
        raise MaterializeRefusal(
            ALREADY_EXISTS_AS_A_FILE,
            f"{name} exists and is not a symlink: refusing to replace it",
        )
    os.unlink(name, dir_fd=dst_fd)
    os.symlink(link, name, dir_fd=dst_fd)


def _copy_into(src_dir: Path, dst_fd: int, dir_mode: int) -> int:
    """One directory level: recreate every entry inside ``dst_fd``."""
    copied = 0
    with os.scandir(src_dir) as entries:
        ordered = sorted(entries, key=lambda entry: entry.name)
    for entry in ordered:
        name = entry.name
        try:
            if entry.is_symlink():
                # Tested *first*: ``is_dir()`` would follow the link and a
                # snapshot could then steer the copy anywhere.
                _write_symlink(os.readlink(entry.path), name, dst_fd)
            elif entry.is_dir(follow_symlinks=False):
                child_fd = _open_child_dir(name, dst_fd, dir_mode)
                try:
                    os.fchmod(child_fd, dir_mode)
                    copied += _copy_into(Path(entry.path), child_fd, dir_mode)
                finally:
                    os.close(child_fd)
            else:
                _copy_regular_file(entry.path, name, dst_fd)
        except MaterializeRefusal:
            raise
        except OSError as exc:
            raise _refusal_for(exc, entry.path, doing="copying") from exc
        copied += 1
    return copied


def _enforce_directory_modes(dst_fd: int, dir_mode: int) -> int:
    """Give every real directory below ``dst_fd`` the tree's mode.

    The worker is the *group* on the tree, never its owner, so the create
    contract is "every directory in the tree is ``dir_mode``" -- the rule the
    old worker path kept by walking the whole tree
    (``uid_pool._prepare_directory_modes``); a ``0755`` one level down leaves
    the data plane unable to write there. The merge above only visits the
    entries the snapshot carries, so a directory an earlier incarnation left
    behind is exactly what this pass is for.

    Links are skipped, never followed: they are recreated as links, and
    ``chmod`` would act on the target instead. Returns the number of
    directories whose mode was set.
    """
    children: list[str] = []
    with os.scandir(dst_fd) as entries:
        for entry in entries:
            # Tested inside the ``scandir`` on purpose: the entry's own
            # cached stat is what decides, and a link must be skipped rather
            # than opened (``_open_dir_at`` would refuse it by name).
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            children.append(entry.name)
    count = 0
    for name in sorted(children):
        child_fd = _open_dir_at(name, dst_fd, where="the destination tree")
        try:
            try:
                os.fchmod(child_fd, dir_mode)
            except OSError as exc:
                raise _refusal_for(exc, name, doing="setting the mode of") from exc
            count += 1 + _enforce_directory_modes(child_fd, dir_mode)
        finally:
            os.close(child_fd)
    return count


def copy_tree(src: str, dst: str, *, dir_mode: int = 0o770) -> int:
    """Recursively copy ``src`` into the existing directory ``dst``.

    Returns the number of entries copied. Two disciplines, one per side, and
    neither is optional (design §4.3.1):

    * **source** -- symlinks are recreated with ``os.symlink`` and never
      dereferenced (``is_symlink`` is tested before ``is_dir``);
    * **destination** -- this directory is merged into, and the sandbox's
      previous incarnation could write there, so every segment is opened
      relative to a descriptor with ``O_NOFOLLOW`` (``dir_fd`` bookkeeping, no
      ``os.path.join`` string building) and a link anywhere in the way is a
      named refusal.

    Directories are created with ``dir_mode`` (``0770``), the invariant the
    worker's ``apply_sandbox_ownership`` keeps; files keep the snapshot's own
    bits. Ownership is not this function's business -- the caller hands the
    whole tree over once, after the copy.
    """
    source = Path(src)
    if source.is_symlink() or not source.is_dir():
        raise MaterializeRefusal(
            PARTIAL_COPY, f"the copy source {src} is not a directory"
        )
    try:
        dst_fd = _open_dir_chain(Path(dst))
    except OSError as exc:
        raise _refusal_for(exc, dst, doing="opening") from exc
    try:
        return _copy_into(source, dst_fd, dir_mode)
    finally:
        os.close(dst_fd)
