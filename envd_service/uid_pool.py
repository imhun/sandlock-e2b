"""Per-worker host uid allocation pool (E3.2).

Each sandbox runs inside its own user namespace with the *host* identity set
by ``RunAs`` (S1.2): inside the namespace the process is uid 0 (fake root),
while the host sees the allocated uid. Distinct host uids give kernel-enforced
file / unix-socket isolation even if Landlock were bypassed, which is the
point of ``0770 owner=<sandbox uid> group=<worker gid>`` + independent uid
workspaces (a sandbox gid is never the worker's gid, so the group bit is the
worker's own data-plane access and nothing else's).

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

Cross-process safety (I1): ``acquire`` briefly takes an exclusive ``flock``
on the shared state file ``<state base>/.uid_pool.lock`` while it recomputes
the free set and atomically writes a reservation marker
(``<state base>/.uid_reservations/<sandbox_id>``). Both are *platform* state,
so they follow ``E2B_STATE_BASE`` out from under the tree root (N27); with no
state base configured they are the workspace base, exactly as before. The
marker makes the reservation visible to every other worker sharing the
workspace from the moment the uid is handed out — the acquire→register window
(volume provisioning, recursive chown) can be long, so waiting for the record
would leave a collision window. The caller persists the ``sandbox.json`` record and
calls :meth:`UidPool.commit` to drop the marker, or abandons the allocation
via :meth:`UidPool.release` (failed create / delete), which drops the marker
and frees the uid. ``flock`` serializes the compute+marker-write critical
section across processes; the marker keeps the picked uid out of every other
worker's view until the record is durable.

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
import time
from pathlib import Path
from typing import Any

import fcntl

from envd_service import priv_helpers
from gateway_common import create_trace
from gateway_common.paths import (
    is_sandbox_workspace_dir,
    resolve_state_base,
    sandbox_record_path,
    validate_sandbox_id,
)

logger = logging.getLogger(__name__)

#: Directory (under the **state base**) holding transient cross-process
#: reservation markers written by ``acquire`` and removed by ``commit`` /
#: ``release``. A marker file is named after the sandbox id and contains the
#: reserved uid as decimal text.
_RESERVATION_DIR = ".uid_reservations"

#: Mode of a sandbox-owned directory (workspace root / volume slice):
#: ``0770 owner=<sandbox uid> group=<worker gid>`` (fix round 1 / c1; the
#: long-form rationale lives on :func:`apply_sandbox_ownership`). Re-exported
#: from :mod:`envd_service.priv_helpers` so the shape has one definition.
WORKSPACE_MODE = priv_helpers.WORKSPACE_MODE

#: Legacy shared-uid RunAs identity (S1.2): with ``E2B_PER_SANDBOX_UID`` off
#: a root worker maps every sandbox to host uid/gid 1000 (the same constant
#: ``SandlockExecutor._run_as_identity`` returns; kept in parity by unit
#: tests). A non-root worker degrades to its own identity instead.
LEGACY_SHARED_UID = 1000


#: Kernel capability numbers (capabilities(7)) that the sandbox identity path
#: depends on but that a hardened container may have dropped.
CAP_SYS_PTRACE = 19
#: The pair that lets a *non-root* worker remap a sandbox to another host uid
#: (the file-cap launcher shape). The fork treats a mediator holding them as
#: privileged for exactly this reason, so E2B has to ask the same question
#: before it claims an in-process create will be refused.
CAP_SETGID = 6
CAP_SETUID = 7


def _cap_eff() -> int | None:
    """This process's effective capability mask, or None if unreadable."""
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            try:
                return int(line.split()[1], 16)
            except (IndexError, ValueError):
                return None
    return None


def has_effective_cap(bit: int) -> bool:
    """Whether capability ``bit`` is *effective* here.

    Not the same question as ``geteuid() == 0``: a container can run as root
    with a dropped bounding/effective set (so the uid map path fails), or as a
    non-root user holding one added capability.
    """
    cap_eff = _cap_eff()
    return cap_eff is not None and (cap_eff >> bit) & 1 == 1


class UidPoolError(RuntimeError):
    """Raised when the pool cannot satisfy an allocation."""


def _pool_range(start: int, size: int) -> set[int]:
    return set(range(start, start + size))


def _recorded_uid(
    workspace_base: str | Path, sandbox_id: str, *, state_base: str | Path | None = None
) -> int | None:
    """``host_uid`` persisted for one sandbox, or None.

    Reads the platform's copy (``_runtime/<id>/sandbox.json``), falling back to
    the pre-split in-tree location. This is the *fallback* allocator's input:
    the control plane owns the fleet-wide allocation (OBS-9), and the tree copy
    is the very file a sandbox can rewrite, which is why it is no longer
    consulted first.
    """
    record_path = sandbox_record_path(
        workspace_base, sandbox_id, state_base=state_base
    )
    if not record_path.is_file():
        record_path = sandbox_record_path(workspace_base, sandbox_id, legacy=True)
        if not record_path.is_file():
            return None
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    uid = payload.get("host_uid") if isinstance(payload, dict) else None
    return uid if isinstance(uid, int) else None


def _recorded_uids(
    workspace_base: str | Path,
    start: int,
    size: int,
    *,
    state_base: str | Path | None = None,
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
        # The same shape predicate every other top-level workspace scan uses
        # (quota orphan scan, worker GC): the ``_``/``snap_`` namespaces are
        # separated by content, so a prefixed directory counts as a sandbox
        # tree only when it carries its own record -- which is the only way
        # this loop can contribute a uid at all.
        if not is_sandbox_workspace_dir(entry):
            continue
        uid = _recorded_uid(base, entry.name, state_base=state_base)
        if uid is not None and start <= uid < start + size:
            used.add(uid)
    return used


def _read_uid_marker(marker: Path) -> int | None:
    """The uid stored in a reservation marker, or None when malformed."""
    try:
        uid = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return uid if uid > 0 else None


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """Recursively chown ``path`` (symlinks themselves, never their targets).

    The root shape's step: only root can descend a tenant's tree and change
    ownership back (``0770`` gives the worker group access, but not ownership,
    so it cannot chown it back). Every other shape reaches its hand-over by a
    different route -- the agent performs the chown itself
    (``agent_fileops``), and a non-root worker without one has no privileged
    file-step path at all (open-issues N52).
    """
    os.lchown(path, uid, gid)
    if not path.is_symlink() and path.is_dir():
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                os.lchown(Path(root) / name, uid, gid)
            for name in dirs:
                os.lchown(Path(root) / name, uid, gid)


def apply_sandbox_ownership(
    workspace_dir: str | Path, host_uid: int, *, sandbox_id: str | None = None
) -> None:
    """Chown a sandbox workspace to ``<sandbox uid>:<worker gid>`` at ``0770``.

    Fix round 1 (裁定 c1): the sandbox owns the tree, and the **worker's
    effective gid** -- ``os.getegid()``, never a hardcoded 65534, because a
    k8s pod may set ``runAsGroup`` -- is the group. The worker is the
    data-plane owner of every workspace (files API, watcher, command logs,
    snapshots, lifecycle -- all of which must work while a sandbox is paused,
    frozen or gone), so its access is expressed as a *permission* rather than
    as a per-syscall capability.

    Cross-sandbox isolation is unchanged: a sandbox runs as ``uid X, gid X``
    with ``setgroups([])`` (the slot broker clears the groups; the userns path
    has ``setgroups=deny``), so it is never in the worker's group and the
    ``other`` bits are 0 -- the kernel DAC check is still the isolation
    backstop behind Landlock.

    Every *directory* in the tree gets the same mode (files keep theirs): the
    files API uploads into ``<workspace>/workspace/``, a directory the worker
    itself created as ``0755``, so "``0770`` on the sandbox root" alone would
    leave the worker without write access one level down.

    Order matters: ``chmod`` **before** ``chown``. After the chown the worker
    is no longer the owner and ``chmod`` would fail with EPERM (the broker
    deliberately does not carry ``CAP_FOWNER``); the root shape hides this
    because root ignores ownership, the non-root shape does not.
    """
    path = Path(workspace_dir)
    from envd_service import agent_fileops, priv_helpers

    group = os.getegid()
    mode = priv_helpers.WORKSPACE_MODE
    # The mode pass comes **first**, in every shape: while the worker still owns
    # the tree (``chmod`` after a hand-over the worker did not perform is EPERM),
    # and because ``chown`` does not set modes -- ``e2b-maint``'s verb is an
    # ownership change, nothing else. On a re-create of an existing sandbox the
    # tree is already the sandbox's, i.e. already in this shape, so skipping is
    # correct and keeps the log quiet.
    #
    # ⚠ C3 Task 4 third review (I-1): the agent branch used to return *before*
    # this pass, so a tree handed over by the agent kept whatever ``mkdir`` /
    # ``copytree`` produced (0755) while the worker -- group member, no longer
    # owner -- was left at best ``r-x`` on the tree it must write for the files
    # API, snapshots and lifecycle. The slice path next door
    # (``volumes.provision_sandbox_volume_mount``) does chmod-then-chown in
    # exactly this order; this is the same rule.
    try:
        ours = path.is_dir() and not path.is_symlink() and (
            path.stat().st_uid == os.geteuid()
        )
    except OSError:
        ours = False
    if ours:
        _prepare_directory_modes(path, mode)
    else:
        logger.debug(
            "%s is not owned by this worker; keeping its existing modes", path
        )
    # C3 Task 4: in the agent shape the hand-over is the *agent's* step --
    # ``e2b-maint chown --uid X --gid <worker gid>`` executed by face B, asked
    # for as ``{sandbox_id, op}`` (no path, no uid from this side; hard rules
    # 1/3). It happens *after* the mode pass above, which is why that pass may
    # not be skipped here.
    client = agent_fileops.active()
    if client is not None:
        if sandbox_id is None:
            raise priv_helpers.PrivHelperError(
                "the C3 agent shape needs the sandbox id to hand a tree over: "
                f"{path} was not named by one"
            )
        client.chown_workspace(sandbox_id, recursive=True)
        return
    _chown_tree(path, host_uid, group)


def _prepare_directory_modes(path: Path, mode: int) -> None:
    """Give every directory in a freshly created tree the c1 mode.

    The worker must be able to *write* inside the tree it manages, not only
    read it: the files API uploads into ``<workspace>/workspace/`` (a
    directory the worker itself created, which ``mkdir`` left at 0755), and
    ``0770`` on the sandbox root alone would leave the worker with r-x there.
    So every directory gets ``0770`` -- the group is the worker's gid and the
    sandbox is not in it, so cross-sandbox isolation is unchanged -- while
    **files keep their own modes** (a sandbox-created 0644 file is group/other
    readable, which is what the files API and snapshots need; an overwrite goes
    through the temp-file + rename path, which needs directory write).

    Must run **before** the chown: afterwards the worker is no longer the owner
    and ``chmod`` is EPERM (the broker carries no ``CAP_FOWNER``). Symlinks are
    never touched (``chmod`` on a symlink would follow it on Linux).
    """
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.warning("cannot set %s to %04o: %s", path, mode, exc)
    if path.is_symlink() or not path.is_dir():
        return
    for root, dirs, _files in os.walk(path):
        for name in dirs:
            child = Path(root) / name
            if child.is_symlink():
                continue
            try:
                os.chmod(child, mode)
            except OSError as exc:
                logger.warning("cannot set %s to %04o: %s", child, mode, exc)


def _alignment_target_uid(*, worker_euid: int, owner_uid: int) -> int | None:
    """Shared-uid workspace alignment decision (pure; unit-testable off-Linux).

    Returns the uid a workspace should be chowned to, or ``None`` when no
    ownership change is needed:

    * non-root worker: the workspace was created as the worker's own
      identity, which is also the RunAs identity (S1.2 / E5.1) — and the
      worker could not chown to another uid anyway;
    * root worker, workspace not root-owned: already aligned by someone else
      (e.g. a previous provision or an external storage owner) — never
      fight it.
    """
    if worker_euid != 0 or owner_uid != 0:
        return None
    return LEGACY_SHARED_UID


def align_shared_uid_workspace(workspace_dir: str | Path) -> None:
    """Align a root-created workspace to the legacy shared RunAs uid (FUP #6).

    Pure-sandlock workspaces (no chroot) are written directly by the sandbox
    shell with the host RunAs identity — there is no supervisor mediation
    tier to create files on the shell's behalf (unlike the image-rootfs
    chroot shape). A root worker creates the workspace as root:root, so the
    shared-uid sandbox (host uid 1000) cannot write its own root directory.
    When the worker is root and the workspace is still root-owned, chown the
    whole tree to the shared uid with the same semantics as
    :func:`apply_sandbox_ownership`; anything else is left untouched.

    Shared-volume per-uid isolation is not weakened: this only touches the
    sandbox workspace directory (never shared volume slices) and never
    widens permissions to world-writable.
    """
    if os.geteuid() != 0:
        return
    path = Path(workspace_dir)
    try:
        owner_uid = path.stat().st_uid
    except OSError:
        logger.warning(
            "cannot stat workspace %s for shared-uid ownership alignment",
            path,
        )
        return
    uid = _alignment_target_uid(
        worker_euid=os.geteuid(), owner_uid=owner_uid
    )
    if uid is not None:
        apply_sandbox_ownership(path, uid, sandbox_id=path.name)


class UidPool:
    """Host uid allocation for sandboxes on one worker.

    Correct across processes: allocation recomputes the used set from
    ``sandbox.json`` records **and** reservation markers on disk under a
    cross-process ``flock``, so a separate-process worker sharing the
    workspace never collides with an allocation another worker handed out
    but has not persisted yet. ``acquire`` holds the reservation marker until
    the caller persists the record (:meth:`commit`) or abandons the
    allocation (:meth:`release`). Deployments that share one workspace
    between multiple workers should still use disjoint ``E2B_UID_POOL_START``
    ranges for the other shared state (quota tables, reconcile scans).
    """

    def __init__(
        self,
        workspace_base: str | Path,
        *,
        start: int = 10000,
        size: int = 1000,
        state_base: str | Path | None = None,
    ) -> None:
        if not isinstance(start, int) or start <= 0:
            raise UidPoolError(f"invalid uid pool start: {start!r}")
        if not isinstance(size, int) or size <= 0:
            raise UidPoolError(f"invalid uid pool size: {size!r}")
        self._start = start
        self._size = size
        self._workspace_base = Path(workspace_base)
        #: The base the pool's own files live under: the cross-process lock and
        #: the reservation markers are *platform* state, not the sandbox's, so
        #: they follow ``E2B_STATE_BASE`` out from under the tree root (N27).
        #: The *records* this pool scans for allocated uids are read from the
        #: same base (``sandbox_record_path(..., state_base=...)``), while the
        #: directories it walks stay under the workspace base.
        self._state_base = resolve_state_base(self._workspace_base, state_base)
        self._allocated: set[int] = set()
        self._by_sandbox: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def start(self) -> int:
        return self._start

    @property
    def size(self) -> int:
        return self._size

    @property
    def lock_path(self) -> Path:
        """Cross-process serialization point for the free-set computation."""
        return self._state_base / ".uid_pool.lock"

    def _open_reservation_lock(self) -> int:
        """Open and exclusively flock the shared state file.

        Blocks until any other worker's free-set recompute + marker write
        critical section finishes. The kernel drops the lock if the holder
        dies, so a crashed create cannot wedge the pool.
        """
        # The *workspace* base is ensured on demand (this pool's own scans read
        # it, and yesterday it was also the lock's parent). The state base is
        # not: it belongs to the deployment's init container, which owns its
        # mode and ownership on the shared export -- a worker that created it
        # out of band would leave a platform directory nobody styled. A missing
        # state base therefore fails loudly on the ``open`` below.
        self._workspace_base.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _close_reservation_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _marker_path(self, sandbox_id: str) -> Path:
        return self._state_base / _RESERVATION_DIR / sandbox_id

    def _write_reservation(self, sandbox_id: str, uid: int) -> None:
        """Atomically persist the reservation marker (under the flock)."""
        marker = self._marker_path(sandbox_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.parent / f".{sandbox_id}.tmp"
        tmp.write_text(f"{uid}\n", encoding="utf-8")
        os.replace(tmp, marker)

    def _remove_reservation(self, sandbox_id: str) -> None:
        marker = self._marker_path(sandbox_id)
        try:
            marker.unlink()
        except FileNotFoundError:
            pass

    def _clear_reservations(self) -> None:
        """Drop every reservation marker (startup reconcile only).

        At a clean startup no create is in flight, so every marker is either a
        stale duplicate of a durable record (harmless) or the leftover of a
        create that crashed between ``acquire`` and ``register`` — the same
        class of orphan the reconcile scan reclaims. Reconcile is a
        startup-only scan; the multi-worker concurrent-startup caveat is the
        documented one for orphan reclaim (report §5 / Concerns §3).
        """
        marker_dir = self._state_base / _RESERVATION_DIR
        if not marker_dir.is_dir():
            return
        try:
            entries = list(marker_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_file():
                    entry.unlink()
            except OSError:
                continue

    def _reserved_uids(self) -> set[int]:
        """Pool-range uids held by reservation markers (cross-process)."""
        marker_dir = self._state_base / _RESERVATION_DIR
        reserved: set[int] = set()
        if not marker_dir.is_dir():
            return reserved
        try:
            entries = list(marker_dir.iterdir())
        except OSError:
            return reserved
        for entry in entries:
            if not entry.is_file():
                continue
            uid = _read_uid_marker(entry)
            if (
                uid is not None
                and self._start <= uid < self._start + self._size
            ):
                reserved.add(uid)
        return reserved

    def _scan_records(self) -> tuple[set[int], dict[str, int]]:
        """Pool-range uids referenced by records, plus the sandbox→uid map.

        The reverse map is what lets :meth:`release` free a pre-restart
        sandbox's uid after :meth:`reconcile` rebuilt the in-memory state
        from disk (I2): without it, deleting such a sandbox leaks its uid in
        ``_allocated`` until the next restart.
        """
        referenced: set[int] = set()
        by_sandbox: dict[str, int] = {}
        base = self._workspace_base
        if not base.is_dir():
            return referenced, by_sandbox
        try:
            entries = list(base.iterdir())
        except OSError:
            return referenced, by_sandbox
        for entry in entries:
            # Same filter as ``_recorded_uids``, and for the same reason: the
            # reverse map is built from records, so only a directory that the
            # shared predicate accepts can appear in it.
            if not is_sandbox_workspace_dir(entry):
                continue
            uid = _recorded_uid(base, entry.name, state_base=self._state_base)
            if (
                uid is not None
                and self._start <= uid < self._start + self._size
            ):
                referenced.add(uid)
                by_sandbox[entry.name] = uid
        return referenced, by_sandbox

    def acquire(
        self, sandbox_id: str, *, preferred: int | None = None
    ) -> int:
        """Reserve the next free uid for ``sandbox_id``.

        ``preferred`` (e.g. the uid already persisted for a re-provisioned /
        migrated sandbox) wins when it lies inside the pool range; otherwise
        the lowest free uid is picked. Records on disk and reservation
        markers count as allocated, so a uid is never reused while any other
        sandbox record *or in-flight allocation* references it. The caller
        must persist the record and call :meth:`commit`, or abandon the
        allocation via :meth:`release`, to drop the reservation marker.
        """
        if not validate_sandbox_id(sandbox_id):
            raise UidPoolError(f"invalid sandbox id: {sandbox_id!r}")
        with self._lock:
            fd = self._open_reservation_lock()
            try:
                recorded = _recorded_uid(
                    self._workspace_base, sandbox_id, state_base=self._state_base
                )
                if (
                    recorded is not None
                    and self._start <= recorded < self._start + self._size
                ):
                    uid = recorded
                else:
                    used = _recorded_uids(
                        self._workspace_base,
                        self._start,
                        self._size,
                        state_base=self._state_base,
                    )
                    used.update(self._allocated)
                    used.update(self._reserved_uids())
                    if (
                        preferred is not None
                        and self._start <= preferred
                        < self._start + self._size
                        and preferred not in used
                    ):
                        uid = preferred
                    else:
                        uid = None
                        for candidate in range(
                            self._start, self._start + self._size
                        ):
                            if candidate not in used:
                                uid = candidate
                                break
                        if uid is None:
                            raise UidPoolError(
                                f"uid pool exhausted "
                                f"({self._start}.."
                                f"{self._start + self._size - 1})"
                            )
                self._write_reservation(sandbox_id, uid)
                self._allocated.add(uid)
                self._by_sandbox[sandbox_id] = uid
                return uid
            finally:
                self._close_reservation_lock(fd)

    def claim(self, sandbox_id: str, uid: int) -> int:
        """Adopt a uid the **control plane** allocated (OBS-9).

        The authority for fleet-wide uid allocation moved out of the shared
        volume: ``sandbox.json`` inside a sandbox tree is writable by any root
        on any mounting node, so a uid sourced from there could be forged --
        and forging one is enough to make two sandboxes share a uid and remove
        the cross-uid isolation wall. The control plane now allocates in its
        shared store and passes the uid down with the create; this method just
        records it locally so the *fallback* allocator
        (:meth:`acquire`, used when no uid arrives) cannot hand out the same
        value.

        Raises :class:`UidPoolError` when the uid is outside the pool range --
        a control plane and a worker that disagree about the range would
        otherwise silently put two sandboxes on one uid.
        """
        if not validate_sandbox_id(sandbox_id):
            raise UidPoolError(f"invalid sandbox id: {sandbox_id!r}")
        if not (self._start <= uid < self._start + self._size):
            raise UidPoolError(
                f"uid {uid} is outside this worker's pool "
                f"[{self._start}, {self._start + self._size})"
            )
        with self._lock:
            self._allocated.add(uid)
        return uid

    def commit(self, sandbox_id: str) -> None:
        """Drop the reservation marker once the record is durable.

        Call after the ``sandbox.json`` record referencing the uid has been
        persisted: the record now pins the uid for every worker, so the
        transient marker is no longer needed. If the record is not on disk
        (persist failed), the marker is kept so another worker cannot reuse
        the uid — fail-safe. Idempotent.

        A uid the **control plane** allocated leaves no marker behind
        (:meth:`claim` only records the value in memory), so the common
        deployment path has nothing to drop here. Asking the disk whether the
        record exists before checking for a marker made every create pay for
        one NFS read plus one negative ``unlink`` to learn that -- on the
        deployment's NAS that was 5-25 ms of the create (2026-10-01). The
        marker is now checked first: no marker, nothing to commit.
        """
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            started = time.monotonic()
            if not self._marker_path(sandbox_id).exists():
                return
            if (
                _recorded_uid(
                    self._workspace_base, sandbox_id, state_base=self._state_base
                )
                is None
            ):
                return
            self._remove_reservation(sandbox_id)
        create_trace.stage("commit", sandbox_id, started)

    def release(self, sandbox_id: str) -> None:
        """Return the sandbox's uid to the pool (idempotent).

        Also abandons a still-held reservation marker (failed create path),
        so a uid picked by a create that never persisted a record is free
        again instead of leaking a pool slot.
        """
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            self._remove_reservation(sandbox_id)
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
        outside the pool — are never touched. Reservation markers (in-flight
        allocations that never became durable) are dropped, and the
        sandbox→uid reverse map is rebuilt from disk records so deleting a
        pre-restart sandbox releases its uid (I2).

        Returns ``{"referenced", "reclaimed", "cleaned", "skipped"}``.
        """
        with self._lock:
            fd = self._open_reservation_lock()
            try:
                return self._reconcile_locked()
            finally:
                self._close_reservation_lock(fd)

    def _reconcile_locked(self) -> dict[str, Any]:
        """Reconcile body; caller holds ``self._lock`` and the flock."""
        referenced, by_sandbox = self._scan_records()
        self._allocated = set(referenced)
        self._by_sandbox = by_sandbox
        self._clear_reservations()
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
            # This is the one scan where the criterion decides a side effect,
            # so it is the shared predicate *and* the two conditions below:
            # a prefixed (``_``/``snap_``) real directory without a top-level
            # record is an infrastructure namespace or a snapshot store, not
            # an orphan sandbox tree, and reclaiming the uid it happens to
            # carry -- then recursively chowning the tree to the worker --
            # would hand a foreign tree's ownership to the next allocation.
            # The record check stays load-bearing for every other name: a
            # recorded tree is allocated (or a crashed create), never an
            # orphan.
            if not is_sandbox_workspace_dir(entry):
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
