"""Per-worker host uid allocation pool (E3.2).

Each sandbox runs inside its own user namespace with the *host* identity set
by ``RunAs`` (S1.2): inside the namespace the process is uid 0 (fake root),
while the host sees the allocated uid. Distinct host uids give kernel-enforced
file / unix-socket isolation even if Landlock were bypassed, which is the
point of ``0700`` + independent uid workspaces.

The pool hands out uids from ``[start, start + size)`` (``10000+i`` by
default, far away from image-internal uids such as 1000). The allocated uid
is persisted in each ``sandbox.json`` (``host_uid``), so the allocation
survives worker restarts and separate-process deployments: the free set is
always recomputed from the records on disk. Deletion releases the uid through
the runtime registry's unregister callback. Startup reconciliation scans for
orphan uids — pool-range uids that own a workspace directory with no
``sandbox.json`` record (e.g. a create that crashed between chown and
persist) — reclaims them and chowns the stale directories away from the pool
so a later allocation cannot inherit foreign files.

Independent per-sandbox uids require a privileged (root) supervisor: a
non-root supervisor cannot map an arbitrary host uid (S1.2 fail-closed
contract), so allocation / ownership changes only happen when the worker
runs as root. Non-root workers keep the fixed-uid + Landlock model
(``E2B_PER_SANDBOX_UID`` degrades to the worker identity).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from gateway_common.paths import validate_sandbox_id

logger = logging.getLogger(__name__)


class UidPoolError(RuntimeError):
    """Raised when the pool cannot satisfy an allocation."""


def _pool_range(start: int, size: int) -> set[int]:
    return set(range(start, start + size))


def _recorded_uid(workspace_base: str | Path, sandbox_id: str) -> int | None:
    """``host_uid`` persisted for one sandbox, or None."""
    record_path = Path(workspace_base) / sandbox_id / "sandbox.json"
    if not record_path.is_file():
        return None
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    uid = payload.get("host_uid") if isinstance(payload, dict) else None
    return uid if isinstance(uid, int) else None


def _recorded_uids(
    workspace_base: str | Path, start: int, size: int
) -> set[int]:
    """Every pool-range ``host_uid`` referenced by a ``sandbox.json`` record.

    Records are the source of truth for "allocated": a uid referenced by any
    record (on any worker sharing the workspace) is never handed out again,
    and reconcile never treats it as an orphan.
    """
    base = Path(workspace_base)
    used: set[int] = set()
    if not base.is_dir():
        return used
    try:
        entries = list(base.iterdir())
    except OSError:
        return used
    for entry in entries:
        if not entry.is_dir() or not validate_sandbox_id(entry.name):
            continue
        uid = _recorded_uid(base, entry.name)
        if uid is not None and start <= uid < start + size:
            used.add(uid)
    return used


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """Recursively chown ``path`` (symlinks themselves, never their targets)."""
    os.lchown(path, uid, gid)
    if not path.is_symlink() and path.is_dir():
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                os.lchown(Path(root) / name, uid, gid)
            for name in dirs:
                os.lchown(Path(root) / name, uid, gid)


def apply_sandbox_ownership(workspace_dir: str | Path, host_uid: int) -> None:
    """Chown a sandbox workspace to its host uid and tighten it to 0700.

    The sandbox's mount view (``/workspace`` in image-rootfs mode, or the
    workspace directory itself) is owned by the host uid, so inside the
    sandbox (uid 0) writes land with the sandbox's host identity, and other
    sandboxes (different host uids) cannot even enter the directory — the
    kernel DAC check is the isolation backstop behind Landlock.
    """
    path = Path(workspace_dir)
    _chown_tree(path, host_uid, host_uid)
    os.chmod(path, 0o700)


class UidPool:
    """Host uid allocation for sandboxes on one worker.

    The pool is advisory across processes: allocation recomputes the used set
    from ``sandbox.json`` records on disk, so a separate-process worker
    sharing the workspace never collides with records persisted by another
    process. Deployments that share one workspace between multiple workers
    must configure disjoint ``E2B_UID_POOL_START`` ranges per worker.
    """

    def __init__(
        self,
        *,
        start: int = 10000,
        size: int = 1000,
        workspace_base: str | Path,
    ) -> None:
        if not isinstance(start, int) or start <= 0:
            raise UidPoolError(f"invalid uid pool start: {start!r}")
        if not isinstance(size, int) or size <= 0:
            raise UidPoolError(f"invalid uid pool size: {size!r}")
        self._start = start
        self._size = size
        self._workspace_base = Path(workspace_base)
        self._allocated: set[int] = set()
        self._by_sandbox: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def start(self) -> int:
        return self._start

    @property
    def size(self) -> int:
        return self._size

    def acquire(
        self, sandbox_id: str, *, preferred: int | None = None
    ) -> int:
        """Reserve the next free uid for ``sandbox_id``.

        ``preferred`` (e.g. the uid already persisted for a re-provisioned /
        migrated sandbox) wins when it lies inside the pool range; otherwise
        the lowest free uid is picked. Records on disk count as allocated, so
        a uid is never reused while any sandbox record references it.
        """
        if not validate_sandbox_id(sandbox_id):
            raise UidPoolError(f"invalid sandbox id: {sandbox_id!r}")
        with self._lock:
            recorded = _recorded_uid(self._workspace_base, sandbox_id)
            if (
                recorded is not None
                and self._start <= recorded < self._start + self._size
            ):
                self._allocated.add(recorded)
                self._by_sandbox[sandbox_id] = recorded
                return recorded
            if (
                preferred is not None
                and self._start <= preferred < self._start + self._size
                and preferred not in self._allocated
            ):
                self._allocated.add(preferred)
                self._by_sandbox[sandbox_id] = preferred
                return preferred
            used = _recorded_uids(
                self._workspace_base, self._start, self._size
            )
            used.update(self._allocated)
            for uid in range(self._start, self._start + self._size):
                if uid not in used:
                    self._allocated.add(uid)
                    self._by_sandbox[sandbox_id] = uid
                    return uid
            raise UidPoolError(
                f"uid pool exhausted "
                f"({self._start}..{self._start + self._size - 1})"
            )

    def release(self, sandbox_id: str) -> None:
        """Return the sandbox's uid to the pool (idempotent)."""
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            uid = self._by_sandbox.pop(sandbox_id, None)
            if uid is not None:
                self._allocated.discard(uid)

    def allocated_uids(self) -> set[int]:
        with self._lock:
            return set(self._allocated)

    def reconcile(self) -> dict[str, Any]:
        """Startup reconciliation: rebuild the in-memory used set and reclaim
        orphan pool uids.

        An orphan uid is a pool-range uid that owns a top-level workspace
        directory with no ``sandbox.json`` record (create crashed between
        chown and persist, or delete crashed between unregister and rmtree).
        The uid is reclaimed (available for allocation again) and the stale
        directory is chowned away from the pool to the worker identity so a
        later allocation never inherits foreign files. Directories are never
        deleted. Uids referenced by records — and directories owned by uids
        outside the pool — are never touched.

        Returns ``{"referenced", "reclaimed", "cleaned", "skipped"}``.
        """
        with self._lock:
            referenced = _recorded_uids(
                self._workspace_base, self._start, self._size
            )
            self._allocated = set(referenced)
            self._by_sandbox = {}
            pool = _pool_range(self._start, self._size)
            cleaned: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            reclaimed: set[int] = set()
            base = self._workspace_base
            if not base.is_dir():
                return {
                    "referenced": sorted(referenced),
                    "reclaimed": [],
                    "cleaned": [],
                    "skipped": [],
                }
            try:
                entries = list(base.iterdir())
            except OSError as exc:
                logger.warning(
                    "uid reconcile cannot scan %s: %s", base, exc
                )
                return {
                    "referenced": sorted(referenced),
                    "reclaimed": [],
                    "cleaned": [],
                    "skipped": [{"reason": f"scan failed: {exc}"}],
                }
            for entry in entries:
                if not entry.is_dir() or not validate_sandbox_id(entry.name):
                    continue
                if (entry / "sandbox.json").is_file():
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_uid not in pool or st.st_uid in referenced:
                    continue
                reclaimed.add(st.st_uid)
                try:
                    _chown_tree(entry, os.geteuid(), os.getegid())
                except OSError as exc:
                    skipped.append(
                        {
                            "uid": st.st_uid,
                            "path": str(entry),
                            "reason": str(exc),
                        }
                    )
                    logger.warning(
                        "orphan uid %s cleanup failed for %s: %s",
                        st.st_uid,
                        entry,
                        exc,
                    )
                    continue
                cleaned.append({"uid": st.st_uid, "path": str(entry)})
                logger.info(
                    "cleaned orphan uid %s (stale workspace %s)",
                    st.st_uid,
                    entry,
                )
            return {
                "referenced": sorted(referenced),
                "reclaimed": sorted(reclaimed),
                "cleaned": cleaned,
                "skipped": skipped,
            }
