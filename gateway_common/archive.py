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

import os
import shutil
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
#: The archive carries more members than the unpack may index (a pathological
#: input, not a corrupt one: the member *index* is what grows with the count).
TOO_MANY_MEMBERS = "archive-too-many-members"

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
    archive_path: Path, dest: Path, *, max_members: int | None = None
) -> int:
    """Unpack ``archive_path`` into the existing directory ``dest``.

    Returns the number of members written. Raises :class:`ArchiveRefusal` (and
    never a bare ``tarfile``/``OSError``) so both callers translate one shape.

    ``max_members`` defaults to :func:`resolve_member_max` (``None`` means "read
    the environment"), so the cap is on for every caller that passes nothing;
    ``0`` disables it.
    """
    limit = resolve_member_max() if max_members is None else max_members
    destination = Path(dest)
    resolved_dest = destination.resolve()
    if not resolved_dest.is_dir():
        raise ArchiveRefusal(
            PARTIAL_UNPACK, f"the destination {destination} is not a directory"
        )
    written = 0
    seen = 0
    safe_parents: set[str] = set()
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
