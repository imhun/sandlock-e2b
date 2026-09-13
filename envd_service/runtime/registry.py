"""Sandbox runtime registry.

The control plane registers each sandbox (workspace directory, access token,
env vars, image, policy params). Records are persisted as ``sandbox.json``
inside the sandbox workspace so the envd service can run as a separate
process sharing the workspace volume.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gateway_common.paths import validate_sandbox_id


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
    volume_mounts: list[dict[str, str]] = field(default_factory=list)
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

    def _record_path(self, sandbox_id: str) -> Path:
        return self._workspace_base / sandbox_id / "sandbox.json"

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
        volume_mounts: list[dict[str, str]] | None = None,
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
            try:
                path = self._record_path(sandbox_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(record.to_dict(), separators=(",", ":")), encoding="utf-8"
                )
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
        """Parse ``<base>/<id>/sandbox.json``; ``None`` when unusable."""
        path = self._record_path(sandbox_id)
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

    def unregister(self, sandbox_id: str) -> None:
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            removed = self._records.pop(sandbox_id, None) is not None
            self._activity.pop(sandbox_id, None)
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

    def set_state(self, sandbox_id: str, state: str) -> None:
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            record = self._records.get(sandbox_id)
            if record is None:
                return
            record.state = state
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
