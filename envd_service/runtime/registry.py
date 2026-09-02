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
    memory_mb: int = 512
    cpu_percent: int = 100
    disk_mb: int = 1024
    project_id: int | None = None
    max_processes: int = 64
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

    def __init__(
        self,
        workspace_base: str | Path,
        *,
        uid_pool=None,
    ) -> None:
        self._workspace_base = Path(workspace_base)
        self._records: dict[str, RuntimeSandbox] = {}
        self._lock = threading.Lock()
        self._unregister_callbacks: list[Callable[[str], None]] = []
        self._state_callbacks: list[Callable[[str, str], None]] = []
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

    def _record_path(self, sandbox_id: str) -> Path:
        return self._workspace_base / sandbox_id / "sandbox.json"

    def register(
        self,
        *,
        sandbox_id: str,
        access_token: str,
        workspace_dir: str,
        env_vars: dict[str, str] | None = None,
        base_image: str | None = None,
        host_uid: int | None = None,
        memory_mb: int = 512,
        cpu_percent: int = 100,
        disk_mb: int = 1024,
        project_id: int | None = None,
        max_processes: int = 64,
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
        # Filesystem-backed lookup (separate-process deployment).
        try:
            path = self._record_path(sandbox_id)
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = RuntimeSandbox.from_dict(payload)
            with self._lock:
                self._records[sandbox_id] = record
            return record
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def unregister(self, sandbox_id: str) -> None:
        if not validate_sandbox_id(sandbox_id):
            return
        with self._lock:
            removed = self._records.pop(sandbox_id, None) is not None
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
