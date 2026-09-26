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
    resolve_state_base,
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
        state_base: str | Path | None = None,
    ) -> None:
        self._workspace_base = Path(workspace_base)
        #: The base the platform's own files live under for every sandbox this
        #: registry describes: the record, the command log, the checkpoint
        #: images. ``None`` = the workspace base, i.e. the layout that predates
        #: N27 (``gateway_common.paths.resolve_state_base``).
        self._state_base = state_base
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
        #: N25: bytes each sandbox has *appended* since its last exact
        #: measurement, pushed by its mediator (`note_appended`) instead of
        #: being polled for. A round consumes them, because the round's number
        #: already contains everything committed up to that moment.
        self._appended: dict[str, int] = {}
        #: N25: the last number reported per sandbox. Pushed appends are
        #: increments on *this*, not on the walk (see `_reported_usage`).
        self._reported: dict[str, int] = {}
        #: N25: the cumulative bytes the mediator has watched since the last
        #: round with nothing to push (see `_reported_usage`).
        self._pushed_total: dict[str, int] = {}
        #: N25: the live tightener (`f(sandbox_id, bytes) -> applied | None`),
        #: installed by the app. Absent means "no live tightening": the
        #: per-exec ceiling and the pause gate still hold.
        self._tightener: Callable[[str, int], dict | None] | None = None
        #: Last limit sent per sandbox and when, so a round that changes
        #: nothing costs nothing and a shrinking budget is sent in steps.
        self._tightened: dict[str, int] = {}
        self._tightened_at: dict[str, float] = {}
        #: N31: the same shape for the *entry* cap -- how many names the tree
        #: holds, against a limit. Separate state because the two move
        #: independently: opening an empty file moves this one and not the
        #: bytes, writing a big file moves the bytes and not this one.
        self._entry_tightener: Callable[[str, int, int], dict | None] | None = None
        #: N25: `provider(sandbox_id) -> (spent, freed) | None`, read *before*
        #: the walk so the tightened number can be dated. Absent means the
        #: anchor stays at delivery, which is what it used to be.
        self._counter_provider: Callable[[str], tuple[int, int] | None] | None = None
        self._entry_tightened: dict[str, int] = {}
        self._entry_tightened_at: dict[str, float] = {}
        #: `E2B_DISK_MAX_ENTRIES` (0 = off). The knob is a *runaway backstop*,
        #: not a policy: a legitimate build (`npm install` on a large tree, a
        #: Python venv with thousands of files) must not hit it, while a
        #: sandbox creating names in a loop must. Measured cost of the shape
        #: it defends against: 200 empty files take ~4.2 s through the
        #: mediator (≈21 ms each), so a million of them is hours of work --
        #: the limit exists because the *volume's* inodes and the walk cost are
        #: shared with everyone else on the NAS, not because it is fast.
        self._max_entries = int(_env_seconds("E2B_DISK_MAX_ENTRIES", 0.0))
        #: How much the count must move before it is worth a verb, and how
        #: often one may be sent. A crossing of the limit is always sent (see
        #: `_maybe_tighten_entries`), because that is the moment the gate
        #: changes state.
        self._entry_step = int(_env_seconds("E2B_DISK_ENTRY_STEP", 256.0))
        self._entry_interval_s = _env_seconds("E2B_DISK_ENTRY_INTERVAL_S", 1.0)
        #: N25: what to do when a sandbox is *measured* over its budget. The
        #: default, `log`, is the product semantic: the gate that already
        #: exists (zero ceiling + `ENOSPC` for new names) keeps writes out, the
        #: control plane records and alerts, and nothing is frozen -- the
        #: sandbox keeps the reads and the deletes that bring it back inside.
        #:
        #: `deny` pins the write gate shut for `E2B_DISK_OVERRUN_DENY_S` after
        #: a crossing, whatever the next round's estimate says. That closes the
        #: one hole `log` leaves open: the number the gate is computed from is
        #: an estimate, and an estimate that dips back under the budget on a
        #: stale walk would re-open writes while the tree is still over. It is
        #: opt-in because it also delays a sandbox that legitimately deleted its
        #: way back inside (until the hysteresis below is cleared).
        self._overrun_action = (
            os.environ.get("E2B_DISK_OVERRUN_ACTION", "log") or "log"
        ).strip().lower()
        self._overrun_deny_s = _env_seconds("E2B_DISK_OVERRUN_DENY_S", 60.0)
        self._denied_until: dict[str, float] = {}
        #: The measurement has to fall this far below the budget before `deny`
        #: lets go, so a value that wobbles around the line keeps the gate shut.
        self._overrun_exit_ratio = _env_seconds("E2B_DISK_OVERRUN_EXIT_RATIO", 0.98)
        #: N25: how much the limit must drop before it is worth a verb to the
        #: slot, the smallest limit ever sent (a file must be able to hold
        #: *something*, so the ceiling never goes to zero), and how often a
        #: tightening may be sent at most.
        self._tighten_step_bytes = int(
            _env_seconds("E2B_DISK_TIGHTEN_STEP_MB", 1.0) * 1024 * 1024
        )
        #: 0.1 s, not 0.5 s: the number a *new* file inherits is whatever the
        #: last tightening left, so this interval is how stale that number can
        #: be. Measured on the cluster, a 900 MiB command whose remaining budget
        #: was 124 MiB handed each of its two remaining files the full 124 MiB
        #: and ended at 1148 MiB of a 1024 MiB budget.
        self._tighten_interval_s = _env_seconds("E2B_DISK_TIGHTEN_INTERVAL_S", 0.1)
        #: N25: the push wakes a scan round instead of waiting for the cadence.
        #: Past `E2B_DISK_APPEND_TRIGGER_MB` since the last round, ask for one
        #: now (never more often than `E2B_DISK_APPEND_MIN_INTERVAL_S`, because
        #: the round is the expensive half).
        #:
        #: What the wake is *for*, measured on the cluster: a
        #: `for i in 1 2 3; do dd of=part$i.bin count=900; done` in a 1024 MiB
        #: sandbox was tightened to 124 MiB of remaining budget once part1 was
        #: written, and ended frozen at 1148 MiB -- 900 + 124 + 124, because
        #: each of the two remaining files was allowed the whole remaining
        #: budget. The per-file ceiling can only be as fresh as the number it
        #: was computed from, so the appends have to move that number while the
        #: command runs, not on the next 1 s cadence.
        self._append_trigger_bytes = max(
            1, int(_env_seconds("E2B_DISK_APPEND_TRIGGER_MB", 8.0) * 1024 * 1024)
        )
        self._append_min_interval_s = _env_seconds("E2B_DISK_APPEND_MIN_INTERVAL_S", 0.2)
        self._disk_wakeup: Callable[[str], None] | None = None
        self._disk_wakeup_at = 0.0
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
        #: N25: sandboxes that are over budget now, and the ones that *just*
        #: crossed (the latter is the event worth reporting immediately).
        self._disk_over: set[str] = set()
        self._disk_crossings: dict[str, int] = {}
        #: N25: per-call timing of the two halves of a dirty round (the drain
        #: into the mediator and the re-walk of what it reported), so a round
        #: that takes longer than the walk can be told apart from one that is
        #: waiting on something else. Off unless E2B_DISK_TRACE is set.
        self._trace = str(os.getenv("E2B_DISK_TRACE", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
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
            # N25: date the walk *before* taking it. The worker's number is a
            # maintained ledger and can be older than its message; the two
            # counters are the instant it was true, and the mediator subtracts
            # everything that happened since.
            stamps = self._counter_provider(record.sandbox_id) if self._counter_provider else None
            scanned_usage = self._incremental_dir_usage(record, dirty=dirty)
            entries: int | None = None
            if scanned_usage is None:
                size = priv_helpers.dir_size(record.workspace_dir)
                if dirty:
                    self._dirty_stats["walk"] += 1
            else:
                size, entries = scanned_usage
            if size is not None:
                # N25: the walk answers what has *committed*; the pushed
                # appends answer what the sandbox wrote since that answer,
                # which on this storage can be seconds of writing the
                # filesystem cannot see yet (§22.5). Their sum is a lower
                # bound on the truth, so reporting it is early-or-exact.
                # The walk answers what has *committed*; the pushed appends
                # answer what the sandbox wrote since that answer. They must
                # not be added together: once the writeback lands, the same
                # bytes are in the walk *and* on the push, and a sum reports
                # twice the truth. Measured on the cluster that is what it did
                # -- a 287 MiB file was reported as 647 MiB used, so the
                # remaining budget was 377 MiB and the live tightening cut the
                # file with EFBIG at 696 MiB (`docs/k8s-deployment.md` §22.5.8).
                #
                # So the push is an *increment on the last report*, and the
                # walk is a floor:
                #
                #   reported = max(walk, previous_reported + pushed_since)
                #
                # which counts a byte once whether the push saw it, the walk
                # saw it, or both. With nothing pushed the walk governs, so a
                # sandbox that deletes files is not held at a stale high-water
                # mark for longer than the round after its last write.
                reported = self._reported_usage(
                    record.sandbox_id, int(size), self._take_appended(record.sandbox_id)
                )
                usage[record.sandbox_id] = reported
                self._note_budget_crossing(record, reported)
                self._maybe_tighten(record, reported, stamps)
                if entries is not None:
                    entry_stamps = None if stamps is None else (stamps[2], stamps[3])
                    self._maybe_tighten_entries(record, entries, entry_stamps)
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

    def set_disk_tightener(self, tightener) -> None:
        """Install the live file-size tightener (N25).

        ``tightener(sandbox_id, bytes) -> dict | None`` lowers the running
        sandbox's ``RLIMIT_FSIZE`` to at most ``bytes`` *now* and reports what
        the slot applied. The registry owns *when* -- it is the only component
        that knows what is left -- and the tightener owns *how*.
        """
        with self._lock:
            self._tightener = tightener

    def set_entry_tightener(self, tightener) -> None:
        """Install the entry-count tightener (N31).

        ``tightener(sandbox_id, entries, limit) -> dict | None`` tells the
        running sandbox how many names its tree holds and how many it may
        hold, so the mediator can refuse the four syscalls that create one.
        The registry owns the *count* (it is the component that walks the
        tree); the tightener owns the channel.
        """
        with self._lock:
            self._entry_tightener = tightener

    def set_counter_provider(self, provider) -> None:
        """Install the write-counter source used to *date* a walk (N25).

        ``provider(sandbox_id) -> (spent, freed) | None`` is called at the
        start of an accounting round, before the tree is measured, and the
        numbers travel with the budget the round sends. They are the
        difference between "subtract what happened after this message
        arrived" (which handed a second file 48 MiB past the budget) and
        "subtract what happened after this number was *measured*".
        """
        with self._lock:
            self._counter_provider = provider

    def set_disk_wakeup(self, wakeup) -> None:
        """Install the "run a round now" callback (N25).

        ``wakeup(sandbox_id)`` is called from a slot's event thread when a
        sandbox has appended enough for the current number to be worth
        re-measuring, so it must be safe to call from another thread -- the
        agent's implementation is `loop.call_soon_threadsafe`. ``None`` (the
        default) keeps the plain cadence.
        """
        with self._lock:
            self._disk_wakeup = wakeup

    def note_appended(self, sandbox_id: str, bytes_: int) -> None:
        """Record bytes a mediator watched the sandbox append (N25).

        Called from each slot's event pump, on that slot's own thread, so it
        takes this lock and does nothing else. The number is a *lower bound* on
        how much the workspace grew (see `docs/k8s-deployment.md` §22.5), which
        is the direction a quota wants: `last_exact + appended` can be early,
        never late, and never larger than the truth unless the sandbox deleted
        as much as it wrote in the same window.
        """
        if bytes_ <= 0:
            return
        wakeup = None
        with self._lock:
            self._appended[sandbox_id] = self._appended.get(sandbox_id, 0) + int(bytes_)
            if self._trace:
                logger.info(
                    "disk append: %s +%d bytes (pending %d, wakeup=%s)",
                    sandbox_id,
                    int(bytes_),
                    self._appended[sandbox_id],
                    self._disk_wakeup is not None,
                )
            wakeup = self._disk_wakeup
            now = time.monotonic()
            worth_a_round = self._appended[sandbox_id] >= self._append_trigger_bytes
            allowed = now - self._disk_wakeup_at >= self._append_min_interval_s
            if wakeup is not None and worth_a_round and allowed:
                self._disk_wakeup_at = now
            else:
                wakeup = None
        if wakeup is not None:
            if self._trace:
                logger.info("disk wakeup: %s has appended enough for a round", sandbox_id)
            # Outside the lock: the callback schedules work on the event loop,
            # and a slow or misbehaving one must not stall the event pumps.
            wakeup(sandbox_id)

    def _peek_appended(self, sandbox_id: str) -> int:
        with self._lock:
            return self._appended.get(sandbox_id, 0)

    def _take_appended(self, sandbox_id: str) -> int:
        with self._lock:
            return self._appended.pop(sandbox_id, 0)

    def _reported_usage(self, sandbox_id: str, walk: int, pushed: int) -> int:
        """What to report for one sandbox this round (N25).

        The push and the walk measure the *same* bytes from two sides -- the
        sandbox's own descriptors, and what the server has committed -- and
        neither knows which bytes the other has already seen. They are
        therefore combined as two estimates of the same quantity, never added:

            reported = max(walk, bytes the mediator has watched since the
                                 last round that had nothing to push)

        The push side is *cumulative*, not a per-round increment: that is what
        makes a late sample harmless (the walk it duplicates is already
        covered by the max) and what makes an early one visible. A round with
        nothing pushed re-bases it on the walk, which is when the filesystem is
        authoritative -- and it is what lets a *deletion* come back down.

        Measured on the cluster, the naive `walk + pushed` reported 287 MiB for
        a 287 MiB file (the commit landed between two rounds) and 647 MiB for
        the same file a round later (§22.5.8), which cut a legal 900 MiB file
        with EFBIG at 696 MiB. Measured with the *previous* fix -- `max(walk,
        previous + pushed)` -- a 700 MiB fill was reported as 777 MiB, because
        the mediator's samples of it arrived after the walk had banked the same
        bytes; the 77 MiB of phantom usage left a new file only 247 MiB of a
        324 MiB budget. Anchoring removes both: a byte the walk already counted
        is inside the anchor, not on top of it.

        A round with nothing pushed re-anchors on the walk: that is when the
        filesystem is authoritative, and it is what lets a *deletion* come
        back down.
        """
        with self._lock:
            total = 0 if pushed <= 0 else self._pushed_total.get(sandbox_id, 0) + pushed
            self._pushed_total[sandbox_id] = total
            reported = max(walk, total)
            self._reported[sandbox_id] = reported
            return reported

    def _peek_usage(self, sandbox_id: str, walk: int) -> int:
        """The same identity as [`Self::_reported_usage`], without consuming.

        The ceiling asks this between rounds, so it must see the pending
        appends *and* must not take them from the round that reports them.
        """
        with self._lock:
            pushed = self._appended.get(sandbox_id, 0)
            if pushed <= 0:
                return walk
            total = self._pushed_total.get(sandbox_id, 0) + pushed
            return max(walk, total)

    def note_local_write(self, sandbox_id: str, path: str | Path) -> None:
        """Record a write the **worker itself** made inside a sandbox tree.

        N25/L2c's dirty set comes from the mediator, which sees every write the
        sandbox makes -- but not the ones the platform makes on its behalf
        (the MCP gateway token is the one that lands inside the tree at
        runtime). Those are our own code, so they are marked at the write
        point: no inference, no extra walk.

        The mark is the written path's directory **and every directory above it
        up to the tree root**: since N31 fix 2 a directory's own ``st_size`` is
        part of the number, and the ``mkdir(parents=True)`` that precedes this
        write gives a *new name* to each level -- the token the caller names is
        two levels down, but the tree root is the directory that received
        ``etc``, so a mark set of ``{<tree>/etc}`` left the root's own entry
        stale (measured: the ledger under-reported by exactly the 6 bytes the
        root grew, ``tests/unit/test_registry_dirty_snapshot.py``). Marking an
        ancestor of a deeper mark costs that ancestor's subtree, because the
        mark set is reduced to its shallowest member; the only caller runs
        once per sandbox (``start_mcp_gateway`` caches), where one walk is
        cheap next to being wrong.
        """
        with self._lock:
            ledger = self._ledgers.get(sandbox_id)
        if ledger is None or not ledger.ready:
            return
        marks: list[Path] = []
        for directory in (Path(path).parent, *Path(path).parent.parents):
            marks.append(directory)
            if directory == ledger.root:
                break
        try:
            ledger.apply(marks)
        except DirLedgerUnknown:
            ledger.invalidate()

    def _maybe_tighten(
        self,
        record: RuntimeSandbox,
        size: int,
        stamps: tuple[int, int] | None = None,
    ) -> None:
        """Lower the running sandbox's file-size limit to what is left (N25).

        The pause gate is the platform's backstop, and it is a *slow* one by
        construction: the control plane only learns a sandbox is over budget
        from a report, and the sandbox keeps writing until the freeze lands.
        Tightening is the fast half -- the kernel refuses the next write past
        what is left, in the process that is writing, with no round trip to
        anyone -- and it is what makes a loop inside one command (a shape the
        per-exec ceiling cannot stop, because that ceiling was fixed when the
        command started) stop at the budget instead of at the freeze.

        Three deliberate restraints, so this stays a quota and not a second
        event stream:

        * **material** moves only, in either direction: the number is the budget
          the fork's `open` grants and its "may the tree grow?" refusals are
          computed from, so a sandbox that deletes its way back inside has to
          be able to write again -- while the `RLIMIT_FSIZE` sweep that same
          verb performs stays one-way (a limit a stale reading could widen is
          not a limit);
        * at most once per ``E2B_DISK_TIGHTEN_INTERVAL_S``.
        """
        tightener = self._tightener
        if tightener is None:
            return
        budget = int(record.disk_mb) * 1024 * 1024
        if budget <= 0:
            return
        # Over budget the answer is no growth, not a value that keeps the tree
        # creeping: `E2B_DISK_EXEC_LIMIT_FLOOR_MB` used to guarantee "something
        # can still be written", and on the cluster that is exactly the MiB
        # that puts a full sandbox past its budget -- where the platform then
        # froze it, taking away the deletes it needed to get back inside. The
        # product semantic is the other way round: over the limit, writes stop
        # and everything else keeps working, so zero is sent and the mediator
        # refuses the entries a zero ceiling cannot reach (`ENOSPC`).
        remaining = max(budget - int(size), 0)
        now = time.monotonic()
        # N25: with `E2B_DISK_OVERRUN_ACTION=deny`, a crossing pins the gate
        # shut for a while even if the next estimate dips back under the
        # budget. The estimate is a walk that can be seconds behind; the pin is
        # what keeps a stale one from re-opening writes to a tree that is
        # still over.
        pinned = self._denied_until.get(record.sandbox_id, 0.0) > now
        if pinned:
            remaining = 0
        # The bookkeeping is taken under the lock (registration and
        # unregistration touch the same three fields); the verb itself is
        # *not* -- it crosses a channel, and holding a lock the event pumps
        # also need while doing I/O is how a slow slot becomes a stalled
        # accounting round.
        with self._lock:
            previous = self._tightened.get(record.sandbox_id)
            # A crossing of zero is always worth a verb, however small the
            # move: leaving "exhausted" is what unblocks a sandbox that made
            # room for itself, and entering it is what stops the writes.
            crossing = previous is not None and ((remaining == 0) != (previous == 0))
            if (
                previous is not None
                and abs(remaining - previous) < self._tighten_step_bytes
                and not crossing
            ):
                # Not material in either direction: the verb carries the budget
                # the mediator's `open` grants are computed from, and a value
                # that only moved by a few kilobytes changes no decision.
                return
            if now - self._tightened_at.get(record.sandbox_id, 0.0) < self._tighten_interval_s:
                return
            self._tightened[record.sandbox_id] = remaining
            self._tightened_at[record.sandbox_id] = now
        try:
            applied = tightener(record.sandbox_id, remaining, stamps)
        except Exception:  # noqa: BLE001 - never break a scan round over this
            logger.warning(
                "disk tightening failed for %s", record.sandbox_id, exc_info=True
            )
            return
        if applied:
            logger.info(
                "disk tightening: %s limited to %s bytes (used %s of %s)",
                record.sandbox_id,
                remaining,
                size,
                budget,
            )

    def _maybe_tighten_entries(
        self,
        record: RuntimeSandbox,
        entries: int,
        stamps: tuple[int, int] | None = None,
    ) -> None:
        """Tell the sandbox how many names its tree holds, and the cap (N31).

        Why this axis exists: the byte budget cannot see a tree that grows by
        *names*. Measured on the cluster, 2000 empty files left the platform's
        reported usage at **0 bytes**, while the directory itself was 16384
        bytes on NFS and is not counted either -- so a sandbox could spend the
        volume's inodes without ever touching its budget, and the only gate
        that covered entry creation fired when the byte pool was *exactly*
        zero, which empty entries never reach.

        The count comes from the ledger (a whole-tree walk has no count to
        give without a second pass, so a deployment that runs without the
        incremental ledger simply does not have this gate -- the knob is a
        backstop, not a policy). Two restraints, the same as the byte half:
        the verb is only worth sending when the number moved materially or the
        *crossing* happened, and at most once per interval.

        ``E2B_DISK_MAX_ENTRIES = 0`` (the default) turns all of this off.
        """
        limit = self._max_entries
        if limit <= 0:
            return
        tightener = self._entry_tightener
        if tightener is None:
            return
        now = time.monotonic()
        with self._lock:
            previous = self._entry_tightened.get(record.sandbox_id)
            over = entries >= limit
            crossing = previous is not None and over != (previous >= limit)
            if (
                previous is not None
                and abs(entries - previous) < self._entry_step
                and not crossing
            ):
                return
            if now - self._entry_tightened_at.get(record.sandbox_id, 0.0) < self._entry_interval_s:
                return
            self._entry_tightened[record.sandbox_id] = entries
            self._entry_tightened_at[record.sandbox_id] = now
        try:
            applied = tightener(record.sandbox_id, entries, limit, stamps)
        except Exception:  # noqa: BLE001 - never break a scan round over this
            logger.warning(
                "entry tightening failed for %s", record.sandbox_id, exc_info=True
            )
            return
        if applied and crossing:
            logger.info(
                "entry cap: %s at %s entries of %s (crossing)",
                record.sandbox_id,
                entries,
                limit,
            )

    def _note_budget_crossing(self, record: RuntimeSandbox, size: int) -> None:
        """Note a sandbox that has just gone over its budget (N25).

        The report rides the heartbeat, which is up to a full interval late; a
        sandbox that has *crossed* its budget is the one case worth pushing
        immediately (see `take_budget_crossings`), because the control plane's
        answer is to freeze it -- and until it does, it keeps writing. Only the
        crossing is an event: a sandbox that stays over budget is not news, and
        re-pushing it every round would be a second heartbeat.
        """
        budget = int(record.disk_mb) * 1024 * 1024
        with self._lock:
            over = budget > 0 and size > budget
            if not over:
                self._disk_over.discard(record.sandbox_id)
                # N25: `deny` lets go once the measurement is clearly back
                # inside -- not the moment it touches the line, or a value that
                # wobbles around the budget would flap the gate.
                if budget > 0 and size <= budget * self._overrun_exit_ratio:
                    self._denied_until.pop(record.sandbox_id, None)
                return
            if record.sandbox_id not in self._disk_over:
                self._disk_over.add(record.sandbox_id)
                self._disk_crossings[record.sandbox_id] = size
            if self._overrun_action == "deny" and self._overrun_deny_s > 0:
                self._denied_until[record.sandbox_id] = (
                    time.monotonic() + self._overrun_deny_s
                )

    def take_budget_crossings(self) -> dict[str, int]:
        """Sandboxes that crossed their budget since the last call (N25)."""
        with self._lock:
            crossings = self._disk_crossings
            self._disk_crossings = {}
            return crossings

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
        size = self._incremental_dir_size(record, dirty=True)
        if size is None:
            return None
        # N25: peek, do not take. The per-exec ceiling asks this between rounds,
        # and the same appended bytes must still be there for the next round to
        # account for. The identity is the round's (`_reported_usage`), because
        # the per-exec ceiling is what a *fresh* command starts with: summing
        # the walk and the push here would hand the command a ceiling computed
        # from a number up to twice the truth.
        return self._peek_usage(sandbox_id, int(size))

    def _ledger_for(self, record: RuntimeSandbox) -> DirLedger:
        with self._lock:
            ledger = self._ledgers.get(record.sandbox_id)
            if ledger is None:
                ledger = DirLedger(
                    record.workspace_dir, grace_s=self._dirty_grace_s
                )
                self._ledgers[record.sandbox_id] = ledger
            return ledger

    def _incremental_dir_usage(
        self, record: RuntimeSandbox, *, dirty: bool
    ) -> tuple[int, int] | None:
        """``(bytes, entries)`` from the ledger, or ``None`` to walk it.

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
            return total, ledger.total_entries

        drained_at = time.monotonic()
        drained = provider(record.sandbox_id)
        drain_s = time.monotonic() - drained_at
        if drained is None:
            self._trace_call(record.sandbox_id, drain_s, 0.0, "walk")
            return None
        dirs, overflow = drained
        if overflow or not ledger.ready:
            self._dirty_stats["rebuilt"] += 1
            total = ledger.rebuild()
            self._trace_call(
                record.sandbox_id, drain_s, time.monotonic() - drained_at, "rebuilt"
            )
            # A rebuild drains and then walks, and the two are not atomic: a
            # writer that opened its file *before* the drain and appended
            # *during* the walk can fall between them, in a directory the walk
            # had already passed. Re-checking exactly those directories once
            # more is what closes that window (they are the ones the drain
            # named), and it costs one scan of the directories that changed.
            ledger.rescan_next(dirs)
            return total, ledger.total_entries
        try:
            size = ledger.apply(dirs)
        except DirLedgerUnknown:
            ledger.invalidate()
            return None
        self._dirty_stats["ledger"] += 1
        self._trace_call(
            record.sandbox_id, drain_s, time.monotonic() - drained_at, "ledger"
        )
        # N31: the same walk that answers "how many bytes" answers "how many
        # names" -- files plus directories, the number the entry cap is about.
        return size, ledger.total_entries

    def _incremental_dir_size(
        self, record: RuntimeSandbox, *, dirty: bool
    ) -> int | None:
        """The bytes half of :meth:`_incremental_dir_usage`."""
        usage = self._incremental_dir_usage(record, dirty=dirty)
        return None if usage is None else usage[0]

    def _trace_call(
        self, sandbox_id: str, drain_s: float, total_s: float, path: str
    ) -> None:
        """Log one round's timing split when ``E2B_DISK_TRACE`` is set (N25).

        The two halves are the question: a round that is slow because it
        *wrote* a lot is doing its job, and a round that is slow because it is
        *waiting* (on the slot's channel, or on a thread) is not -- and from the
        outside they look identical.
        """
        if not self._trace:
            return
        logger.info(
            "disk trace: call sid=%s path=%s drain=%.3fs total=%.3fs apply=%.3fs",
            sandbox_id,
            path,
            drain_s,
            total_s,
            max(0.0, total_s - drain_s),
        )

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
        return sandbox_record_path(
            self._workspace_base, sandbox_id, state_base=self._state_base
        )

    def _legacy_record_path(self, sandbox_id: str) -> Path:
        # ``legacy=True`` ignores the state base on purpose: it is the pre-split
        # location *inside* the sandbox's own tree, and a record has to be
        # adopted from there no matter where the platform now writes new ones.
        return sandbox_record_path(
            self._workspace_base, sandbox_id, legacy=True, state_base=self._state_base
        )

    def _ensure_runtime_dir(self, sandbox_id: str) -> Path:
        """Create ``_runtime/<id>``, owned by the worker and closed to sandboxes.

        ``0700`` on purpose: the per-sandbox host uid is not the owner and is
        (by the E3.2 model) not in the worker's group either, so the sandbox
        cannot traverse into it -- not to read the record, and not to delete
        it. Ownership follows whoever runs the worker (root in the production
        shape), never the sandbox uid.
        """
        path = sandbox_runtime_dir(
            self._workspace_base, sandbox_id, state_base=self._state_base
        )
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

    @property
    def state_base(self) -> Path:
        """The base this registry reads and writes its *own* files under.

        The sandbox's runtime record and, beside it, the command log: platform
        state, kept where the sandbox cannot reach it. Equal to
        :attr:`workspace_base` unless ``E2B_STATE_BASE`` names a second base
        (N27); the two are *not* interchangeable, because the trees live under
        the workspace base and the platform's own files do not.
        """
        return resolve_state_base(self._workspace_base, self._state_base)

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
            self._appended.pop(sandbox_id, None)
            self._reported.pop(sandbox_id, None)
            self._pushed_total.pop(sandbox_id, None)
            self._tightened.pop(sandbox_id, None)
            self._tightened_at.pop(sandbox_id, None)
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
            self._appended.pop(sandbox_id, None)
            self._reported.pop(sandbox_id, None)
            self._pushed_total.pop(sandbox_id, None)
            self._tightened.pop(sandbox_id, None)
            self._tightened_at.pop(sandbox_id, None)
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
