"""In-memory sandbox registry with quota reservation."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from gateway_common.ids import (
    access_token,
    client_id,
    sandbox_id as _gen_sandbox_id,
)
from gateway_common.paths import validate_sandbox_id
from gateway_common.timeutil import to_iso_z, utcnow

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[assignment]


class UnknownSandboxError(KeyError):
    """Raised when a sandbox ID is not in the registry."""


class ResourceUnavailableError(RuntimeError):
    """Raised when total resource admission rejects a sandbox creation."""


class UnknownTemplateError(ValueError):
    """Raised when the requested template is unknown."""


class SandboxStateConflictError(RuntimeError):
    """Raised on pause/resume state conflicts."""


@dataclass
class SandboxRecord:
    template_id: str
    sandbox_id: str
    client_id: str
    envd_version: str = "0.6.4+sandlock"
    envd_access_token: str = ""
    traffic_access_token: str | None = None
    domain: str = "localhost"
    started_at: datetime = field(default_factory=utcnow)
    end_at: datetime = field(default_factory=lambda: utcnow() + timedelta(seconds=300))
    cpu_count: int = 1
    memory_mb: int = 512
    disk_size_mb: int = 1024
    metadata: dict[str, str] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    state: str = "running"
    allow_internet_access: bool = False
    alias: str = "base"
    workspace_dir: Path | None = None
    base_image: str | None = None
    max_processes: int = 64
    secure: bool = True
    volume_mounts: list[dict[str, str]] = field(default_factory=list)
    mcp: dict[str, Any] | None = None
    network: dict[str, Any] | None = None
    iam_tokens: dict[str, dict[str, str]] = field(default_factory=dict)
    logs: list[dict[str, str]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    node_id: str = "local"

    def refresh(self, timeout: int) -> None:
        self.end_at = utcnow() + timedelta(seconds=max(1, timeout))

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.end_at

    def append_log(self, line: str, limit: int = 500) -> None:
        self.logs.append({"timestamp": to_iso_z(utcnow()), "line": line})
        if len(self.logs) > limit:
            self.logs = self.logs[-limit:]

    def sample_metric(self) -> dict[str, Any]:
        used = 0
        if self.workspace_dir is not None:
            for root, _dirs, files in os.walk(self.workspace_dir):
                for name in files:
                    try:
                        used += (Path(root) / name).stat().st_size
                    except OSError:
                        pass
        return {
            "timestamp": to_iso_z(utcnow()),
            "timestampUnix": int(time.time()),
            "cpuCount": self.cpu_count,
            "cpuUsedPct": 0.0,
            "memUsed": 0,
            "memTotal": self.memory_mb * 1024 * 1024,
            "memCache": 0,
            "diskUsed": used,
            "diskTotal": self.disk_size_mb * 1024 * 1024,
        }

    def pause(self) -> None:
        if self.state == "paused":
            raise SandboxStateConflictError("Sandbox is already paused")
        self.state = "paused"
        self.append_log("sandbox paused")

    def resume(self, timeout: int | None = None) -> None:
        if self.state == "running":
            raise SandboxStateConflictError("Sandbox is already running")
        self.state = "running"
        if timeout:
            self.refresh(timeout)
        self.append_log("sandbox resumed")

    def as_sandbox(self) -> dict[str, Any]:
        """Official ``Sandbox`` response JSON."""
        return {
            "templateID": self.template_id,
            "sandboxID": self.sandbox_id,
            "clientID": self.client_id,
            "envdVersion": self.envd_version,
            "envdAccessToken": self.envd_access_token,
            "trafficAccessToken": self.traffic_access_token,
            "domain": self.domain,
        }

    def as_listed(self) -> dict[str, Any]:
        return {
            "templateID": self.template_id,
            "alias": self.alias,
            "sandboxID": self.sandbox_id,
            "clientID": self.client_id,
            "startedAt": to_iso_z(self.started_at),
            "endAt": to_iso_z(self.end_at),
            "cpuCount": self.cpu_count,
            "memoryMB": self.memory_mb,
            "diskSizeMB": self.disk_size_mb,
            "metadata": self.metadata,
            "state": self.state,
            "envdVersion": self.envd_version,
        }

    def as_detail(self) -> dict[str, Any]:
        detail = self.as_listed()
        detail.update(
            {
                "envdAccessToken": self.envd_access_token,
                "allowInternetAccess": self.allow_internet_access,
                "domain": self.domain,
                "lifecycle": {"autoResume": False, "onTimeout": "kill"},
                "network": self.network or {},
                "volumeMounts": [],
            }
        )
        return detail

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "sandbox_id": self.sandbox_id,
            "client_id": self.client_id,
            "envd_version": self.envd_version,
            "envd_access_token": self.envd_access_token,
            "traffic_access_token": self.traffic_access_token,
            "domain": self.domain,
            "started_at": to_iso_z(self.started_at),
            "end_at": to_iso_z(self.end_at),
            "cpu_count": self.cpu_count,
            "memory_mb": self.memory_mb,
            "disk_size_mb": self.disk_size_mb,
            "metadata": self.metadata,
            "env_vars": self.env_vars,
            "state": self.state,
            "allow_internet_access": self.allow_internet_access,
            "alias": self.alias,
            "base_image": self.base_image,
            "max_processes": self.max_processes,
            "secure": self.secure,
            "volume_mounts": self.volume_mounts,
            "mcp": self.mcp,
            "network": self.network,
            "iam_tokens": self.iam_tokens,
            "node_id": self.node_id,
            "workspace_dir": (
                str(self.workspace_dir) if self.workspace_dir is not None else None
            ),
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "SandboxRecord":
        from datetime import datetime as _dt

        def _parse(value: str) -> datetime:
            try:
                return _dt.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return utcnow()

        return cls(
            template_id=data["template_id"],
            sandbox_id=data["sandbox_id"],
            client_id=data["client_id"],
            envd_version=data.get("envd_version", "0.6.4+sandlock"),
            envd_access_token=data.get("envd_access_token", ""),
            traffic_access_token=data.get("traffic_access_token"),
            domain=data.get("domain", "localhost"),
            started_at=_parse(data["started_at"]),
            end_at=_parse(data["end_at"]),
            cpu_count=int(data.get("cpu_count", 1)),
            memory_mb=int(data.get("memory_mb", 512)),
            disk_size_mb=int(data.get("disk_size_mb", 1024)),
            metadata=dict(data.get("metadata", {})),
            env_vars=dict(data.get("env_vars", {})),
            state=data.get("state", "running"),
            allow_internet_access=bool(data.get("allow_internet_access", False)),
            alias=data.get("alias", "base"),
            base_image=data.get("base_image"),
            max_processes=int(data.get("max_processes", 64)),
            secure=bool(data.get("secure", True)),
            volume_mounts=list(data.get("volume_mounts", [])),
            mcp=data.get("mcp"),
            network=data.get("network"),
            iam_tokens=dict(data.get("iam_tokens", {})),
            node_id=data.get("node_id", "local"),
            workspace_dir=(
                Path(data["workspace_dir"]) if data.get("workspace_dir") else None
            ),
        )


class SandboxRegistry:
    """Thread-safe in-memory registry.

    ``lock`` must be an ``asyncio.Lock`` when used from async code and a
    ``threading.Lock`` otherwise; both expose ``async with``-compatible
    behavior through the provided helper methods which take no lock argument.
    """

    def __init__(self, settings, redis_client=None, namespace: str = "e2b") -> None:
        self._settings = settings
        self._sandboxes: dict[str, SandboxRecord] = {}
        self._reserved_memory = 0
        self._reserved_cpu = 0
        self._reserved_disk = 0
        self._reserved_processes = 0
        self._migration_locks: dict[str, tuple[str, float]] = {}
        self._pending: dict[str, tuple[dict[str, Any], float]] = {}
        self._on_removed_callbacks: list[Callable[[SandboxRecord], None]] = []
        self._lock = threading.Lock()
        self._redis = None
        self._quota_store = None
        self._record_store = None
        self._ns = namespace
        if redis_client is not None:
            from control_plane.registry.redis_backend import (
                RedisQuotaStore,
                RedisRecordStore,
            )

            self._redis = redis_client
            self._quota_store = RedisQuotaStore(redis_client, namespace)
            self._record_store = RedisRecordStore(redis_client, namespace)

    def add_on_removed(self, callback: Callable[[SandboxRecord], None]) -> None:
        self._on_removed_callbacks.append(callback)

    # -- migration lock ------------------------------------------------------

    def try_acquire_migration(
        self, sandbox_id: str, ttl: int = 600
    ) -> str | None:
        """Atomically claim ``sandbox_id`` for migration.

        Returns an owner token on success, or ``None`` when another replica
        is already migrating the sandbox (callers should answer 409). The
        marker expires after ``ttl`` seconds so a crashed replica cannot
        block migrations forever.
        """
        token = secrets.token_hex(16)
        if self._redis is not None:
            key = f"{self._ns}:migrate:{sandbox_id}"
            if self._redis.set(key, token, nx=True, ex=ttl):
                return token
            return None
        with self._lock:
            now = time.monotonic()
            existing = self._migration_locks.get(sandbox_id)
            if existing is not None and existing[1] > now:
                return None
            self._migration_locks[sandbox_id] = (token, now + ttl)
            return token

    def release_migration(self, sandbox_id: str, token: str) -> None:
        """Release a migration lock previously acquired with ``token``.

        Ownership is verified so a stale caller (whose lock expired and was
        re-acquired) cannot release another replica's lock.
        """
        if self._redis is not None:
            key = f"{self._ns}:migrate:{sandbox_id}"
            # Compare-and-delete under WATCH (equivalent to a Lua script,
            # and supported by both real Redis and fakeredis).
            with self._redis.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(key)
                        current = pipe.get(key)
                        if current is not None and current != token.encode():
                            pipe.unwatch()
                            return
                        pipe.multi()
                        pipe.delete(key)
                        pipe.execute()
                        return
                    except redis.WatchError:  # type: ignore[attr-defined]
                        continue
                    except redis.exceptions.WatchError:  # pragma: no cover
                        continue
            return
        with self._lock:
            existing = self._migration_locks.get(sandbox_id)
            if existing is not None and existing[0] == token:
                del self._migration_locks[sandbox_id]

    # -- pending create (idempotent X-Sandbox-Id slow path) -----------------

    def claim_pending(
        self, sandbox_id: str, payload: dict[str, Any], ttl: int = 300
    ) -> bool:
        """Atomically claim the idempotent-create marker for ``sandbox_id``.

        Returns ``False`` when another request already owns the pending
        marker (callers should wait for it to resolve).
        """
        if self._redis is not None:
            key = f"{self._ns}:pending:{sandbox_id}"
            return bool(
                self._redis.set(
                    key, json.dumps(payload, separators=(",", ":")), nx=True, ex=ttl
                )
            )
        with self._lock:
            now = time.monotonic()
            existing = self._pending.get(sandbox_id)
            if existing is not None and existing[1] > now:
                return False
            self._pending[sandbox_id] = (payload, now + ttl)
            return True

    def get_pending(self, sandbox_id: str) -> dict[str, Any] | None:
        if self._redis is not None:
            raw = self._redis.get(f"{self._ns}:pending:{sandbox_id}")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
        with self._lock:
            entry = self._pending.get(sandbox_id)
            if entry is None:
                return None
            payload, deadline = entry
            if time.monotonic() > deadline:
                self._pending.pop(sandbox_id, None)
                return None
            return payload

    def release_pending(self, sandbox_id: str) -> None:
        if sandbox_id is None:
            return
        if self._redis is not None:
            self._redis.delete(f"{self._ns}:pending:{sandbox_id}")
            return
        with self._lock:
            self._pending.pop(sandbox_id, None)

    # -- quota ------------------------------------------------------------

    def _quota_allows_locked(
        self, memory_mb: int, cpu: int, disk_mb: int, processes: int
    ) -> bool:
        s = self._settings
        if s.max_sandboxes > 0 and len(self._sandboxes) >= s.max_sandboxes:
            return False
        if (
            s.max_total_memory_mb > 0
            and self._reserved_memory + memory_mb > s.max_total_memory_mb
        ):
            return False
        if (
            s.max_total_cpu_percent > 0
            and self._reserved_cpu + cpu > s.max_total_cpu_percent
        ):
            return False
        if (
            s.max_total_disk_mb > 0
            and self._reserved_disk + disk_mb > s.max_total_disk_mb
        ):
            return False
        if (
            s.max_total_processes > 0
            and self._reserved_processes + processes > s.max_total_processes
        ):
            return False
        return True

    def _reserve(self, record: SandboxRecord) -> None:
        self._sandboxes[record.sandbox_id] = record
        self._reserved_memory += record.memory_mb
        self._reserved_cpu += record.cpu_count * 100
        self._reserved_disk += record.disk_size_mb
        self._reserved_processes += record.max_processes

    def _release(self, record: SandboxRecord) -> None:
        if self._quota_store is not None:
            self._quota_store.release(
                "global",
                {
                    "memory": record.memory_mb,
                    "cpu": record.cpu_count * 100,
                    "disk": record.disk_size_mb,
                    "processes": record.max_processes,
                },
            )
            self._record_store.delete(record.sandbox_id)
        with self._lock:
            self._sandboxes.pop(record.sandbox_id, None)
            self._reserved_memory = max(0, self._reserved_memory - record.memory_mb)
            self._reserved_cpu = max(0, self._reserved_cpu - record.cpu_count * 100)
            self._reserved_disk = max(0, self._reserved_disk - record.disk_size_mb)
            self._reserved_processes = max(
                0, self._reserved_processes - record.max_processes
            )
            callbacks = list(self._on_removed_callbacks)
        for callback in callbacks:
            try:
                callback(record)
            except Exception:  # pragma: no cover - defensive
                pass

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        *,
        template_id: str,
        sandbox_id: str | None = None,
        timeout: int,
        metadata: dict[str, str],
        env_vars: dict[str, str],
        secure: bool,
        allow_internet_access: bool,
        base_image: str | None,
        volume_mounts: list[dict[str, str]] | None = None,
        mcp: dict[str, Any] | None = None,
        network: dict[str, Any] | None = None,
        iam_tokens: dict[str, dict[str, str]] | None = None,
    ) -> SandboxRecord:
        s = self._settings
        if sandbox_id is not None and not validate_sandbox_id(sandbox_id):
            raise ValueError("sandbox_id must be a valid sandbox id")
        timeout = timeout if timeout is not None else s.default_timeout
        if timeout < 1:
            raise ValueError("timeout must be a positive integer")

        memory_mb = s.default_memory_mb
        cpu = s.default_cpu_percent
        disk_mb = s.default_disk_mb
        processes = s.default_max_processes

        limits = {
            "memory": self._settings.max_total_memory_mb,
            "cpu": self._settings.max_total_cpu_percent,
            "disk": self._settings.max_total_disk_mb,
            "processes": self._settings.max_total_processes,
        }
        dims = {
            "memory": memory_mb,
            "cpu": cpu,
            "disk": disk_mb,
            "processes": processes,
        }
        if self._quota_store is not None:
            if not self._quota_store.reserve("global", limits, dims):
                raise ResourceUnavailableError("No resources available")
        else:
            with self._lock:
                if not self._quota_allows_locked(memory_mb, cpu, disk_mb, processes):
                    raise ResourceUnavailableError("No resources available")
            now = utcnow()
            record = SandboxRecord(
                template_id=template_id,
                sandbox_id=sandbox_id or _gen_sandbox_id(),
                client_id=client_id(),
                envd_access_token=access_token() if secure else "",
                started_at=now,
                end_at=now + timedelta(seconds=max(1, timeout)),
                memory_mb=memory_mb,
                disk_size_mb=disk_mb,
                metadata=dict(metadata or {}),
                env_vars=dict(env_vars or {}),
                allow_internet_access=bool(allow_internet_access),
                alias=template_id,
                base_image=base_image,
                max_processes=processes,
                secure=bool(secure),
                volume_mounts=list(volume_mounts or []),
                mcp=dict(mcp) if mcp else None,
                network=dict(network) if network else None,
                iam_tokens=dict(iam_tokens or {}),
            )
            self._reserve(record)
        if self._quota_store is not None:
            now = utcnow()
            record = SandboxRecord(
                template_id=template_id,
                sandbox_id=sandbox_id or _gen_sandbox_id(),
                client_id=client_id(),
                envd_access_token=access_token() if secure else "",
                started_at=now,
                end_at=now + timedelta(seconds=max(1, timeout)),
                memory_mb=memory_mb,
                disk_size_mb=disk_mb,
                metadata=dict(metadata or {}),
                env_vars=dict(env_vars or {}),
                allow_internet_access=bool(allow_internet_access),
                alias=template_id,
                base_image=base_image,
                max_processes=processes,
                secure=bool(secure),
                volume_mounts=list(volume_mounts or []),
                mcp=dict(mcp) if mcp else None,
                network=dict(network) if network else None,
                iam_tokens=dict(iam_tokens or {}),
            )
            self._record_store.put(
                record.sandbox_id,
                record.to_storage_dict(),
                ttl=None,  # reaped by remove_expired, which releases quota
            )
            with self._lock:
                self._sandboxes[record.sandbox_id] = record
        return record

    def get(self, sandbox_id: str) -> SandboxRecord:
        if not validate_sandbox_id(sandbox_id):
            raise UnknownSandboxError(sandbox_id)
        if self._record_store is not None:
            # Redis-backed registries always read the shared store so records
            # deleted or updated by another replica are visible immediately.
            payload = self._record_store.get(sandbox_id)
            if payload is None:
                raise UnknownSandboxError(sandbox_id)
            record = SandboxRecord.from_storage_dict(payload)
            with self._lock:
                self._sandboxes[sandbox_id] = record
            return record
        record = self._sandboxes.get(sandbox_id)
        if record is None:
            raise UnknownSandboxError(sandbox_id)
        return record

    def get_or_expired(self, sandbox_id: str) -> SandboxRecord | None:
        try:
            return self.get(sandbox_id)
        except UnknownSandboxError:
            return None

    def delete(self, sandbox_id: str) -> SandboxRecord:
        record = self.get(sandbox_id)
        self._release(record)
        return record

    def save(self, record: SandboxRecord) -> SandboxRecord:
        """Persist a mutated record (node_id, timeout, state, ...).

        In-memory registries already share the record object, so this is a
        no-op for them; Redis-backed registries must push every mutation to
        the shared store so other replicas observe it.
        """
        with self._lock:
            self._sandboxes[record.sandbox_id] = record
        if self._record_store is not None:
            self._record_store.put(
                record.sandbox_id, record.to_storage_dict(), ttl=None
            )
        return record

    def connect(self, sandbox_id: str, timeout: int) -> SandboxRecord:
        record = self.get(sandbox_id)
        record.refresh(timeout or self._settings.default_timeout)
        self.save(record)
        return record

    def set_timeout(self, sandbox_id: str, timeout: int) -> SandboxRecord:
        if timeout < 1:
            raise ValueError("timeout must be a positive integer")
        record = self.get(sandbox_id)
        record.refresh(timeout)
        self.save(record)
        return record

    # -- listing ----------------------------------------------------------

    def list(
        self,
        *,
        metadata_filter: dict[str, str] | None = None,
        state_filter: list[str] | None = None,
        order: str = "desc",
        started_after: datetime | None = None,
        template: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SandboxRecord]:
        if self._record_store is not None:
            records = []
            for sandbox_id in self._record_store.keys():
                try:
                    records.append(self.get(sandbox_id))
                except UnknownSandboxError:
                    continue
        else:
            records = list(self._sandboxes.values())
        if metadata_filter:
            records = [
                r
                for r in records
                if all(r.metadata.get(k) == v for k, v in metadata_filter.items())
            ]
        if state_filter is not None:
            records = [r for r in records if r.state in state_filter]
        if started_after is not None:
            records = [r for r in records if r.started_at > started_after]
        if template is not None:
            records = [r for r in records if r.template_id == template]
        records.sort(key=lambda r: r.started_at, reverse=(order != "asc"))
        if limit is not None and limit > 0:
            records = records[offset : offset + limit]
        return records

    def count(self) -> int:
        return len(self._sandboxes)

    def remove_expired(self, now: datetime | None = None) -> list[SandboxRecord]:
        if self._record_store is not None:
            expired = []
            for sandbox_id in self._record_store.keys():
                try:
                    record = self.get(sandbox_id)
                except UnknownSandboxError:
                    continue
                if record.is_expired(now):
                    expired.append(record)
                    self._release(record)
            return expired
        now = now or utcnow()
        expired = [r for r in self._sandboxes.values() if r.is_expired(now)]
        for record in expired:
            self._release(record)
        return expired

    def cleanup_workspace(self, record: SandboxRecord) -> None:
        if record.workspace_dir is not None:
            shutil.rmtree(record.workspace_dir, ignore_errors=True)
