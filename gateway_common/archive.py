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
agent ~0.9 GB of index. Bounding *that* is a named follow-up (a member-count cap
belongs beside Task 3's byte cap); what is asserted here today is only the data
path.
"""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import PurePosixPath
from pathlib import Path

#: A member's *name* walks out of the destination (``..`` or an absolute path).
MEMBER_ESCAPES = "archive-member-escapes"
#: A clean member name that still lands outside: the destination holds a link.
DESTINATION_IS_A_SYMLINK = "destination-is-a-symlink"
#: The extraction started and did not finish (a truncation, an I/O error).
ARCHIVE_IS_CORRUPT = "archive-is-corrupt"
#: The caller's destination is not a directory to unpack into.
PARTIAL_UNPACK = "partial-unpack"

class ArchiveRefusal(Exception):
    """A named, fail-closed refusal from one archive extraction."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


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


def extract_sandbox_archive(archive_path: Path, dest: Path) -> int:
    """Unpack ``archive_path`` into the existing directory ``dest``.

    Returns the number of members written. Raises :class:`ArchiveRefusal` (and
    never a bare ``tarfile``/``OSError``) so both callers translate one shape.
    """
    destination = Path(dest)
    resolved_dest = destination.resolve()
    if not resolved_dest.is_dir():
        raise ArchiveRefusal(
            PARTIAL_UNPACK, f"the destination {destination} is not a directory"
        )
    written = 0
    safe_parents: set[str] = set()
    try:
        with tarfile.open(Path(archive_path), "r:*") as tar:
            for member in tar:
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
