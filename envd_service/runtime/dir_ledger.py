"""Per-directory workspace accounting, kept current by the mediator (N25/L2c).

The whole-tree walk answers "how big is this sandbox's workspace" in a cost
that scales with the tree (measured: ~2.4 ms per directory, ~3.5 us per file).
That is cheap enough to run every few seconds for a small fleet, and not cheap
enough to run for a large one -- so this ledger keeps the answer *and* the
per-directory breakdown, and updates only what the mediator saw change.

The two numbers must agree exactly. `DirLedger.total_bytes` is the same
quantity as `priv_helpers.dir_size` -- the sum of the size of every
non-directory entry `os.walk` yields -- and the contract is byte equality, not
approximation: an accounting that drifts is worse than a slow one, because
nothing notices. `tests/unit/test_dir_ledger.py` pins that equality against the
full walk over random mutation sequences.

That size comes from :func:`envd_service.runtime.brief_stat.entry_size`, not
from `os.path.getsize`, and the difference is the whole point of this file's
cost model: on NFS, `stat` of a file whose dirty pages are still in this
client's page cache flushes them to the server first (`nfs_getattr`) and waits
-- measured at 1405 ms for a file being written at speed, against 0.01 ms for
the same number asked with `statx(STATX_SIZE)`. A ledger that wants to answer
"what has this workspace grown to" every second cannot afford to ask in the
form that also says "and please finish writing it".

Two properties of the split matter to the reader:

* **A dirty mark names the directory that *contains* the change**, so one
  changed file costs one directory scan. A mark that names an ancestor of a
  large subtree (a rename at the tree root, a `rm -rf` of a whole branch) costs
  that subtree -- correctness first, and those are the rare shapes.
* **A directory that is still growing stays dirty.** The mediator only sees
  *path* syscalls, so a process that holds a file descriptor open and keeps
  appending (a log) marks its directory once, at the `open`. Re-checking every
  directory whose subtree grew closes that gap for as long as the growth
  continues; the periodic full walk in the caller is what catches the rest.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from envd_service.runtime.brief_stat import directory_cost, entry_size

logger = logging.getLogger(__name__)


class DirLedgerUnknown(Exception):
    """A directory could not be read, so the ledger cannot be trusted.

    Raised instead of guessing: the caller falls back to a whole-tree walk for
    the number *and* marks the ledger unusable, so the next round rebuilds it
    rather than continuing from a baseline with a hole in it.
    """


class SubtreeScan(NamedTuple):
    """One subtree walk: the bytes per directory, and the files per directory.

    The second number exists because the first one cannot see a tree that
    grows by *names*: an empty file contributes zero bytes, and directories
    contribute nothing at all, so a sandbox can spend the volume's inodes
    without moving the byte ledger (N31 -- measured on the cluster: 2000 empty
    files, platform number unchanged at 0 bytes, while the directory itself
    was 16384 bytes on NFS and is not counted either).
    """

    bytes_by_dir: dict[str, int]
    files_by_dir: dict[str, int]

    @property
    def bytes(self) -> int:
        return sum(self.bytes_by_dir.values())

    @property
    def files(self) -> int:
        return sum(self.files_by_dir.values())


def scan_subtree(root: Path, rel: str) -> SubtreeScan:
    """``{relative directory: the bytes it owns}`` under ``root/rel``.

    ``rel`` itself is always present (possibly with 0 bytes), so a caller
    replacing a subtree has something to subtract even for a directory that
    became empty.

    A directory's entry is its **allocated size** plus the sizes of the files
    it contains: N31's fix 2.  The file-only number could not see a tree that
    grows by names -- 2000 empty entries moved it by 0 -- and the platform
    number is what the quota is decided on, so the directories count now.
    The term is ``st_blocks x 512`` rather than ``st_size``: measured on the
    cluster's NAS (2026-09-21) a directory's ``st_size`` was 4096 empty and
    16384 at 2000 entries while its allocation and ``du -s`` stayed at **512**
    the whole way, so `st_size` would have moved the platform's number away
    from the sandbox's own ``du`` (`brief_stat.directory_cost` carries the
    numbers). Symlinks are counted by their target's size and never followed
    as directories -- exactly what ``os.walk`` plus the same probes do in
    ``priv_helpers.dir_size``, which is the number this must match byte for
    byte.
    """

    start = root / rel if rel else root
    found: dict[str, int] = {rel: 0}
    counts: dict[str, int] = {rel: 0}

    def _raise(exc: OSError) -> None:
        raise exc

    try:
        for dirpath, _dirs, files in os.walk(start, onerror=_raise):
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir == ".":
                rel_dir = ""
            # The directory's own allocation first (N31 fix 2), then its files.
            try:
                owned = directory_cost(dirpath)
            except OSError:
                owned = 0
            counts[rel_dir] = len(files)
            for name in files:
                try:
                    owned += entry_size(os.path.join(dirpath, name))
                except OSError:
                    continue
            found[rel_dir] = owned
    except OSError as exc:
        raise DirLedgerUnknown(f"cannot walk {start}: {exc}") from exc
    return SubtreeScan(found, counts)


class DirLedger:
    """The workspace size, kept as a per-directory breakdown."""

    def __init__(
        self,
        root: str | Path,
        *,
        grace_s: float = 120.0,
        clock=time.monotonic,
    ) -> None:
        self._root = Path(root)
        self._dirs: dict[str, int] = {}
        self._total = 0
        #: Files per directory, the parallel to `_dirs`: `len(_dirs)` counts
        #: the directories themselves, so entries = files + directories.
        self._files: dict[str, int] = {}
        self._entries = 0
        self._ready = False
        self._clock = clock
        #: N25/L2c: directories to re-check until their deadline. A mediator
        #: mark says "written since the last drain", but a writer that holds a
        #: descriptor open and comes back later emits no further path syscall
        #: (measured on the cluster: a file opened, written, slept 12 s, written
        #: again, and the second write was invisible). Re-checking a directory
        #: for a while after it was written is what covers that, and it is
        #: bounded: the set is what was written recently, and each re-check is
        #: one `scandir` of that directory.
        self._grace_s = grace_s
        self._recheck: dict[str, float] = {}
        #: When the last whole-tree rebuild ran, so the caller can bound how
        #: long the incremental answer may go without a backstop.
        self._rebuilt_at = clock()
        self._full_walks = 0

    @property
    def ready(self) -> bool:
        """Whether a baseline exists (a ledger that is not ready has no total)."""
        return self._ready

    @property
    def root(self) -> Path:
        """The tree this ledger accounts for (marks are relative to it)."""
        return self._root

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def total_entries(self) -> int:
        """Files plus directories: what a `stat` would call a name.

        The number the entry cap is about -- not the bytes, which stay at zero
        for empty files and never see directories at all (N31).
        """
        return self._entries if self._ready else 0

    @property
    def directory_count(self) -> int:
        return len(self._dirs)

    @property
    def seconds_since_rebuild(self) -> float:
        return self._clock() - self._rebuilt_at

    @property
    def full_walks(self) -> int:
        return self._full_walks

    def invalidate(self) -> None:
        """Forget the baseline: the next update must be a full rebuild."""
        self._ready = False
        self._recheck.clear()

    def rebuild(self) -> int:
        """Recompute everything from one whole-tree walk."""
        scan = scan_subtree(self._root, "")
        self._dirs = scan.bytes_by_dir
        self._files = scan.files_by_dir
        self._total = sum(self._dirs.values())
        self._entries = sum(self._files.values()) + len(self._dirs)
        self._recheck.clear()
        self._ready = True
        self._rebuilt_at = self._clock()
        self._full_walks += 1
        return self._total

    def rescan_next(self, host_dirs: Iterable[str | os.PathLike]) -> None:
        """Re-check these directories for the grace window.

        For the windows a rebuild cannot cover by itself: a writer whose path
        syscall landed before the drain and whose bytes landed after the walk
        had passed that directory, and a writer that comes back to a file it
        opened earlier.
        """
        deadline = self._clock() + self._grace_s
        for rel in self._shallow_targets(host_dirs):
            self._recheck[rel] = max(self._recheck.get(rel, 0.0), deadline)

    def apply(self, host_dirs: Iterable[str | os.PathLike]) -> int:
        """Re-scan the subtrees the mediator reported (plus any still recent).

        ``host_dirs`` are absolute host paths, as the mediator records them; a
        path outside this ledger's root is ignored (another sandbox's tree, or
        a writable mount the caller does not account for) rather than refused,
        because the ledger's job is to be right about *its own* tree.

        Raises :class:`DirLedgerUnknown` when any subtree cannot be read; the
        caller decides what to do about the number, and this ledger is marked
        unusable so it rebuilds next time.
        """
        now = self._clock()
        targets = self._shallow_targets(host_dirs)
        # A directory that was just written is re-checked for the grace window,
        # whatever the mediator says next round.
        for rel in targets:
            self._recheck[rel] = max(self._recheck.get(rel, 0.0), now + self._grace_s)
        targets |= {rel for rel, until in self._recheck.items() if until > now}

        # Rescan first, then swap: a failure part-way must not leave half the
        # tree replaced (the exception propagates before any mutation).
        scanned: list[tuple[str, SubtreeScan]] = []
        for rel in sorted(targets):
            fresh = scan_subtree(self._root, rel)
            if fresh.bytes > self._subtree_bytes(rel):
                # Still growing: keep watching it past the mark's own window,
                # so a writer that keeps appending is never caught mid-flight.
                self._recheck[rel] = now + self._grace_s
            scanned.append((rel, fresh))

        for rel, fresh in scanned:
            self._replace(rel, fresh)
        self._recheck = {
            rel: until for rel, until in self._recheck.items() if until > now
        }
        self._total = sum(self._dirs.values())
        self._entries = sum(self._files.values()) + len(self._dirs)
        self._ready = True
        return self._total

    @property
    def recheck_count(self) -> int:
        """How many directories are being re-checked (for the operator)."""
        return len(self._recheck)

    # -- internals ---------------------------------------------------------

    def _shallow_targets(self, host_dirs: Iterable[str | os.PathLike]) -> set[str]:
        """The reported directories, reduced to the shallowest and made relative.

        Reducing matters for correctness *and* cost: a mark set that carries
        both a directory and its parent (a `rename` marks both ends, a
        `rm -rf` marks every level) must be scanned once, at the shallowest
        directory, or the replacement below would be applied twice over the
        same bytes.
        """
        candidates: set[str] = set()
        root = os.fspath(self._root)
        for host in host_dirs:
            path = os.fspath(host)
            if path == root:
                candidates.add("")
                continue
            prefix = root.rstrip("/") + "/"
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix) :]
            # A mark that is not itself a directory (a file path, or a name
            # that is already gone) belongs to its parent -- the same rule the
            # mediator applies when it records a write. Normalising here keeps
            # the ledger correct for any producer instead of refusing.
            while rel and not os.path.isdir(self._root / rel):
                parent = os.path.dirname(rel)
                if parent == rel:
                    break
                rel = parent
            candidates.add(rel)

        out: set[str] = set()
        for rel in sorted(candidates, key=lambda r: (r.count("/"), r)):
            if any(rel == kept or rel.startswith(kept + "/") for kept in out):
                continue
            out.add(rel)
        return out

    def _subtree_bytes(self, rel: str) -> int:
        """What this subtree contributes *now* (before the replacement)."""
        if rel == "":
            # The empty relative path is the root: its subtree is everything.
            return self._total if self._ready else sum(self._dirs.values())
        prefix = rel + "/"
        return sum(
            size
            for key, size in self._dirs.items()
            if key == rel or key.startswith(prefix)
        )

    def _replace(self, rel: str, fresh: SubtreeScan) -> None:
        """Swap one subtree's entries for the freshly scanned ones."""
        if rel == "":
            self._dirs = dict(fresh.bytes_by_dir)
            self._files = dict(fresh.files_by_dir)
            return
        prefix = rel + "/"
        for key in [
            key
            for key in self._dirs
            if key == rel or key.startswith(prefix)
        ]:
            del self._dirs[key]
            self._files.pop(key, None)
        self._dirs.update(fresh.bytes_by_dir)
        self._files.update(fresh.files_by_dir)
