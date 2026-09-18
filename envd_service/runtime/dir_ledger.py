"""Per-directory workspace accounting, kept current by the mediator (N25/L2c).

The whole-tree walk answers "how big is this sandbox's workspace" in a cost
that scales with the tree (measured: ~2.4 ms per directory, ~3.5 us per file).
That is cheap enough to run every few seconds for a small fleet, and not cheap
enough to run for a large one -- so this ledger keeps the answer *and* the
per-directory breakdown, and updates only what the mediator saw change.

The two numbers must agree exactly. `DirLedger.total_bytes` is the same
quantity as `priv_helpers.dir_size` -- the sum of `os.path.getsize` over every
non-directory entry `os.walk` yields -- and the contract is byte equality, not
approximation: an accounting that drifts is worse than a slow one, because
nothing notices. `tests/unit/test_dir_ledger.py` pins that equality against the
full walk over random mutation sequences.

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

logger = logging.getLogger(__name__)


class DirLedgerUnknown(Exception):
    """A directory could not be read, so the ledger cannot be trusted.

    Raised instead of guessing: the caller falls back to a whole-tree walk for
    the number *and* marks the ledger unusable, so the next round rebuilds it
    rather than continuing from a baseline with a hole in it.
    """


def scan_subtree(root: Path, rel: str) -> dict[str, int]:
    """``{relative directory: own non-directory bytes}`` under ``root/rel``.

    ``rel`` itself is always present (possibly with 0 bytes), so a caller
    replacing a subtree has something to subtract even for a directory that
    became empty. Symlinks are counted by their target's size and never
    followed as directories -- exactly what ``os.walk`` + ``getsize`` does in
    ``priv_helpers.dir_size``, which is the number this must match.
    """

    start = root / rel if rel else root
    found: dict[str, int] = {rel: 0}

    def _raise(exc: OSError) -> None:
        raise exc

    try:
        for dirpath, _dirs, files in os.walk(start, onerror=_raise):
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir == ".":
                rel_dir = ""
            owned = 0
            for name in files:
                try:
                    owned += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
            found[rel_dir] = owned
    except OSError as exc:
        raise DirLedgerUnknown(f"cannot walk {start}: {exc}") from exc
    return found


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
    def total_bytes(self) -> int:
        return self._total

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
        self._dirs = scan_subtree(self._root, "")
        self._total = sum(self._dirs.values())
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
        scanned: list[tuple[str, dict[str, int]]] = []
        for rel in sorted(targets):
            fresh = scan_subtree(self._root, rel)
            if sum(fresh.values()) > self._subtree_bytes(rel):
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

    def _replace(self, rel: str, fresh: dict[str, int]) -> None:
        """Swap one subtree's entries for the freshly scanned ones."""
        if rel == "":
            self._dirs = dict(fresh)
            return
        prefix = rel + "/"
        for key in [
            key
            for key in self._dirs
            if key == rel or key.startswith(prefix)
        ]:
            del self._dirs[key]
        self._dirs.update(fresh)
