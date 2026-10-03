"""One tar extractor, shared by every image that reads a sandbox archive.

The same operation -- "take tar bytes somebody else produced and land them in a
directory" -- is needed by two services that ship as **different images**:

* the control plane, importing a migration archive (``_import_sandbox_archive``);
* the per-node agents: ``c3_agent``'s materialization unpacks a snapshot's
  ``fs.tar`` (Task 2), and ``envd_service``'s degraded create path unpacks the
  same payload when the control plane did not materialize the tree itself.

Until Task 2 the first two call sites each carried their own copy of the guard
(``control_plane/api/sandboxes.py::_extract_sandbox_archive`` and
``envd_service/agent.py::_extract_sandbox_archive``). The agent is a different
image, so "import the control plane's" was never available -- which is exactly
how a path-escape guard ends up written twice and fixed once. It lives here
instead: both images already depend on ``gateway_common``, and each caller
imports *this* function object.

Two rules, and they are the whole point:

* **member filtering** -- a symlink member whose target is absolute is skipped
  (volume mounts are archived as links to paths that only exist on the source
  node; the target is re-created by provisioning), and a member whose *name*
  walks out of the destination is refused;
* **destination containment** -- every member is resolved against the
  destination before it is written, so a link the previous incarnation left in
  the destination is a named refusal rather than a way out of the tree.

Reasons are named (:class:`ArchiveRefusal`) because the callers branch on
them: "the payload is hostile/broken" (502 on a create) is not "the plan is
wrong" (400), and neither is "the destination holds a link" -- which is the
§4.3.1 requirement that the *destination* side has its own name.

The unpack **streams the member data**: members are walked one at a time and
each one's bytes go through ``tarfile``'s own 64 KiB buffer, so the payload is
never held whole (that is the driver of the OOM the agent's ``maint`` container
hit during Task 1's measurement, ``docs/create-local-first-design.md`` §3.0:
900 MiB of tree into a 512 MiB cgroup).

It does **not** make the unpack index-free, and this module must not be read as
claiming it does: CPython's ``TarFile.next()`` appends every ``TarInfo`` to
``TarFile.members`` no matter who iterates, so a caller that avoids
``getmembers()`` still pays ~430 B per member (measured on 3.12.13, 2026-10-02:
200 000 members ⇒ ``len(tar.members)`` 200 000 and an 85.7 MB peak for a
102.4 MB archive of empty members ⇒ 428.6 B/member; review round 1 measured
88.8 MB ≈ 444 B/member for the same shape). A pathological archive of
~2 000 000 empty members (~1 GiB of 512 B headers) therefore still costs the
agent ~0.9 GB of index. Bounding *that* is this module's own job and it is done
here, next to Task 3's byte cap: :data:`DEFAULT_ARCHIVE_MAX_MEMBERS` with
:func:`resolve_member_max` below, on by default so all three unpack call sites
are covered without touching one of them. The numbers its value comes from are
measured by ``deploy/scripts/acceptance/archive_member_index_memory.py`` and
quoted at the constant.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tarfile
import time
from pathlib import PurePosixPath
from pathlib import Path

logger = logging.getLogger(__name__)

#: A member's *name* walks out of the destination (``..`` or an absolute path).
MEMBER_ESCAPES = "archive-member-escapes"
#: A clean member name that still lands outside: the destination holds a link.
DESTINATION_IS_A_SYMLINK = "destination-is-a-symlink"
#: The extraction started and did not finish (a truncation, an I/O error).
ARCHIVE_IS_CORRUPT = "archive-is-corrupt"
#: The caller's destination is not a directory to unpack into.
PARTIAL_UNPACK = "partial-unpack"
#: The archive carries more members than the unpack may index (a pathological
#: input, not a corrupt one: the member *index* is what grows with the count).
TOO_MANY_MEMBERS = "archive-too-many-members"
#: The unpack crossed its wall-clock budget (N67). The member cap bounds the
#: index this walk pays for; it does not bound the *time*: 1 500 000 members
#: measured ~12 min of CPU (``deploy/scripts/acceptance/archive_member_index_memory.py``),
#: which is 12 minutes of a ``maint`` slot a create waits on. A pathological
#: *input* again, so it is named apart from the corrupt-payload reasons.
TIME_BUDGET_EXCEEDED = "archive-time-budget-exceeded"

class ArchiveRefusal(Exception):
    """A named, fail-closed refusal from one archive extraction."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


#: The read-side member cap: what a payload may carry before the unpack refuses
#: it, and the value is **measured** rather than picked. The unpack's member
#: index is CPython's -- ``TarFile.members`` grows one ``TarInfo`` per member no
#: matter who iterates -- so the cost is per-member and invisible to the byte
#: cap: 2 000 000 empty members is ~1 GiB of 512 B headers, *inside*
#: ``E2B_TREE_COPY_MAX_BYTES`` (1.25 GiB), while the ~0.6 GiB of index at
#: 1 500 000 members is what has to fit the agent's 2 GiB ``maint`` limit
#: (``deploy/k8s/c3-agent.yaml``). The scaled measurement is in
#: ``deploy/scripts/acceptance/archive_member_index_memory.py``.
DEFAULT_ARCHIVE_MAX_MEMBERS = 1_500_000


def resolve_member_max() -> int:
    """The member cap for this process, from ``E2B_ARCHIVE_MAX_MEMBERS``.

    Module level, not a ``Settings`` field: unlike the byte cap this guards
    against a pathological *input* rather than a capacity the replicas have to
    agree on, so each of the three call sites may read its own environment and
    none of them has to pass anything. Empty or unparsable falls back to
    :data:`DEFAULT_ARCHIVE_MAX_MEMBERS`; ``0`` -- and only ``0`` -- disables
    the cap (the same discipline as the byte knob's ``0``).
    """
    raw = os.environ.get("E2B_ARCHIVE_MAX_MEMBERS", "").strip()
    try:
        value = int(raw)
    except ValueError:  # unset, empty, or not a number: the default stands
        return DEFAULT_ARCHIVE_MAX_MEMBERS
    return value if value >= 0 else DEFAULT_ARCHIVE_MAX_MEMBERS


#: The wall-clock budget for one unpack: how long a single archive may take
#: before the walk refuses it. On by default for the same reason the member cap
#: is: the payload is the caller's, so every unpack call site is covered
#: without passing anything. Three minutes is a value the *product* can hold --
#: a full 1 GiB quota tree unpacks in well under a minute, while the 12-minute
#: pathological walk the member cap still admits is refused.
DEFAULT_ARCHIVE_MAX_SECONDS = 180.0

#: Members read between two clock reads. ``time.monotonic()`` per member is a
#: syscall-scale cost on a 1.5 M-member walk, so the clock is *sampled*: once
#: at the first member (a tiny archive under a tiny budget still refuses) and
#: then every this many members.
TIME_BUDGET_CHECK_EVERY = 4096


def resolve_time_budget() -> float:
    """The unpack budget for this process, from ``E2B_ARCHIVE_MAX_SECONDS``.

    Same discipline as :func:`resolve_member_max`: read at call time, so every
    unpack call site is covered without passing anything; empty or unparsable
    falls back to :data:`DEFAULT_ARCHIVE_MAX_SECONDS`; ``0`` -- and only ``0``
    -- disables the check. A float, not an int, so a sub-second budget is
    settable (the tests' own lever).
    """
    raw = os.environ.get("E2B_ARCHIVE_MAX_SECONDS", "").strip()
    try:
        value = float(raw)
    except ValueError:  # unset, empty, or not a number: the default stands
        return DEFAULT_ARCHIVE_MAX_SECONDS
    return value if value >= 0 else DEFAULT_ARCHIVE_MAX_SECONDS


def _now() -> float:
    """The clock the unpack budget reads (patched by the tests' own lever)."""
    return time.monotonic()


def _guard_member(dest: Path, name: str, safe_parents: set[str]) -> None:
    """Refuse a member whose *name* or whose *path* leaves the destination.

    The two escapes are named apart on purpose: a name that walks out (``..``,
    an absolute path) is a broken archive, while a clean name whose parent
    component is a symbolic link in the destination is the failure the caller
    has to be able to name (``materialize.py`` turns it into its own
    ``destination-is-a-symlink``, the §4.3.1 rule that a tree refusing to be
    written through is not the same event as a corrupt payload).

    **The parent check is cached per directory** and that cache is the whole
    reason this is affordable. Resolving each member's own path against the
    destination costs one ``lstat`` per component *per member*, and on the
    shared NAS that is a metadata round trip: measured 2026-10-02 inside one
    sandbox over 203 members, the shared extractor was **13.0 s** against
    stdlib ``extractall(filter="data")``'s **11.0 s** -- ~2 ms per member of
    pure re-checking, which would have made the whole tar task a regression on
    the restore leg. With the cache the check is an ``lstat`` per *distinct
    parent directory* (one, for a snapshot's ``workspace/``) plus a set lookup
    per member. Parent components are only ever added to ``safe_parents`` after
    every one of their own components was ``lstat``-ed and found not to be a
    link, and the extraction has the tree to itself (the sandbox is frozen for
    a capture and not yet running for a restore), so a cached decision cannot
    be invalidated underneath us.
    """
    parts = PurePosixPath(name).parts
    if not name or name.startswith("/") or any(part == ".." for part in parts):
        raise ArchiveRefusal(
            MEMBER_ESCAPES, f"archive member {name!r} escapes the destination"
        )
    parent = PurePosixPath(*parts[:-1])
    key = str(parent)
    if key in safe_parents:
        return
    probe = dest
    for part in parts[:-1]:
        probe = probe / part
        try:
            info = os.lstat(probe)
        except FileNotFoundError:
            # Nothing is there yet, so nothing can be a link: every component
            # from here down is one this extraction makes itself.
            break
        except OSError as exc:
            raise ArchiveRefusal(
                PARTIAL_UNPACK, f"cannot inspect {probe}: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ArchiveRefusal(
                DESTINATION_IS_A_SYMLINK,
                f"the destination holds a symbolic link at {part!r} on the "
                f"way to {name!r}: refusing to write through it",
            )
    safe_parents.add(key)


def _guard_directory(dest: Path, name: str) -> None:
    """A directory member's *own* path may not be a link in the destination.

    ``workspace/`` is one member out of a snapshot's thousands, so this check
    costs one ``lstat`` per *directory* rather than per entry -- and it is the
    one that matters most: writing a directory through a link puts a whole
    subtree somewhere the tree never named. A **file** member whose own name is
    a link in the destination is left to ``tarfile``'s own ``data`` filter: it
    refuses any target that resolves outside the destination (which is what an
    escape looks like), and a link that stays inside the destination lands the
    bytes elsewhere *in the same tree*, which is a shape this module documents
    rather than replicates. The directory-less copy path (``copy_tree``) is
    stricter there; the difference is named in the module docstring.
    """
    try:
        info = os.lstat(dest / name)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ArchiveRefusal(
            PARTIAL_UNPACK, f"cannot inspect {dest / name}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ArchiveRefusal(
            DESTINATION_IS_A_SYMLINK,
            f"the destination holds a symbolic link at {name!r}: refusing to "
            "write through it",
        )


def _named_filter(member, dest_path):
    """``tarfile``'s ``data`` filter, with its refusals translated by name.

    The stdlib filter is the **independent second layer**: every member is
    resolved against the destination again, whoever else looked at it (the same
    doctrine as the control plane's derivation plus the agent's own re-check).
    Its errors are the shapes callers have to tell apart, so they are named
    here instead of surfacing as one bare ``TarError`` -- and the (cheap)
    classification runs on the error path only, so the happy path costs exactly
    what stdlib's filter costs.
    """
    try:
        return tarfile.data_filter(member, dest_path)
    except tarfile.TarError as exc:
        raise ArchiveRefusal(_reason_for_filter_error(exc), str(exc)) from exc


def _reason_for_filter_error(exc: BaseException) -> str:
    """Which named refusal one of stdlib's filter errors is."""
    name = type(exc).__name__
    if "LinkOutside" in name or "Outside" in name:
        # "The path this member lands on resolves outside the destination" --
        # a name that walks out is caught before we get here, so what is left
        # is a link (the destination's or the archive's) on the way.
        return DESTINATION_IS_A_SYMLINK
    return MEMBER_ESCAPES


def extract_sandbox_archive(
    archive_path: Path,
    dest: Path,
    *,
    max_members: int | None = None,
    max_seconds: float | None = None,
) -> int:
    """Unpack ``archive_path`` into the existing directory ``dest``.

    Returns the number of members written. Raises :class:`ArchiveRefusal` (and
    never a bare ``tarfile``/``OSError``) so both callers translate one shape.

    ``max_members`` defaults to :func:`resolve_member_max` (``None`` means "read
    the environment"), so the cap is on for every caller that passes nothing;
    ``0`` disables it. ``max_seconds`` is the same shape over
    :func:`resolve_time_budget`: the walk refuses with
    :data:`TIME_BUDGET_EXCEEDED` once the wall clock it has spent passes the
    budget, sampled (never per member) so the check stays cheap.
    """
    limit = resolve_member_max() if max_members is None else max_members
    budget = resolve_time_budget() if max_seconds is None else max_seconds
    destination = Path(dest)
    resolved_dest = destination.resolve()
    if not resolved_dest.is_dir():
        raise ArchiveRefusal(
            PARTIAL_UNPACK, f"the destination {destination} is not a directory"
        )
    written = 0
    seen = 0
    safe_parents: set[str] = set()
    # Read once, before the walk: the budget is over the *whole* unpack.
    started = _now() if budget else 0.0
    try:
        with tarfile.open(Path(archive_path), "r:*") as tar:
            for member in tar:
                # Must stay *before* the ``continue`` below: the cap counts the
                # index the walk has already paid for, dropped members included.
                seen += 1
                if limit and seen > limit:
                    # Before the member that crosses the cap is written: the
                    # refusal is about the *index* the walk has already paid
                    # for, so it has to land as early as that index does.
                    raise ArchiveRefusal(
                        TOO_MANY_MEMBERS,
                        f"{archive_path} holds more than {limit} members "
                        f"(seen {seen} so far; E2B_ARCHIVE_MAX_MEMBERS, "
                        "0 disables it)",
                    )
                # Sampled on purpose: one ``monotonic()`` per member would be a
                # syscall-scale cost on the walk the cap already admits (1.5 M
                # members), so the clock is read once at the first member and
                # then every ``TIME_BUDGET_CHECK_EVERY``.
                if budget and (seen == 1 or seen % TIME_BUDGET_CHECK_EVERY == 0):
                    elapsed = _now() - started
                    if elapsed > budget:
                        raise ArchiveRefusal(
                            TIME_BUDGET_EXCEEDED,
                            f"{archive_path} exceeded its {budget:g}s unpack "
                            f"budget after {elapsed:.3f}s ({seen} members seen; "
                            "E2B_ARCHIVE_MAX_SECONDS, 0 disables it)",
                        )
                if member.issym() and os.path.isabs(member.linkname):
                    # A volume mount from the source node: the path does not
                    # exist here and provisioning re-creates it.
                    continue
                _guard_member(destination, member.name, safe_parents)
                if member.isdir():
                    _guard_directory(destination, member.name)
                try:
                    tar.extract(member, destination, filter=_named_filter)
                except TypeError:  # pragma: no cover - Python < 3.12
                    tar.extract(member, destination)
                written += 1
    except ArchiveRefusal:
        raise
    except (tarfile.TarError, EOFError) as exc:
        raise ArchiveRefusal(
            ARCHIVE_IS_CORRUPT, f"{archive_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ArchiveRefusal(PARTIAL_UNPACK, f"{archive_path}: {exc}") from exc
    return written


# ---------------------------------------------------------------------------
# Landing an archive in a live tree without leaving half of one
# ---------------------------------------------------------------------------
#
# ``extract_sandbox_archive`` writes into the directory it is handed, so a
# refusal halfway through a *live* tree leaves the members it already wrote
# there. For the control plane that never mattered: ``stage_tree_from_archive``
# unpacks into a dot-named sibling and removes it on failure. The two worker
# call sites (a snapshot restore and the agent's materialization) unpacked
# straight into the tree root, so a payload refused at the member cap left
# 1.5 M files in a sandbox's own tree -- with no record that would let the TTL
# sweeper see them (N68).
#
# ``extract_into_place`` is that operation done where a refusal costs nothing.
# It is the one implementation both worker call sites use; the staging logic
# deliberately stays out of ``extract_sandbox_archive`` because the control
# plane's path already stages for itself.
#
# The publish itself has two shapes, and only one of them is atomic (N76). The
# absent-destination rename is one step. The merge into an *existing* tree is
# per-entry ``os.replace``, because the tree keeps everything the payload does
# not name and ``rename(2)`` onto a non-empty directory is ``ENOTEMPTY``:
# "build ``<dest>.next`` and swap" would need ``RENAME_EXCHANGE`` (Linux-only;
# support over the deployment's NFS is unverified) or an "old tree aside, new
# tree in" window that is not atomic either. So a merge killed halfway can
# still leave a half-published tree. What this module owes the operator in that
# case is *where it stopped*: the refusal names the entries already published,
# and a retry converges -- every entry is an idempotent ``os.replace``,
# so re-running the same materialization finishes the merge instead of
# doubling it. The caller's own rollback decides what happens to the tree; a
# refused create's tree is reclaimed by the node reconcile / orphan-tree GC.


def extract_into_place(
    archive_path: Path,
    dest: Path,
    *,
    max_members: int | None = None,
    max_seconds: float | None = None,
) -> int:
    """Unpack ``archive_path`` beside ``dest``, then publish it at ``dest``.

    The archive lands in ``<dest>.importing`` -- same parent, so the publish is
    a rename within one filesystem and never ``EXDEV``. Nothing reaches
    ``dest`` until every member has been written, and a refusal (the archive's
    own, or the destination guards') removes the staging tree and re-raises the
    same exception, unchanged: the caller sees exactly what a direct unpack
    would have raised, and the live tree is exactly as it was.

    The publish follows what ``dest`` already is:

    * **absent** -- ``os.rename`` the staged tree into place, one step. The
      target must not be a mount point for this to work (``rename(2)`` onto a
      mount point is ``EBUSY``); none of this repo's shapes makes one at a
      tree root -- ``workspace-root`` is mounted at ``E2B_WORKSPACE_BASE`` and
      a tree is a subdirectory of it, while volume slices are bound *inside*
      the tree.
    * **an existing directory** -- merge the staged entries into it
      (``os.replace`` per entry, recursing into directories that are already
      there). That is the shape the agent's materialization has always had: a
      snapshot merges at the tree root and keeps what the previous incarnation
      left (``dirs_exist_ok`` semantics), so a rename onto a non-empty tree
      would both fail with ``ENOTEMPTY`` and drop files the tree still needs.

    Either way the destination guards are re-run against ``dest`` first: the
    unpack itself only ever saw the clean staging directory, so "the tree holds
    a link on the way to a member" has to be checked on the side that keeps it.
    """
    target = Path(dest)
    staging = target.parent / f"{target.name}.importing"
    if target.exists() or target.is_symlink():
        if not target.resolve().is_dir():
            raise ArchiveRefusal(
                PARTIAL_UNPACK, f"the destination {target} is not a directory"
            )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveRefusal(
            PARTIAL_UNPACK, f"cannot create the parent of {target}: {exc}"
        ) from exc
    # A leftover from a run that died before it could clean up: it is not a
    # tree this call may merge into.
    _discard_staging(staging)
    try:
        staging.mkdir()
    except OSError as exc:
        raise ArchiveRefusal(
            PARTIAL_UNPACK, f"cannot stage the unpack at {staging}: {exc}"
        ) from exc
    try:
        written = extract_sandbox_archive(
            Path(archive_path),
            staging,
            max_members=max_members,
            max_seconds=max_seconds,
        )
        _publish_staged_tree(staging, target)
    except BaseException:
        _discard_staging(staging)
        raise
    return written


def _publish_staged_tree(staging: Path, target: Path) -> None:
    """Move a completed staging tree to ``target`` (or merge it there)."""
    if not (target.exists() or target.is_symlink()):
        os.rename(staging, target)
        return
    # Everything the merge would touch is checked *before* anything moves, so
    # a refusal here leaves ``target`` exactly as it was.
    _guard_staged_tree(staging, target)
    _merge_staged_tree(staging, target, [])
    _discard_staging(staging)


def _merge_stopped_after(detail: str, target: Path, merged: list[str]) -> str:
    """Append "how far the merge got" to a refusal's detail (N76).

    The merge into an existing tree is per-entry ``os.replace`` and therefore
    not atomic (see the section comment above): a failure halfway leaves a tree
    that is part old, part new. The operator's next question is *which* part,
    so the refusal answers it -- and says the way out, because every entry is
    an idempotent rename and re-running the same materialization finishes the
    merge rather than doubling it.
    """
    if not merged:
        return (
            f"{detail} (merge into {target}: nothing published; "
            "re-running is free)"
        )
    shown = ", ".join(merged[:8])
    more = "" if len(merged) <= 8 else f", … (+{len(merged) - 8} more)"
    return (
        f"{detail} (merge into {target}: published so far = "
        f"[{shown}{more}]; re-running finishes the merge)"
    )


def _guard_staged_tree(staging: Path, target: Path) -> None:
    """Re-apply the destination guards to every staged entry against ``target``.

    The unpack checked this against the *staging* directory, which it made
    itself and therefore holds no links. What can still be wrong is the tree
    the bytes are about to land in: a link the previous incarnation left at or
    above a member's path is the §4.3.1 refusal, and it must not be skipped
    just because the payload was unpacked somewhere else first.
    """
    safe_parents: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(staging):
        base = Path(dirpath)
        for name in dirnames + filenames:
            entry = base / name
            relative = entry.relative_to(staging).as_posix()
            _guard_member(target, relative, safe_parents)
            if name in dirnames and not entry.is_symlink():
                _guard_directory(target, relative)


def _merge_staged_tree(
    staging: Path, target: Path, merged: list[str], prefix: str = ""
) -> None:
    """Move every entry under ``staging`` into ``target``, merging directories.

    ``target`` is an existing tree: entries the archive names replace what is
    there (``os.replace``, one rename each), directories merge recursively, and
    entries the archive does not name are left alone -- the semantics the
    unpack always had, now with nothing half-written on the way.

    ``merged`` accumulates what this call and its ancestors have already
    published (paths relative to the tree root under ``prefix``), so a failure
    can name where the merge stopped (N76).
    """
    with os.scandir(staging) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        source = staging / name
        destination = target / name
        relative = f"{prefix}{name}"
        try:
            if source.is_dir() and not source.is_symlink():
                if destination.is_symlink():
                    # ``_guard_staged_tree`` already refused a link here; kept
                    # as a second layer rather than trusted alone.
                    raise ArchiveRefusal(
                        DESTINATION_IS_A_SYMLINK,
                        _merge_stopped_after(
                            f"the destination holds a symbolic link at "
                            f"{name!r}: refusing to write through it",
                            target,
                            merged,
                        ),
                    )
                if destination.exists() and not destination.is_dir():
                    raise ArchiveRefusal(
                        PARTIAL_UNPACK,
                        _merge_stopped_after(
                            f"{destination} is not a directory to merge into",
                            target,
                            merged,
                        ),
                    )
                if destination.exists():
                    _merge_staged_tree(source, destination, merged, f"{relative}/")
                    continue
                os.replace(source, destination)
                merged.append(relative)
                continue
            if destination.is_dir() and not destination.is_symlink():
                raise ArchiveRefusal(
                    PARTIAL_UNPACK,
                    _merge_stopped_after(
                        f"{destination} is a directory in the way of {name!r}",
                        target,
                        merged,
                    ),
                )
            os.replace(source, destination)
            merged.append(relative)
        except OSError as exc:
            raise ArchiveRefusal(
                PARTIAL_UNPACK,
                _merge_stopped_after(
                    f"publishing {source} at {destination}: {exc}", target, merged
                ),
            ) from exc


def _discard_staging(staging: Path) -> None:
    """Remove a staging tree (or file/symlink), best effort, and loudly.

    Never raises: its two callers are cleaning a leftover from a previous run
    (which must not stop this one) and unwinding a refusal (whose own exception
    is what the caller has to see). A failure is a named WARNING, because a
    half-unpacked staging tree left on disk is exactly the residue this helper
    exists to keep out of the live tree.
    """
    try:
        if staging.is_symlink() or staging.is_file():
            staging.unlink()
            return
        if staging.is_dir():
            shutil.rmtree(staging)
    except OSError as exc:
        logger.warning(
            "archive-staging-leftover: cannot remove the staging tree %s: %s",
            staging,
            exc,
        )


# ---------------------------------------------------------------------------
# Task 3: moving a whole tree, bounded
# ---------------------------------------------------------------------------
#
# Two operations, one shape: a **tree copies**, either out of a node or into
# one, and the copy's size is the caller's business. The measurements behind
# the numbers are in ``docs/create-local-first-design.md`` §3.0/§3.1:
#
# * the copy used to be unbounded in memory (``resp.content`` on the control
#   plane, ``tar_path.read_bytes()`` on the wire, ``await request.body()`` in
#   the receiving agent), and one 900 MiB restore already put the agent's
#   ``maint`` container at 512.0 MiB / 512 MiB with zero headroom and OOMKilled
#   it once (10/10 runs at the limit);
# * the page cache is *the* memory item: a copy through this code drops each
#   finished window with ``posix_fadvise(POSIX_FADV_DONTNEED)``, the same trick
#   the sequential-write probe uses when it ``fsync``es every 64 MiB.
#
# The configured value is the **single-copy** bound, not a tree inventory
# bound: the node's standing tree budget is already ``E2B_NODE_DISK_MB``, and
# the design doc explicitly rules out inventing a smaller tree cap (there is
# nothing in the capacity account that says 8 GiB of trees per node does not
# fit). What does not fit, and what this refuses by name, is one copy that is
# larger than one sandbox's own disk quota.

#: The named refusal. Callers map it to their own surface (the control plane
#: and the worker answer 413, ``materialize`` answers its own
#: ``tree-too-large``), and the *name* is what an operator greps for.
TREE_COPY_TOO_LARGE = "tree-copy-too-large"

#: 1.25 GiB: the cap has to admit **a full sandbox**, and a full sandbox's
#: archive is bigger than the tree it carries -- one sandbox's default quota is
#: ``E2B_DEFAULT_DISK_MB`` = 1 GiB, and the archive adds tar headers plus
#: gzip framing (and a tree of already-compressed data does not shrink). A cap
#: of exactly the quota would refuse the copy of a sandbox that is merely
#: *full*, which is the one case the cap must not catch (design §3.1).
DEFAULT_TREE_COPY_MAX_BYTES = 1280 * 1024 * 1024

#: How many bytes are copied before the page cache for that stretch is
#: dropped. 64 MiB is the probe's own ``fsync`` window; it bounds the peak at
#: "one window + in-flight dirty pages" instead of "the whole copy × 2.9".
DEFAULT_TREE_COPY_WINDOW_BYTES = 64 * 1024 * 1024


class TreeCopyTooLargeError(Exception):
    """A tree copy crossed its byte-denominated cap; the name is the contract."""

    reason = TREE_COPY_TOO_LARGE

    def __init__(self, limit_bytes: int, written_bytes: int, path: str = "") -> None:
        self.limit_bytes = limit_bytes
        self.written_bytes = written_bytes
        super().__init__(
            f"{TREE_COPY_TOO_LARGE}: {path or 'the copy'} reached "
            f"{written_bytes} bytes, over the {limit_bytes}-byte limit "
            "(E2B_TREE_COPY_MAX_BYTES; 0 disables it)"
        )


def drop_page_cache(fd: int, length: int, *, offset: int = 0) -> None:
    """Best-effort ``posix_fadvise(POSIX_FADV_DONTNEED)`` over one window.

    Best-effort on purpose: not every filesystem/kernel combination supports
    it (and ``os.posix_fadvise`` is missing on some platforms), and failing to
    drop the cache is a memory-pressure problem, never a correctness one.
    """
    if length <= 0:
        return
    advice = getattr(os, "posix_fadvise", None)
    dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
    if advice is None or dontneed is None:  # pragma: no cover - platform gap
        return
    try:
        advice(fd, offset, length, dontneed)
    except OSError:  # pragma: no cover - some filesystems refuse the hint
        pass


class BoundedTreeWriter:
    """A write-only file object for one tree copy: capped, and window-dropped.

    Usable wherever a file object is (``tarfile.open(fileobj=…)``, a gzip
    stream, or the loop that forwards HTTP chunks), so the copy never has to be
    held whole to be measured. ``max_bytes=0`` disables the cap.
    """

    def __init__(
        self,
        fd: int,
        *,
        max_bytes: int = DEFAULT_TREE_COPY_MAX_BYTES,
        window_bytes: int = DEFAULT_TREE_COPY_WINDOW_BYTES,
        path: str = "",
    ) -> None:
        self._fd = fd
        self.max_bytes = int(max_bytes)
        self.window_bytes = max(0, int(window_bytes))
        self.path = path
        self.written = 0
        self._window_start = 0

    def write(self, data) -> int:
        size = len(data)
        if self.max_bytes and self.written + size > self.max_bytes:
            raise TreeCopyTooLargeError(
                self.max_bytes, self.written + size, self.path
            )
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]
        self.written += size
        if self.window_bytes and self.written - self._window_start >= (
            self.window_bytes
        ):
            drop_page_cache(
                self._fd, self.written - self._window_start,
                offset=self._window_start,
            )
            self._window_start = self.written
        return size

    def flush(self) -> None:
        """``GzipFile``/``TarFile`` may flush; the last window is dropped here."""
        drop_page_cache(
            self._fd, self.written - self._window_start, offset=self._window_start
        )
        self._window_start = self.written

    def tell(self) -> int:
        """The bytes written so far.

        ``tarfile`` asks its file object for the offset when it was handed a
        bare file object (rather than a path) and when it closes a ``"w"``
        stream, so a write-only wrapper without ``tell`` cannot take a tar.
        Every caller here writes from offset 0, which is what makes the byte
        count the offset.
        """
        return self.written

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False


def tree_payload_bytes(path: Path) -> int:
    """The size of one snapshot payload, in either shape.

    The tar shape is one ``stat``; the pre-tar ``fs/`` directory is a walk --
    it is the shape whose *copy* also walks, so measuring it the same way costs
    nothing the copy does not already pay.
    """
    source = Path(path)
    if source.is_file():
        return source.stat().st_size
    if source.is_dir():
        total = 0
        for root, _dirs, files in os.walk(source):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:  # pragma: no cover - raced away under us
                    continue
        return total
    return 0


def stage_tree_from_archive(
    archive_path: Path,
    tree_root: Path,
    *,
    max_bytes: int = DEFAULT_TREE_COPY_MAX_BYTES,
    window_bytes: int = DEFAULT_TREE_COPY_WINDOW_BYTES,
) -> tuple[Path, int]:
    """Unpack ``archive_path`` beside ``tree_root`` and return the staging dir.

    Beside, not in ``_migrate``: the staging has to be on the **same
    filesystem** as the tree, because the publish step is a ``rename(2)`` --
    and after the reslice the tree is on the node's own disk while the staging
    area is the shared NAS (``EXDEV``). The name carries a leading dot, so no
    workspace scan can read a half-extracted tree as a sandbox.
    """
    root = Path(tree_root)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.parent / f".{root.name}.importing-{os.urandom(6).hex()}"
    staging.mkdir()
    size = tree_payload_bytes(Path(archive_path))
    if max_bytes and size > max_bytes:
        staging.rmdir()
        raise TreeCopyTooLargeError(max_bytes, size, str(archive_path))
    try:
        extract_sandbox_archive(Path(archive_path), staging)
    except BaseException:
        _rmtree_quietly(staging)
        raise
    return staging, size


def _rmtree_quietly(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def publish_staged_tree(
    staging: Path,
    tree_root: Path,
    *,
    remove_existing=None,
) -> None:
    """Publish a staged tree at ``tree_root`` in one step.

    ``remove_existing`` is the caller's own (audited) removal of the tree that
    is already there -- the agent passes the one that goes through the control
    plane's file-op channel -- and it runs only once the staged tree is
    complete. A failure in either half leaves the old tree as it was and the
    staging directory removed: there is no state in which ``tree_root`` holds
    half of one tree.
    """
    root = Path(tree_root)
    try:
        if remove_existing is not None and (root.exists() or root.is_symlink()):
            remove_existing()
        os.replace(staging, root)
    except BaseException:
        _rmtree_quietly(Path(staging))
        raise
