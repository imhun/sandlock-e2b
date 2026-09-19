"""Sandbox runtime registry.

The control plane registers each sandbox (workspace directory, access token,
env vars, image, policy params). Records are persisted as
``<base>/_runtime/<id>/sandbox.json`` -- *next to* the sandbox's tree, never
inside it, so the envd service (a separate process sharing the workspace
volume) can read them while the sandbox itself cannot: it owns its tree
directory and could otherwise unlink and rewrite its own record.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gateway_common.paths import (
    sandbox_record_path,
    sandbox_runtime_dir,
    validate_sandbox_id,
)
from envd_service.runtime.dir_ledger import DirLedger, DirLedgerUnknown

logger = logging.getLogger(__name__)


@dataclass
class RuntimeSandbox:
    sandbox_id: str
    access_token: str
    workspace_dir: str
    #: Wall-clock time this runtime was registered on the worker (E6.1).
    #: Used by node-agent reconciliation to distinguish runtimes that
    #: already existed when the control-plane snapshot was taken from
    #: runtimes created concurrently during the reconcile window: anything
    #: registered after the snapshot request started is a live create and
    #: must never be treated as an orphan.
    created_at: float = field(default_factory=lambda: time.time())
    env_vars: dict[str, str] = field(default_factory=dict)
    base_image: str | None = None
    #: Host uid allocated from the worker uid pool (E3.2). The sandbox runs
    #: as uid 0 inside its user namespace while the host sees this uid, so
    #: distinct sandboxes get kernel-enforced file isolation. ``None`` =
    #: legacy shared-uid mode (fixed uid + Landlock).
    host_uid: int | None = None
    memory_mb: int = 1024
    cpu_percent: int = 100
    disk_mb: int = 1024
    project_id: int | None = None
    max_processes: int = 256
    max_open_files: int = 4096
    allow_internet_access: bool = False
    max_command_timeout: int = 3600
    state: str = "running"
    #: Why the platform put this sandbox out of ``running`` (N28/D). Set with
    #: the state and cleared on resume, so a refusal can say *why* the sandbox
    #: is paused -- "the platform paused you" and "you paused yourself" are the
    #: same state but not the same thing to a caller. In-memory only: it is
    #: pushed with the state and never read from disk, because a stale reason
    #: outliving the pause that produced it would be worse than none.
    pause_reason: str | None = None
    #: One entry per mounted volume: ``{"path", "hostPath",
    #: "perSandboxQuotaMb"}``. The quota rides along because the single-file
    #: ceiling (N28/C) has to be at least as large as the biggest budget the
    #: sandbox was sold, and a volume slice is a budget of its own.
    volume_mounts: list[dict[str, Any]] = field(default_factory=list)
    #: Per-sandbox volume quota state (E2.5): one entry per quota-limited
    #: mount — ``{"volume_id", "sandbox_id", "mount_path", "sandbox_dir",
    #: "projid"}``. Persisted so deletion and migration re-provision can
    #: release / reuse the exact project id.
    volume_projects: list[dict[str, Any]] = field(default_factory=list)
    mcp: dict | None = None
    network: dict | None = None
    allow_public_traffic: bool = False
    iam_tokens: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuntimeSandbox":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in payload.items() if k in known})


def _env_seconds(name: str, default: float) -> float:
    """A worker-side duration knob in seconds (``<= 0`` disables it)."""
    from gateway_common.env import env_float

    return env_float(name, default)


def max_file_size_mb(record: RuntimeSandbox) -> int | None:
    """The single-file ceiling for ``record`` (``RLIMIT_FSIZE``), or ``None``.

    The ceiling is the *largest* budget the sandbox was sold -- its tree and
    every mounted volume slice -- because a limit below a legal budget would
    refuse a write the sandbox is allowed to make, and a hard limit that
    refuses legal work is worse than no limit at all.

    ``None`` (inherit the system limit) whenever any of those budgets is
    unbounded: a recorded ``diskMB <= 0`` means no tree budget, and a mount
    with ``perSandboxQuotaMb == 0`` means that slice is unlimited (see
    ``build_volume_mounts``). With one unbounded dimension there is no honest
    number to pick, and guessing one would be the same "refuses legal work"
    failure with extra steps.
    """
    budgets: list[int] = []
    for value in [record.disk_mb, *(
        mount.get("perSandboxQuotaMb") for mount in record.volume_mounts
    )]:
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        if value <= 0:
            return None
        budgets.append(value)
    return max(budgets) if budgets else None


def state_clause(record: RuntimeSandbox | None, state: str | None = None) -> str:
    """``"Sandbox is paused"`` -- plus the platform's reason when it has one.

    Called by both gates (the HTTP file endpoints and the Connect-RPC
    dispatcher) so the two refusals cannot drift, and so a caller that is
    being *kept out* learns why in the same message that tells it to resume
    (N28/D). The reason is only ever set by a platform-initiated pause
    (``SandboxRegistry.enforce_disk_budget``); a caller's own pause answers
    with the bare clause.
    """
    current = state or getattr(record, "state", "running")
    reason = getattr(record, "pause_reason", None)
    return f"Sandbox is {current}: {reason}" if reason else f"Sandbox is {current}"


class RuntimeRegistry:
    """Maps sandbox IDs to runtime records; filesystem-backed fallback."""

    #: E9.1: coalesce activity marks so a busy sandbox does not turn every
    #: proxied request into a callback / heartbeat-payload update. The idle
    #: threshold these feed is minutes wide (default 300s).
    ACTIVITY_COALESCE_S = 10.0
    #: A record unregistered while its tree is being torn down must not come
    #: back: ``unregister()`` does not delete ``sandbox.json``, and the heavy
    #: half of a teardown (quota release, rmtree) runs off the event loop, so
    #: a request arriving in that window reads the file straight back into
    #: this process's registry -- which then claims a sandbox whose tree is
    #: already gone (review W1, race B). The teardown drops the tombstone when
    #: it finishes; the deadline is the safety net for an unregister that has
    #: no teardown behind it.
    UNREGISTER_TOMBSTONE_S = 5.0

    def __init__(
        self,
        workspace_base: str | Path,
        *,
        uid_pool=None,
    ) -> None:
        self._workspace_base = Path(workspace_base)
        self._records: dict[str, RuntimeSandbox] = {}
        #: ``sandbox_id -> monotonic deadline`` of the just-unregistered
        #: marker (see ``UNREGISTER_TOMBSTONE_S``).
        self._tombstones: dict[str, float] = {}
        self._lock = threading.Lock()
        self._unregister_callbacks: list[Callable[[str], None]] = []
        self._state_callbacks: list[Callable[[str, str], None]] = []
        #: E9.1: ``sandbox_id -> unix seconds`` of the last request the
        #: sandbox served. In-memory only (never written into ``sandbox.json``:
        #: it would turn every proxied call into a disk write); the worker
        #: ships it to the control plane on each heartbeat, and an in-process
        #: (combined) deployment gets it through ``_activity_callbacks``.
        self._activity: dict[str, float] = {}
        self._activity_callbacks: list[Callable[[str, float], None]] = []
        #: E3.2 host-uid allocator shared by every app that provisions
        #: sandboxes on this workspace (worker agent + local-node control
        #: plane). ``None`` = independent-uid mode disabled.
        self.uid_pool = uid_pool
        self._dirty_provider: Callable[[str], tuple[list[str], bool] | None] | None = None
        #: N25/L2c: per-sandbox `DirLedger`, keyed by id, dropped when the
        #: sandbox is unregistered (its tree is about to go away).
        self._ledgers: dict[str, DirLedger] = {}
        #: N25/L2c: how the last rounds were answered, and when that was last
        #: reported (see `_log_dirty_split`).
        self._dirty_stats = {"ledger": 0, "rebuilt": 0, "walk": 0}
        self._dirty_log_at = 0.0
        #: N25/L2c: how long a written directory keeps being re-checked (see
        #: `DirLedger`), and how long the incremental answer may go before the
        #: accounting is rebuilt from a whole-tree walk. Both are worker-side
        #: knobs read here rather than threaded through `Settings`: they only
        #: exist while `E2B_DISK_ENFORCE_DIRTY` selects this path.
        self._dirty_grace_s = _env_seconds("E2B_DISK_DIRTY_GRACE_S", 120.0)
        self._dirty_reconcile_s = _env_seconds("E2B_DISK_RECONCILE_INTERVAL_S", 900.0)
        #: N25/L2b: where the next ``disk_usage_snapshot`` round starts, so a
        #: scan budget that runs out does not always starve the same trees.
        self._disk_scan_cursor = 0
        # Best-effort, idempotent migration of records that still live inside
        # their sandbox tree (pre-split fleets); never fatal at startup.
        try:
            self.adopt_legacy_records()
        except Exception:  # pragma: no cover - defensive
            logger.warning("legacy record adoption failed", exc_info=True)

    def add_unregister_callback(self, callback) -> None:
        """Invoke ``callback(sandbox_id)`` after a sandbox is unregistered."""
        with self._lock:
            self._unregister_callbacks.append(callback)

    def add_state_callback(self, callback) -> None:
        """Invoke ``callback(sandbox_id, state)`` when a sandbox pauses/resumes."""
        with self._lock:
            self._state_callbacks.append(callback)

    def add_activity_callback(self, callback) -> None:
        """Invoke ``callback(sandbox_id, unix_seconds)`` on sandbox activity.

        Used by the combined (control plane + worker in one process)
        deployment, where there is no heartbeat to carry the report.
        """
        with self._lock:
            self._activity_callbacks.append(callback)

    def mark_active(self, sandbox_id: str) -> None:
        """Note that ``sandbox_id`` just served a request (E9.1)."""
        moment = time.time()
        with self._lock:
            if sandbox_id not in self._records:
                return
            previous = self._activity.get(sandbox_id)
            if previous is not None and moment - previous < self.ACTIVITY_COALESCE_S:
                return
            self._activity[sandbox_id] = moment
            callbacks = list(self._activity_callbacks)
        for callback in callbacks:
            try:
                callback(sandbox_id, moment)
            except Exception:  # pragma: no cover - defensive
                pass

    def activity_snapshot(self) -> dict[str, float]:
        """Copy of the per-sandbox activity timestamps, for the heartbeat."""
        with self._lock:
            return dict(self._activity)

    def disk_usage_snapshot(
        self, *, budget_s: float | None = None, dirty: bool = False
    ) -> dict[str, int]:
        """Measured file bytes per sandbox tree, for the heartbeat (N25/L2b).

        The worker owns the mount, so it is the only party that can measure a
        tree; the control plane turns a report into a pause (it owns state).
        This is the *second* disk gate: the per-node and fleet ledgers bound
        what the sandbox was **sold** (``diskMB`` at create time), and this
        one catches the sandbox that wrote past it.

        Cost was measured on the real NAS rather than assumed (see
        ``docs/disk-quota-options.md`` §5.2): ~3.6-5.5 us/file and ~2.5 ms per
        directory, because NFSv4 readdirplus returns a directory's attributes
        in one RPC -- 10 000 files in one directory walk in 36 ms, and the
        same 10 000 spread over 100 directories in 273 ms. That is cheap
        enough that no change-notification machinery is needed; instead the
        round stops at ``budget_s`` and resumes at the next sandbox next time,
        so one enormous tree cannot monopolise the heartbeat thread.

        ``None`` from the size scan (a tree the worker's DAC cannot reach and
        the broker does not cover) is reported as an absent entry: "unknown"
        must never be read as "empty".
        """
        from envd_service import priv_helpers

        with self._lock:
            records = list(self._records.values())
            if not records:
                return {}
            start = self._disk_scan_cursor % len(records)
        deadline = None if budget_s is None else time.monotonic() + budget_s
        usage: dict[str, int] = {}
        scanned = 0
        for index, record in enumerate(records[start:] + records[:start]):
            # Always scan one: a round that returns nothing at all would leave
            # the cursor where it was and starve every tree behind it forever.
            if index and deadline is not None and time.monotonic() >= deadline:
                break
            size = self._incremental_dir_size(record, dirty=dirty)
            if size is None:
                size = priv_helpers.dir_size(record.workspace_dir)
                if dirty:
                    self._dirty_stats["walk"] += 1
            if size is not None:
                usage[record.sandbox_id] = int(size)
            scanned += 1
        if dirty:
            self._log_dirty_split()
        with self._lock:
            self._disk_scan_cursor = (start + scanned) % len(records)
        return usage

    def set_dirty_provider(self, provider) -> None:
        """Install the per-sandbox dirty-directory source (N25/L2c).

        ``provider(sandbox_id) -> (dirs, overflow) | None``: the directories
        that sandbox has written since the last call, or ``None`` when there is
        no ledger to ask (no live session, an older wheel, the pure shape).
        The registry owns the *sizes*; the provider owns "what changed".
        """
        with self._lock:
            self._dirty_provider = provider

    def note_local_write(self, sandbox_id: str, path: str | Path) -> None:
        """Record a write the **worker itself** made inside a sandbox tree.

        N25/L2c's dirty set comes from the mediator, which sees every write the
        sandbox makes -- but not the ones the platform makes on its behalf
        (the MCP gateway token is the one that lands inside the tree at
        runtime). Those are our own code, so they are marked at the write
        point: no inference, no extra walk.
        """
        with self._lock:
            ledger = self._ledgers.get(sandbox_id)
        if ledger is None or not ledger.ready:
            return
        try:
            ledger.apply([Path(path).parent])
        except DirLedgerUnknown:
            ledger.invalidate()

    def refresh_disk_usage(self, sandbox_id: str) -> int | None:
        """The tree's size *now*, from the ledger (N25/L2c), or ``None``.

        The per-exec ceiling (N25/C) asks this before each command, so the
        ceiling is "what is left" rather than "what was left up to one scan
        interval ago". With dirty-directory accounting that refresh is the work
        one scan round does for one sandbox -- milliseconds -- instead of the
        whole-tree walk it replaced (measured: 1044 ms for 400 directories).

        ``None`` means "cannot answer" (no provider, no ledger yet, an
        unreadable directory, the pure shape), and the caller must fall back to
        the instance ceiling rather than invent a number.
        """
        try:
            record = self.get(sandbox_id)
        except UnknownSandboxError:
            return None
        return self._incremental_dir_size(record, dirty=True)

    def _ledger_for(self, record: RuntimeSandbox) -> DirLedger:
        with self._lock:
            ledger = self._ledgers.get(record.sandbox_id)
            if ledger is None:
                ledger = DirLedger(
                    record.workspace_dir, grace_s=self._dirty_grace_s
                )
                self._ledgers[record.sandbox_id] = ledger
            return ledger

    def _incremental_dir_size(
        self, record: RuntimeSandbox, *, dirty: bool
    ) -> int | None:
        """The tree's size from the ledger, or ``None`` to walk it.

        Every "cannot answer" path degrades to the whole-tree walk that was
        the only implementation before this existed -- a wrong number is the
        one outcome that must not happen, so an overflow, a lost baseline, an
        unreadable directory, or a baseline older than the reconcile interval
        all end in a walk rather than an estimate.
        """
        if not dirty:
            return None
        provider = self._dirty_provider
        if provider is None:
            return None
        ledger = self._ledger_for(record)
        # The backstop first, because it does not depend on the answer: the
        # mediator cannot see everything (a descriptor held open past the grace
        # window, a write from another trust domain), so the accounting is
        # rebuilt from a real walk on its own schedule no matter how healthy
        # the incremental path looks.
        reconcile = self._dirty_reconcile_s
        if (
            reconcile > 0
            and ledger.ready
            and ledger.seconds_since_rebuild >= reconcile
        ):
            drained = provider(record.sandbox_id)
            dirs = drained[0] if drained is not None else []
            self._dirty_stats["rebuilt"] += 1
            total = ledger.rebuild()
            ledger.rescan_next(dirs)
            return total

        drained = provider(record.sandbox_id)
        if drained is None:
            return None
        dirs, overflow = drained
        if overflow or not ledger.ready:
            # Overflow means the mediator stopped recording, so the ledger has
            # to be rebuilt from scratch before it can be trusted again.
            self._dirty_stats["rebuilt"] += 1
            total = ledger.rebuild()
            # A rebuild drains and then walks, and the two are not atomic: a
            # writer that opened its file *before* the drain and appended
            # *during* the walk can fall between them, in a directory the walk
            # had already passed. Re-checking exactly those directories once
            # more is what closes that window (they are the ones the drain
            # named), and it costs one scan of the directories that changed.
            ledger.rescan_next(dirs)
            return total
        try:
            size = ledger.apply(dirs)
        except DirLedgerUnknown:
            ledger.invalidate()
            return None
        self._dirty_stats["ledger"] += 1
        return size

    def _log_dirty_split(self) -> None:
        """Say how the last rounds were answered, at most once a minute.

        The incremental path is allowed to fall back to the walk at any time,
        which means a *correct* number proves nothing about whether the ledger
        is doing any work: without this line, "the feature is inert" and "the
        feature works" look identical from outside.
        """
        if not any(self._dirty_stats.values()):
            return
        now = time.monotonic()
        if now - self._dirty_log_at < 60.0:
            return
        self._dirty_log_at = now
        logger.info(
            "disk accounting: ledger=%d rebuilt=%d walk=%d (since the last "
            "report)",
            self._dirty_stats["ledger"],
            self._dirty_stats["rebuilt"],
            self._dirty_stats["walk"],
        )
        self._dirty_stats = {"ledger": 0, "rebuilt": 0, "walk": 0}

    def _record_path(self, sandbox_id: str) -> Path:
        """Where this sandbox's runtime record is written.

        ``_runtime/<id>/sandbox.json`` -- outside the sandbox's own tree. The
        tree is the sandbox's to own (it must be able to write its workspace),
        which also means it can unlink anything inside it, so the platform's
        record cannot live there and stay trustworthy. Readers still fall back
        to the old in-tree location; :meth:`adopt_legacy_records` moves it.
        """
        return sandbox_record_path(self._workspace_base, sandbox_id)

    def _legacy_record_path(self, sandbox_id: str) -> Path:
        return sandbox_record_path(self._workspace_base, sandbox_id, legacy=True)

    def _ensure_runtime_dir(self, sandbox_id: str) -> Path:
        """Create ``_runtime/<id>``, owned by the worker and closed to sandboxes.

        ``0700`` on purpose: the per-sandbox host uid is not the owner and is
        (by the E3.2 model) not in the worker's group either, so the sandbox
        cannot traverse into it -- not to read the record, and not to delete
        it. Ownership follows whoever runs the worker (root in the production
        shape), never the sandbox uid.
        """
        path = sandbox_runtime_dir(self._workspace_base, sandbox_id)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
            os.chown(path, os.geteuid(), os.getegid())
        except OSError:  # pragma: no cover - best effort, like the modes above
            pass
        return path

    @property
    def workspace_base(self) -> Path:
        """The workspace root this registry reads and writes records under.

        The trees it describes live here, one directory per sandbox id, so
        this -- not any path a record claims -- is the base a teardown derives
        its target from (W1).
        """
        return self._workspace_base

    def register(
        self,
        *,
        sandbox_id: str,
        access_token: str,
        workspace_dir: str,
        env_vars: dict[str, str] | None = None,
        base_image: str | None = None,
        host_uid: int | None = None,
        memory_mb: int = 1024,
        cpu_percent: int = 100,
        disk_mb: int = 1024,
        project_id: int | None = None,
        max_processes: int = 256,
        max_open_files: int = 4096,
        allow_internet_access: bool = False,
        max_command_timeout: int = 3600,
        volume_mounts: list[dict[str, Any]] | None = None,
        volume_projects: list[dict[str, Any]] | None = None,
        mcp: dict | None = None,
        network: dict | None = None,
        allow_public_traffic: bool = False,
        iam_tokens: dict[str, dict[str, str]] | None = None,
    ) -> RuntimeSandbox:
        if not validate_sandbox_id(sandbox_id):
            raise ValueError(f"invalid sandbox id: {sandbox_id}")
        record = RuntimeSandbox(
            sandbox_id=sandbox_id,
            access_token=access_token,
            workspace_dir=workspace_dir,
            env_vars=dict(env_vars or {}),
            base_image=base_image,
            host_uid=host_uid,
            memory_mb=memory_mb,
            cpu_percent=cpu_percent,
            disk_mb=disk_mb,
            project_id=project_id,
            max_processes=max_processes,
            max_open_files=max_open_files,
            allow_internet_access=allow_internet_access,
            max_command_timeout=max_command_timeout,
            volume_mounts=list(volume_mounts or []),
            volume_projects=list(volume_projects or []),
            mcp=mcp,
            network=dict(network) if network else None,
            allow_public_traffic=bool(allow_public_traffic),
            iam_tokens=dict(iam_tokens or {}),
        )
        with self._lock:
            self._tombstones.pop(sandbox_id, None)
            self._records[sandbox_id] = record
            # A re-created id must not inherit the previous incarnation's
            # baseline (N25/L2c): its tree may be a fresh template copy, and a
            # ledger that assumed continuity would report the difference.
            self._ledgers.pop(sandbox_id, None)
            try:
                self._ensure_runtime_dir(sandbox_id)
                path = self._record_path(sandbox_id)
                path.write_text(
                    json.dumps(record.to_dict(), separators=(",", ":")), encoding="utf-8"
                )
                # The in-tree copy was the pre-split location and is still
                # writable by the sandbox itself; once the authoritative copy
                # exists outside the tree, drop it rather than leave a
                # forgeable second version behind.
                self._legacy_record_path(sandbox_id).unlink(missing_ok=True)
            except OSError:
                pass
        return record

    def get(self, sandbox_id: str) -> RuntimeSandbox | None:
        if not validate_sandbox_id(sandbox_id):
            return None
        with self._lock:
            record = self._records.get(sandbox_id)
            if record is not None:
                return record
            if self._tombstoned(sandbox_id):
                # Its teardown is in flight (or just finished): the file on
                # disk is the input the teardown is deleting, not a record to
                # materialise (race B).
                return None
        # Filesystem-backed lookup (separate-process deployment).
        record = self._load_from_disk(sandbox_id)
        if record is None:
            return None
        with self._lock:
            # The teardown can have started while the file was being read.
            if self._tombstoned(sandbox_id):
                return None
            self._records[sandbox_id] = record
        return record

    def _tombstoned(self, sandbox_id: str) -> bool:
        """Whether ``sandbox_id`` was unregistered moments ago (lock held)."""
        deadline = self._tombstones.get(sandbox_id)
        if deadline is None:
            return False
        if deadline <= time.monotonic():
            self._tombstones.pop(sandbox_id, None)
            return False
        return True

    def release_tombstone(self, sandbox_id: str) -> None:
        """Drop the just-unregistered marker once its teardown has finished.

        The window the marker closes is the teardown itself; a caller that
        re-creates the same sandbox id right after the teardown must not be
        answered from the deleted tree's leftovers (``register()`` clears it
        as well).
        """
        with self._lock:
            self._tombstones.pop(sandbox_id, None)

    def peek(self, sandbox_id: str) -> RuntimeSandbox | None:
        """Read a record without caching it in this process.

        The orphan-tree scan uses this instead of :meth:`get`: in a
        shared-workspace deployment the scan sees every node's trees, and a
        foreign record must not enter this process's registry (every
        in-memory record is treated as one this worker owns and may tear
        down).
        """
        if not validate_sandbox_id(sandbox_id):
            return None
        with self._lock:
            record = self._records.get(sandbox_id)
        if record is not None:
            return record
        return self._load_from_disk(sandbox_id)

    def _load_from_disk(self, sandbox_id: str) -> RuntimeSandbox | None:
        """Parse the runtime record; ``None`` when unusable.

        Prefers ``_runtime/<id>/sandbox.json`` and falls back to the legacy
        in-tree copy (see :meth:`adopt_legacy_records`), so a worker rolling
        onto a fleet that still has pre-split trees can still adopt them.
        """
        path = self._record_path(sandbox_id)
        if not path.is_file():
            legacy = self._legacy_record_path(sandbox_id)
            if legacy.is_file():
                path = legacy
        try:
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = RuntimeSandbox.from_dict(payload)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and "created_at" not in payload:
            # Records written before the field existed (2026-09-02) would
            # otherwise parse with the dataclass default, i.e. the time we
            # happened to read them -- every one of them looks like a create
            # that raced the reconcile window and is pinned forever (review
            # round 1, M2). The file's mtime is the creation time the disk
            # actually has; a genuine concurrent create always carries the
            # key, because ``register()`` writes ``asdict()``.
            try:
                record.created_at = path.stat().st_mtime
            except OSError:  # pragma: no cover - defensive
                pass
        return record

    def adopt_legacy_records(self) -> list[str]:
        """Move pre-split in-tree records into ``_runtime/``.

        Idempotent, and safe to call at startup: for every sandbox tree that
        still carries its own ``sandbox.json`` and has no runtime copy yet, the
        file is moved (not copied) into ``_runtime/<id>/``. After this the
        platform's record is out of the sandbox's reach, which is the whole
        point of the split -- a sandbox can delete and rewrite files inside its
        own tree, so a record left there is a record it can forge.

        The moved file carries whatever the sandbox left behind, so adoption
        logs it: a forged record is frozen here rather than trusted, and the
        fleet-level values it might lie about (``host_uid``, volume slices) are
        owned by the control plane's record store, not by this file.
        """
        adopted: list[str] = []
        try:
            entries = list(self._workspace_base.iterdir())
        except OSError:
            return adopted
        for entry in entries:
            if not validate_sandbox_id(entry.name) or not entry.is_dir():
                continue
            legacy = self._legacy_record_path(entry.name)
            if not legacy.is_file() or self._record_path(entry.name).is_file():
                continue
            try:
                self._ensure_runtime_dir(entry.name)
                os.replace(legacy, self._record_path(entry.name))
            except OSError:
                continue
            logger.warning(
                "adopted the pre-split record of %s into _runtime/ (its "
                "contents came from a file the sandbox could rewrite; the "
                "authoritative host uid and volume slices live in the control "
                "plane)",
                entry.name,
            )
            adopted.append(entry.name)
        return adopted

    def unregister(self, sandbox_id: str) -> None:
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            removed = self._records.pop(sandbox_id, None) is not None
            self._activity.pop(sandbox_id, None)
            self._ledgers.pop(sandbox_id, None)
            self._tombstones[sandbox_id] = (
                time.monotonic() + self.UNREGISTER_TOMBSTONE_S
            )
            callbacks = list(self._unregister_callbacks)
        if removed:
            for callback in callbacks:
                try:
                    callback(sandbox_id)
                except Exception:
                    pass
        # Deliberately *not* removing ``_runtime/<id>`` here: unregister also
        # runs when a teardown is refused, and the record has to stay on disk
        # for the next delete to verify against (review W7). It is removed
        # together with the tree, by whoever removes the tree.

    def set_state(
        self, sandbox_id: str, state: str, reason: str | None = None
    ) -> None:
        """Move ``sandbox_id`` to ``state``; ``reason`` explains a non-running one.

        The reason travels with the state (see ``RuntimeSandbox.pause_reason``)
        and is dropped on the way back to ``running``: it belongs to the pause
        it was set by.
        """
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            record = self._records.get(sandbox_id)
            if record is None:
                return
            record.state = state
            record.pause_reason = reason if state != "running" else None
            callbacks = list(self._state_callbacks)
        for callback in callbacks:
            try:
                callback(sandbox_id, state)
            except Exception:
                pass

    def freeze(self, sandbox_id: str) -> None:
        """Temporarily freeze the sandbox process tree without changing state.

        Used for consistent filesystem snapshots: running commands are
        SIGSTOPped while the directory is copied, then thawed.
        """
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            callbacks = list(self._state_callbacks)
        for callback in callbacks:
            try:
                callback(sandbox_id, "paused")
            except Exception:
                pass

    def thaw(self, sandbox_id: str) -> None:
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            callbacks = list(self._state_callbacks)
        for callback in callbacks:
            try:
                callback(sandbox_id, "running")
            except Exception:
                pass

    def list(self) -> list[RuntimeSandbox]:
        with self._lock:
            return list(self._records.values())
