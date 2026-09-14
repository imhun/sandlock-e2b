"""In-memory sandbox registry with quota reservation."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

logger = logging.getLogger(__name__)


#: Type tag written into every sandbox record. The shared
#: ``e2b:record:<id>`` store also holds volume records (both registries are
#: built with the same namespace), so the read paths have to tell them apart.
#: Legacy records predate the tag, which is why ``_is_sandbox_record_payload``
#: only uses it to *confirm* the shape, never to reject a record outright.
RECORD_KIND_SANDBOX = "sandbox"


def _is_sandbox_record_payload(payload: Any) -> bool:
    """Whether a stored payload is a sandbox record (and not a volume's).

    A volume record carries ``volume_id`` and no ``template_id``; parsing one
    as a sandbox record raised ``KeyError`` out of every enumeration path
    (``list`` / ``tenant_usage`` / ``remove_expired``), which made
    ``GET /sandboxes`` answer 500 and stopped the TTL sweep for as long as any
    volume existed (measured on the deployed stack, 2026-09-12).
    """
    if not isinstance(payload, dict):
        return False
    kind = payload.get("kind")
    if kind is not None:
        return kind == RECORD_KIND_SANDBOX
    return "sandbox_id" in payload and "volume_id" not in payload


class UnknownSandboxError(KeyError):
    """Raised when a sandbox ID is not in the registry."""


class ResourceUnavailableError(RuntimeError):
    """Raised when total resource admission rejects a sandbox creation."""


class UnknownTemplateError(ValueError):
    """Raised when the requested template is unknown."""


class SandboxStateConflictError(RuntimeError):
    """Raised on pause/resume state conflicts."""


# Eviction priority range accepted from clients (E9.3). Lower priority
# sandboxes are evicted first when the fleet is out of capacity.
PRIORITY_MIN = 0
PRIORITY_MAX = 10
PRIORITY_DEFAULT = 5

#: E9.3: hard cap for the in-memory eviction-notice table (the Redis-backed
#: table is bounded by its key TTL instead). Lazy expiry drops stale entries,
#: and the cap drops the oldest entry so a flood of evictions cannot grow the
#: table without bound in a single process.
_MAX_EVICTION_NOTICES = 10_000

#: E9.3: canonical eviction reason used in logs, notices and the 404 payload.
EVICTION_REASON = "evicted-idle"


def _safe_priority(value: object) -> int:
    """Coerce a supplied/stored ``priority`` into the accepted range.

    Reading a record must never fail because of a malformed stored value,
    so out-of-range or non-numeric input falls back to the default.
    """
    try:
        priority = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return PRIORITY_DEFAULT
    return max(PRIORITY_MIN, min(PRIORITY_MAX, priority))


@dataclass
class SandboxRecord:
    template_id: str
    sandbox_id: str
    client_id: str
    tenant_id: str | None = None
    envd_version: str = "0.6.4+sandlock"
    envd_access_token: str = ""
    traffic_access_token: str | None = None
    domain: str = "localhost"
    started_at: datetime = field(default_factory=utcnow)
    end_at: datetime = field(default_factory=lambda: utcnow() + timedelta(seconds=300))
    cpu_count: int = 1
    memory_mb: int = 1024
    disk_size_mb: int = 1024
    metadata: dict[str, str] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    state: str = "running"
    allow_internet_access: bool = False
    alias: str = "base"
    workspace_dir: Path | None = None
    base_image: str | None = None
    max_processes: int = 256
    secure: bool = True
    volume_mounts: list[dict[str, str]] = field(default_factory=list)
    mcp: dict[str, Any] | None = None
    network: dict[str, Any] | None = None
    iam_tokens: dict[str, dict[str, str]] = field(default_factory=dict)
    logs: list[dict[str, str]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    node_id: str = "local"
    #: E9.1: wall-clock of the last activity observed for this sandbox —
    #: a control-plane API call on the sandbox, or a worker heartbeat that
    #: reported in-sandbox traffic (commands, file access, HTTP). Feeds the
    #: idle threshold used by resource-driven eviction (E9.3).
    last_active_at: datetime = field(default_factory=utcnow)
    #: E9.3: eviction priority, 0-10 (default 5). When the fleet runs out of
    #: capacity, idle sandboxes with the *lowest* priority are evicted first.
    priority: int = 5
    #: E9.2: this record's admission reservation was released (it is paused).
    #: Owned by the registry (``pause`` / ``resume`` / ``_release``), never by
    #: the record's own state helpers. Records created before E9.2 read as
    #: ``False``, so an upgrade cannot release a reservation twice.
    quota_released: bool = False

    def refresh(self, timeout: int) -> None:
        self.end_at = utcnow() + timedelta(seconds=max(1, timeout))

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.end_at

    def touch(self, when: datetime | None = None) -> bool:
        """Mark the sandbox active at ``when`` (default: now).

        The timestamp only ever moves forward: activity reports arrive out of
        order (worker heartbeats every few seconds, concurrent API calls) and
        a stale report must never make a busy sandbox look idle. Returns
        ``True`` when the record actually changed.
        """
        moment = when or utcnow()
        if moment.tzinfo is None:  # defensive: callers may pass naive times
            moment = moment.replace(tzinfo=timezone.utc)
        if moment <= self.last_active_at:
            return False
        self.last_active_at = moment
        return True

    def idle_seconds(self, now: datetime | None = None) -> float:
        """Seconds since the last observed activity (never negative)."""
        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return max(0.0, (now - self.last_active_at).total_seconds())

    def is_idle(self, threshold_s: int, now: datetime | None = None) -> bool:
        """Idle = quieter than ``threshold_s`` (E2B_SANDBOX_IDLE_THRESHOLD_S).

        A threshold of ``0`` disables idleness entirely: nothing is ever
        considered idle, so nothing is ever an eviction candidate.
        """
        if threshold_s <= 0:
            return False
        return self.idle_seconds(now) > threshold_s

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
            # E9.1/E9.3 additions (informational; official SDKs ignore
            # unknown fields, and the eviction UI needs both to be listable).
            "lastActiveAt": to_iso_z(self.last_active_at),
            "priority": self.priority,
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
            # Shared-store type tag (see ``_is_sandbox_record_payload``).
            "kind": RECORD_KIND_SANDBOX,
            "template_id": self.template_id,
            "sandbox_id": self.sandbox_id,
            "client_id": self.client_id,
            "tenant_id": self.tenant_id,
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
            "last_active_at": to_iso_z(self.last_active_at),
            "priority": int(self.priority),
            "quota_released": bool(self.quota_released),
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
            tenant_id=data.get("tenant_id"),
            envd_version=data.get("envd_version", "0.6.4+sandlock"),
            envd_access_token=data.get("envd_access_token", ""),
            traffic_access_token=data.get("traffic_access_token"),
            domain=data.get("domain", "localhost"),
            started_at=_parse(data["started_at"]),
            end_at=_parse(data["end_at"]),
            cpu_count=int(data.get("cpu_count", 1)),
            memory_mb=int(data.get("memory_mb", 1024)),
            disk_size_mb=int(data.get("disk_size_mb", 1024)),
            metadata=dict(data.get("metadata", {})),
            env_vars=dict(data.get("env_vars", {})),
            state=data.get("state", "running"),
            allow_internet_access=bool(data.get("allow_internet_access", False)),
            alias=data.get("alias", "base"),
            base_image=data.get("base_image"),
            max_processes=int(data.get("max_processes", 256)),
            secure=bool(data.get("secure", True)),
            volume_mounts=list(data.get("volume_mounts", [])),
            mcp=data.get("mcp"),
            network=data.get("network"),
            iam_tokens=dict(data.get("iam_tokens", {})),
            node_id=data.get("node_id", "local"),
            last_active_at=_parse(data.get("last_active_at") or data.get("started_at")),
            priority=_safe_priority(data.get("priority")),
            quota_released=bool(data.get("quota_released", False)),
            workspace_dir=(
                Path(data["workspace_dir"]) if data.get("workspace_dir") else None
            ),
        )


@dataclass
class EvictionResult:
    """One sandbox acted on by :meth:`SandboxRegistry.evict_for_capacity`.

    ``record`` is the victim as it was when the action ran: for a pause it is
    still in the registry (``state == "paused"``, ``quota_released``); for a
    kill it was removed through the normal delete chain, and the record object
    is kept only so the caller can tear down the runtime afterwards.
    """

    sandbox_id: str
    action: str  # "paused" | "killed"
    reason: str = EVICTION_REASON
    at: datetime = field(default_factory=utcnow)
    record: SandboxRecord | None = None
    tenant_id: str | None = None
    priority: int = PRIORITY_DEFAULT
    idle_seconds: float = 0.0


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
        # Per-tenant reservation ledger, parallel to the global one. Only
        # populated for tenants with configured limits; dims are the same as
        # the global ledger plus a sandbox count.
        self._tenant_reserved: dict[str, dict[str, int]] = {}
        self._migration_locks: dict[str, tuple[str, float]] = {}
        self._pending: dict[str, tuple[dict[str, Any], float]] = {}
        #: E9.1: when each record's activity timestamp was last pushed to the
        #: shared store (bounds write amplification; see :meth:`mark_active`).
        self._persisted_activity: dict[str, datetime] = {}
        #: E9.3: eviction notices for killed sandboxes (in-memory fallback;
        #: Redis stores them under ``{ns}:evicted:*`` with a real TTL). Each
        #: value carries a monotonic deadline so reads expire lazily without a
        #: sweeper; the table is capped so it cannot grow without bound.
        self._eviction_notices: dict[str, dict[str, Any]] = {}
        #: E9.3: per-process eviction throttle (``eviction_min_interval_s``).
        #: Multi-replica deployments do NOT share this state (known limitation,
        #: see docs/resource-contention.md §8); injectable clock for tests.
        self._last_eviction_at = 0.0
        self._eviction_clock: Callable[[], float] = time.monotonic
        self._on_removed_callbacks: list[Callable[[SandboxRecord], None]] = []
        #: E9.4: fired after a real quota release (pause / delete / expiry)
        #: so create waiters can retry admission as soon as room exists.
        self._on_quota_released_callbacks: list[
            Callable[[SandboxRecord], None]
        ] = []
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

    def add_on_quota_released(
        self, callback: Callable[[SandboxRecord], None]
    ) -> None:
        """Register a callback fired when a record's reservation returns."""
        self._on_quota_released_callbacks.append(callback)

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

    _TENANT_DIMS = ("sandboxes", "memory", "cpu", "disk", "processes")
    _TENANT_LIMIT_KEYS = {
        "max_sandboxes": "sandboxes",
        "max_total_memory_mb": "memory",
        "max_total_cpu_percent": "cpu",
        "max_total_disk_mb": "disk",
        "max_total_processes": "processes",
    }

    def _quota_allows_locked(
        self, memory_mb: int, cpu: int, disk_mb: int, processes: int
    ) -> bool:
        s = self._settings
        # Paused sandboxes hold no reservation (E9.2), so they do not consume
        # a slot of the concurrency cap either.
        if s.max_sandboxes > 0 and self._held_count_locked() >= s.max_sandboxes:
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

    def _held_count_locked(self) -> int:
        """Records currently holding a reservation (E9.2: not paused).

        Callers must hold ``self._lock``.
        """
        return sum(1 for r in self._sandboxes.values() if not r.quota_released)

    def _tenant_limits(self, tenant_id: str | None, is_admin: bool) -> dict[str, int] | None:
        """Per-tenant admission limits keyed by ledger dim, or ``None`` when
        the tenant has no configured limits (or the caller is an admin /
        has no tenant)."""
        if is_admin or not tenant_id:
            return None
        raw = self._settings.tenant_limits.get(tenant_id)
        if not raw:
            return None
        return {
            dim: int(raw[key])
            for key, dim in self._TENANT_LIMIT_KEYS.items()
            if key in raw and raw[key] > 0
        }

    def _tenant_quota_allows_locked(
        self,
        tenant_id: str,
        limits: dict[str, int],
        memory_mb: int,
        cpu: int,
        disk_mb: int,
        processes: int,
    ) -> bool:
        used = self._tenant_reserved.setdefault(
            tenant_id, {dim: 0 for dim in self._TENANT_DIMS}
        )
        demand = {
            "sandboxes": 1,
            "memory": memory_mb,
            "cpu": cpu,
            "disk": disk_mb,
            "processes": processes,
        }
        return all(
            used[dim] + demand[dim] <= limit
            for dim, limit in limits.items()
        )

    def _tenant_dims(self, record: SandboxRecord) -> dict[str, int]:
        return {
            "sandboxes": 1,
            "memory": record.memory_mb,
            "cpu": record.cpu_count * 100,
            "disk": record.disk_size_mb,
            "processes": record.max_processes,
        }

    def _global_dims(self, record: SandboxRecord) -> dict[str, int]:
        """The admission dimensions one record occupies (global and node)."""
        return {
            "memory": record.memory_mb,
            "cpu": record.cpu_count * 100,
            "disk": record.disk_size_mb,
            "processes": record.max_processes,
        }

    def _global_limits(self) -> dict[str, int]:
        s = self._settings
        return {
            "memory": s.max_total_memory_mb,
            "cpu": s.max_total_cpu_percent,
            "disk": s.max_total_disk_mb,
            "processes": s.max_total_processes,
        }

    # -- pause / resume accounting (E9.2) ---------------------------------

    def release_quota(self, record: SandboxRecord) -> bool:
        """Give back ``record``'s admission reservation (paused sandbox).

        Idempotent: a record whose reservation is already out (or a record
        created before E9.2 that was never accounted for) returns ``False``
        and changes nothing, so the delete path can never underflow the
        ledgers by releasing twice.
        """
        if record.quota_released:
            return False
        dims = self._global_dims(record)
        if self._quota_store is not None:
            self._quota_store.release("global", dims)
            tenant_limits = self._tenant_limits(record.tenant_id, is_admin=False)
            if tenant_limits is not None:
                self._quota_store.release(
                    f"tenant:{record.tenant_id}", self._tenant_dims(record)
                )
        else:
            with self._lock:
                self._reserved_memory = max(0, self._reserved_memory - record.memory_mb)
                self._reserved_cpu = max(0, self._reserved_cpu - record.cpu_count * 100)
                self._reserved_disk = max(
                    0, self._reserved_disk - record.disk_size_mb
                )
                self._reserved_processes = max(
                    0, self._reserved_processes - record.max_processes
                )
                if record.tenant_id:
                    used = self._tenant_reserved.get(record.tenant_id)
                    if used is not None:
                        for dim, value in self._tenant_dims(record).items():
                            used[dim] = max(0, used[dim] - value)
        record.quota_released = True
        with self._lock:
            callbacks = list(self._on_quota_released_callbacks)
        # Wake capacity waiters (E9.4) *only* on the real-release path, never
        # on the idempotent no-op above. Callbacks may run outside the event
        # loop (TTLSweeper removal chain), so each hook must be thread-safe.
        for callback in callbacks:
            try:
                callback(record)
            except Exception:  # pragma: no cover - defensive
                pass
        return True

    def hold_quota(self, record: SandboxRecord) -> bool:
        """Re-acquire a reservation released by :meth:`release_quota`.

        Raises :class:`ResourceUnavailableError` when the fleet (or the
        tenant ledger) has no room; the record is left untouched so the
        caller can keep it paused. Returns ``True`` when the reservation was
        taken by this call.
        """
        if not record.quota_released:
            return False
        dims = self._global_dims(record)
        tenant_limits = self._tenant_limits(record.tenant_id, is_admin=False)
        if self._quota_store is not None:
            if not self._quota_store.reserve("global", self._global_limits(), dims):
                raise ResourceUnavailableError("No resources available")
            if tenant_limits is not None and not self._quota_store.reserve(
                f"tenant:{record.tenant_id}", tenant_limits, self._tenant_dims(record)
            ):
                self._quota_store.release("global", dims)
                raise ResourceUnavailableError("tenant quota exceeded")
            record.quota_released = False
            return True
        with self._lock:
            if not self._quota_allows_locked(
                dims["memory"], dims["cpu"], dims["disk"], dims["processes"]
            ):
                raise ResourceUnavailableError("No resources available")
            if tenant_limits is not None and not self._tenant_quota_allows_locked(
                record.tenant_id,
                tenant_limits,
                dims["memory"],
                dims["cpu"],
                dims["disk"],
                dims["processes"],
            ):
                raise ResourceUnavailableError("tenant quota exceeded")
            self._reserved_memory += dims["memory"]
            self._reserved_cpu += dims["cpu"]
            self._reserved_disk += dims["disk"]
            self._reserved_processes += dims["processes"]
            if record.tenant_id:
                used = self._tenant_reserved.setdefault(
                    record.tenant_id, {dim: 0 for dim in self._TENANT_DIMS}
                )
                for dim, value in self._tenant_dims(record).items():
                    used[dim] += value
        record.quota_released = False
        return True

    def pause(self, record: SandboxRecord) -> SandboxRecord:
        """Pause ``record`` and release its admission reservation (E9.2).

        A paused sandbox stops counting against the global, tenant and node
        pools, which is what makes "hibernate the idle ones to make room"
        (E9.3) worth doing: the processes are frozen on the worker and the
        workspace stays, but the capacity is bookable again.
        """
        record.pause()
        self.release_quota(record)
        self.save(record)
        return record

    def resume(
        self, record: SandboxRecord, timeout: int | None = None
    ) -> SandboxRecord:
        """Resume a paused sandbox, re-acquiring capacity first.

        Admission happens *before* the state flip: with no room the caller
        gets ``ResourceUnavailableError`` (→ 503 / queue) and the sandbox
        stays paused instead of running unaccounted.
        """
        acquired = self.hold_quota(record)
        try:
            record.resume(timeout)
        except SandboxStateConflictError:
            if acquired:
                self.release_quota(record)
            raise
        self.save(record)
        return record

    # -- eviction (E9.3) --------------------------------------------------

    def eviction_candidates(
        self,
        *,
        tenant_id: str | None = None,
        is_admin: bool = False,
        exclude_ids: tuple[str, ...] = (),
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[SandboxRecord]:
        """Idle ``running`` victims in eviction order (pure selection).

        No side effects, so selection can be unit-tested in isolation. Only
        records that are ``running`` AND idle past ``sandbox_idle_threshold_s``
        qualify: ``paused`` records already released their reservation and
        ``orphaned`` records sit on a node the control plane lost (E6.1) —
        neither may be evicted. Ordering is priority (low first) -> idle
        oldest -> tenant weight -> ``sandbox_id`` for determinism.

        Cross-tenant protection: unless the caller is an admin or
        ``eviction_cross_tenant`` is enabled, only victims of the requester's
        own tenant are returned — otherwise any tenant could use "create a
        sandbox" to evict other tenants' idle sandboxes (denial of service).
        """
        settings = self._settings
        if not settings.eviction_enabled or settings.sandbox_idle_threshold_s <= 0:
            return []
        moment = now or utcnow()
        cross_tenant = is_admin or bool(settings.eviction_cross_tenant)
        candidates: list[SandboxRecord] = []
        for record in self.list():
            if record.state != "running":
                continue
            if record.sandbox_id in exclude_ids:
                continue
            if not record.is_idle(settings.sandbox_idle_threshold_s, moment):
                continue
            if not cross_tenant and record.tenant_id != tenant_id:
                # 默认只踢请求者自己的租户（无租户请求只看无租户记录）。
                continue
            candidates.append(record)
        candidates.sort(
            key=lambda r: (
                r.priority,
                r.last_active_at,
                self._eviction_tenant_key(r.tenant_id),
                r.sandbox_id,
            )
        )
        if limit is not None and limit > 0:
            candidates = candidates[:limit]
        return candidates

    def _eviction_tenant_key(self, tenant_id: str | None) -> tuple[int, int]:
        """Tenant weight used in eviction ordering (E9.3).

        Tenants with a configured ``tenant_limits[tenant].max_sandboxes`` are
        evicted before tenants without one, and a smaller quota sorts first;
        unconfigured / unowned records go last.
        """
        if tenant_id is None:
            return (1, 0)
        raw = self._settings.tenant_limits.get(tenant_id) or {}
        cap = raw.get("max_sandboxes")
        if isinstance(cap, int) and cap > 0:
            return (0, cap)
        return (1, 0)

    def evict_for_capacity(
        self,
        *,
        tenant_id: str | None = None,
        is_admin: bool = False,
        exclude_ids: tuple[str, ...] = (),
        now: datetime | None = None,
        max_victims: int | None = None,
        prefer_pause: bool | None = None,
        actor: str | None = None,
        pause_action: Callable[[SandboxRecord], None] | None = None,
        kill_action: Callable[[SandboxRecord], None] | None = None,
    ) -> list[EvictionResult]:
        """Evict idle victims so a create can be retried (E9.3).

        The registry only owns admission records: pausing returns the global /
        tenant reservation and killing removes the record through the regular
        delete chain (which releases node quota via ``add_on_removed``). The
        registry never talks to worker teardown directly — the API layer parks
        node capacity / freezes the runtime through ``pause_action`` and
        destroys killed runtimes from the returned results.

        One call processes at most ``max_victims`` (default
        ``eviction_max_per_create``) candidates, never more than once per
        ``eviction_min_interval_s`` (per-process throttle; replicas do not
        share it). Returns an empty list when eviction is disabled, throttled,
        or there is no candidate, and in that case no quota changes at all.
        """
        settings = self._settings
        if not settings.eviction_enabled:
            return []
        if max_victims is None:
            max_victims = settings.eviction_max_per_create
        if prefer_pause is None:
            prefer_pause = settings.eviction_prefer_pause
        interval = settings.eviction_min_interval_s
        if interval > 0:
            with self._lock:
                clock_now = self._eviction_clock()
                if clock_now - self._last_eviction_at < interval:
                    # 防驱逐风暴：最小间隔内不再动手（进程内节流；多副本不
                    # 共享节流状态——docs/resource-contention.md §8 已知限制）。
                    return []
                self._last_eviction_at = clock_now
        candidates = self.eviction_candidates(
            tenant_id=tenant_id,
            is_admin=is_admin,
            exclude_ids=exclude_ids,
            now=now,
            limit=max_victims,
        )
        results: list[EvictionResult] = []
        for victim in candidates:
            if prefer_pause:
                self._evict_pause(victim)
                if pause_action is not None:
                    pause_action(victim)
                action = "paused"
            else:
                self._evict_kill(victim, actor=actor)
                if kill_action is not None:
                    kill_action(victim)
                action = "killed"
            moment = now or utcnow()
            results.append(
                EvictionResult(
                    sandbox_id=victim.sandbox_id,
                    action=action,
                    reason=EVICTION_REASON,
                    at=moment,
                    record=victim,
                    tenant_id=victim.tenant_id,
                    priority=victim.priority,
                    idle_seconds=victim.idle_seconds(moment),
                )
            )
            logger.warning(
                "evicted sandbox %s (reason=%s, tenant=%s, priority=%s, idle=%.0fs)",
                victim.sandbox_id,
                EVICTION_REASON,
                victim.tenant_id,
                victim.priority,
                victim.idle_seconds(moment),
            )
        return results

    def _evict_pause(self, record: SandboxRecord) -> None:
        """E9.3 pause action: keep the record, return its reservation.

        The API layer additionally returns the victim's node reservation and
        freezes the runtime through the injected ``pause_action`` hook; the
        registry itself only knows the admission ledgers.
        """
        self.pause(record)
        record.append_log(f"sandbox evicted (reason={EVICTION_REASON})")
        self.save(record)

    def _evict_kill(self, record: SandboxRecord, *, actor: str | None = None) -> None:
        """E9.3 kill action: log + persist the notice, then remove.

        The eviction log line is written and the notice stored *before* the
        record disappears, so a user that queries the id afterwards gets the
        "evicted" 404 instead of a plain not-found. Deletion goes through the
        normal ``_release`` chain (``add_on_removed`` + quota release).
        """
        record.append_log(f"sandbox evicted (reason={EVICTION_REASON})")
        self.record_eviction(
            record.sandbox_id,
            reason=EVICTION_REASON,
            actor=actor,
            tenant_id=record.tenant_id,
        )
        self._release(record)

    def record_eviction(
        self,
        sandbox_id: str,
        *,
        reason: str = EVICTION_REASON,
        at: datetime | None = None,
        actor: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """Persist a kill-eviction notice for ``sandbox_id``.

        Redis-backed registries store it under ``{ns}:evicted:*`` with a TTL of
        ``eviction_notice_ttl_s``; the in-memory fallback expires lazily on
        read and is capped at ``_MAX_EVICTION_NOTICES`` entries. ``tenant_id``
        is kept so the API can hide an eviction notice from other tenants.
        """
        moment = at or utcnow()
        notice: dict[str, Any] = {
            "reason": reason,
            "at": to_iso_z(moment),
            "actor": actor,
            "tenant_id": tenant_id,
        }
        ttl = self._settings.eviction_notice_ttl_s
        if self._redis is not None:
            key = f"{self._ns}:evicted:{sandbox_id}"
            payload = json.dumps(notice, separators=(",", ":"))
            if ttl > 0:
                self._redis.set(key, payload, ex=ttl)
            else:
                self._redis.set(key, payload)
            return
        with self._lock:
            if ttl > 0:
                notice["_expires_at"] = self._eviction_clock() + ttl
            self._eviction_notices[sandbox_id] = notice
            self._prune_eviction_notices_locked()

    def eviction_notice(self, sandbox_id: str) -> dict[str, Any] | None:
        """Return the stored eviction notice, or ``None`` (expired/absent)."""
        if self._redis is not None:
            raw = self._redis.get(f"{self._ns}:evicted:{sandbox_id}")
            if not raw:
                return None
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None
        with self._lock:
            notice = self._eviction_notices.get(sandbox_id)
            if notice is None:
                return None
            deadline = notice.get("_expires_at", 0)
            if deadline and deadline <= self._eviction_clock():
                self._eviction_notices.pop(sandbox_id, None)
                return None
            return {k: v for k, v in notice.items() if k != "_expires_at"}

    def _prune_eviction_notices_locked(self) -> None:
        """Drop expired notices, then the oldest beyond the cap.

        In-memory fallback only; callers must hold ``self._lock``.
        """
        now = self._eviction_clock()
        expired = [
            sid
            for sid, notice in self._eviction_notices.items()
            if notice.get("_expires_at", 0) and notice["_expires_at"] <= now
        ]
        for sid in expired:
            self._eviction_notices.pop(sid, None)
        while len(self._eviction_notices) > _MAX_EVICTION_NOTICES:
            oldest = next(iter(self._eviction_notices), None)
            if oldest is None:
                break
            self._eviction_notices.pop(oldest, None)

    def _reserve(self, record: SandboxRecord) -> None:
        self._sandboxes[record.sandbox_id] = record
        self._reserved_memory += record.memory_mb
        self._reserved_cpu += record.cpu_count * 100
        self._reserved_disk += record.disk_size_mb
        self._reserved_processes += record.max_processes
        if record.tenant_id:
            used = self._tenant_reserved.setdefault(
                record.tenant_id, {dim: 0 for dim in self._TENANT_DIMS}
            )
            for dim, value in self._tenant_dims(record).items():
                used[dim] += value

    def _release(self, record: SandboxRecord) -> None:
        """Drop ``record``: remove it, notify, then release what it held.

        The reservation is given back *after* the removal callbacks so they
        can still tell whether the record held one (a paused sandbox already
        released its node quota in E9.2 and must not release it twice).
        """
        if self._quota_store is not None:
            self._record_store.delete(record.sandbox_id)
        with self._lock:
            self._sandboxes.pop(record.sandbox_id, None)
            self._persisted_activity.pop(record.sandbox_id, None)
            callbacks = list(self._on_removed_callbacks)
        for callback in callbacks:
            try:
                callback(record)
            except Exception:  # pragma: no cover - defensive
                pass
        self.release_quota(record)

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
        tenant_id: str | None = None,
        is_admin: bool = False,
        priority: int = PRIORITY_DEFAULT,
    ) -> SandboxRecord:
        s = self._settings
        if sandbox_id is not None and not validate_sandbox_id(sandbox_id):
            raise ValueError("sandbox_id must be a valid sandbox id")
        timeout = timeout if timeout is not None else s.default_timeout
        if timeout < 1:
            raise ValueError("timeout must be a positive integer")
        priority = _safe_priority(priority)

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
        if is_admin:
            # Admin-created resources are unowned (visible only to admins)
            # and exempt from tenant limits.
            tenant_id = None
        tenant_limits = self._tenant_limits(tenant_id, is_admin)
        if self._quota_store is not None:
            if not self._quota_store.reserve("global", limits, dims):
                raise ResourceUnavailableError("No resources available")
            if tenant_limits is not None:
                tenant_dims = dict(dims)
                tenant_dims["sandboxes"] = 1
                if not self._quota_store.reserve(
                    f"tenant:{tenant_id}", tenant_limits, tenant_dims
                ):
                    self._quota_store.release("global", dims)
                    raise ResourceUnavailableError("tenant quota exceeded")
        else:
            with self._lock:
                if not self._quota_allows_locked(memory_mb, cpu, disk_mb, processes):
                    raise ResourceUnavailableError("No resources available")
                if tenant_limits is not None and not self._tenant_quota_allows_locked(
                    tenant_id, tenant_limits, memory_mb, cpu, disk_mb, processes
                ):
                    raise ResourceUnavailableError("tenant quota exceeded")
            now = utcnow()
            record = SandboxRecord(
                template_id=template_id,
                sandbox_id=sandbox_id or _gen_sandbox_id(),
                client_id=client_id(),
                tenant_id=tenant_id,
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
                priority=priority,
                last_active_at=now,
            )
            self._reserve(record)
        if self._quota_store is not None:
            now = utcnow()
            record = SandboxRecord(
                template_id=template_id,
                sandbox_id=sandbox_id or _gen_sandbox_id(),
                client_id=client_id(),
                tenant_id=tenant_id,
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
                priority=priority,
                last_active_at=now,
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
            if not _is_sandbox_record_payload(payload):
                # The store is shared with the volume registry: a volume id
                # (or any other foreign record) reads as "no such sandbox"
                # instead of breaking the caller.
                logger.debug(
                    "record %s in the shared store is not a sandbox record",
                    sandbox_id,
                )
                raise UnknownSandboxError(sandbox_id)
            try:
                record = SandboxRecord.from_storage_dict(payload)
            except (KeyError, TypeError, ValueError) as exc:
                # An upgrade must never require a Redis flush: a record whose
                # shape we cannot read is skipped (and named), not raised.
                logger.warning("unreadable sandbox record %s: %s", sandbox_id, exc)
                raise UnknownSandboxError(sandbox_id) from exc
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

    def list_by_node(self, node_id: str) -> list[SandboxRecord]:
        """All live sandbox records scheduled onto ``node_id``."""
        return [r for r in self.list() if r.node_id == node_id]

    def mark_orphaned(self, node_id: str) -> list[SandboxRecord]:
        """Mark every sandbox on an unhealthy node as ``orphaned`` (E6.1).

        Called by the periodic node-health sweep when a remote node stops
        heartbeating. Orphaned records are skipped by TTL expiry, so a
        partitioned worker's live sandboxes are never torn down underneath
        its running processes (which would keep the deleted inode open).
        Recovery reconciliation (``recover_node``) flips them back to
        ``running`` when the worker reconnects and reports them.

        A **``paused``** record is left exactly as it is. Orphaning it would
        protect nothing -- it already released its node/global/tenant
        reservations when it paused, and TTL expiry already skips it
        (:meth:`SandboxRegistry.remove_expired`) -- while destroying the one
        fact the resume path is built on: *paused* is what tells the control
        plane that the hosting worker may still hold a SIGSTOPped process
        tree whose thaw has to be pushed to that worker. Only
        ``connect_sandbox``'s auto-resume (the SDK's only public resume
        surface) gates on it, so a paused record the sweep flipped to
        ``orphaned`` -- and that ``recover_node`` later restored to
        ``running`` when the worker reported it -- comes back as a sandbox
        that the SDK can talk to while every command on it stays frozen
        forever. A node that loses its heartbeat while a sandbox is paused
        therefore keeps that sandbox ``paused``; the SDK's next
        ``Sandbox.connect`` re-books capacity, pushes the thaw, and (when the
        node really is gone) reads the documented best-effort transport
        caveat, exactly like every other resume.
        """
        marked: list[SandboxRecord] = []
        for record in self.list_by_node(node_id):
            if record.state == "paused":
                continue
            if record.state != "orphaned":
                record.state = "orphaned"
                record.append_log("node unreachable; sandbox orphaned")
                self.save(record)
            marked.append(record)
        return marked

    def recover_node(
        self,
        node_id: str,
        sandbox_ids: set[str],
        snapshot_ids: set[str],
        *,
        timeout: int | None = None,
    ) -> dict[str, list[str]]:
        """Reconcile control-plane records for a node against the worker's
        local runtime (E6.1 recovery path).

        ``sandbox_ids`` is the authoritative list of sandboxes the worker
        currently runs. Records the worker still has are un-orphaned (and
        refreshed); records the worker no longer has are removed — but only
        when they were part of the ``snapshot_ids`` the worker reconciled
        against. A record created/assigned to the node *after* that snapshot
        is a concurrent create and is left untouched even if the worker's
        report (computed before the create landed) does not include it.
        Returns ``{"recovered", "removed", "kept"}`` sandbox id lists so
        callers and operators can see exactly what the reconcile changed.
        """
        recovered: list[str] = []
        removed: list[str] = []
        kept: list[str] = []
        for record in self.list_by_node(node_id):
            if record.sandbox_id in sandbox_ids:
                if record.state == "orphaned":
                    record.state = "running"
                    record.append_log("worker recovered; sandbox restored")
                    recovered.append(record.sandbox_id)
                else:
                    kept.append(record.sandbox_id)
                if timeout is not None:
                    record.refresh(timeout)
                self.save(record)
            else:
                if record.sandbox_id in snapshot_ids:
                    removed.append(record.sandbox_id)
                    self.delete(record.sandbox_id)
                else:
                    # Created/assigned after the snapshot was taken: a
                    # concurrent create racing the reconcile. The worker may
                    # not have reported it yet (its diff predated the create),
                    # so it must survive; tearing it down would kill a live
                    # sandbox the control plane just scheduled here.
                    kept.append(record.sandbox_id)
        return {"recovered": recovered, "removed": removed, "kept": kept}

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
            self._persisted_activity[record.sandbox_id] = record.last_active_at
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

    # -- activity (E9.1) --------------------------------------------------

    def mark_active(
        self, record: SandboxRecord, *, when: datetime | None = None
    ) -> bool:
        """Record activity on ``record`` and persist it on a coarse interval.

        Returns ``True`` when the timestamp moved forward. The shared store is
        only written when the previous value is older than
        ``E2B_ACTIVITY_PERSIST_INTERVAL_S``: activity arrives on every
        proxied request and every worker heartbeat, while the idle threshold
        it feeds is minutes wide — sub-second accuracy buys nothing and a
        Redis write per request would not.
        """
        if not record.touch(when):
            return False
        if self._record_store is None:
            self.save(record)
            return True
        interval = self._settings.activity_persist_interval_s
        if interval <= 0:
            self.save(record)
            return True
        persisted = self._persisted_activity.get(record.sandbox_id)
        if persisted is None or (utcnow() - persisted).total_seconds() >= interval:
            self._persisted_activity[record.sandbox_id] = record.last_active_at
            self.save(record)
        return True

    def touch(self, sandbox_id: str, *, when: datetime | None = None) -> SandboxRecord:
        """Mark a sandbox active by id (raises ``UnknownSandboxError``)."""
        record = self.get(sandbox_id)
        self.mark_active(record, when=when)
        return record

    def apply_activity_report(
        self, node_id: str | None, activity: dict[str, Any] | None
    ) -> int:
        """Merge per-sandbox activity timestamps from a worker heartbeat.

        The worker sees traffic the control plane never does (commands, file
        access, in-sandbox HTTP through the gateway), so its report is the
        authoritative idle signal. Entries are ``{sandbox_id: unix_seconds}``;
        ``node_id`` guards against a worker reporting for sandboxes it does
        not host (pass ``None`` from an in-process deployment, where there is
        no node boundary). Unknown/foreign sandboxes and malformed values are
        ignored. Returns the number of records updated.
        """
        updated = 0
        for sandbox_id, value in (activity or {}).items():
            try:
                moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            try:
                record = self.get(str(sandbox_id))
            except UnknownSandboxError:
                continue
            if node_id is not None and record.node_id != node_id:
                continue
            if self.mark_active(record, when=moment):
                updated += 1
        return updated

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
        tenant_id: str | None = None,
    ) -> list[SandboxRecord]:
        if self._record_store is not None:
            records = list(self._iter_stored_records())
        else:
            records = list(self._sandboxes.values())
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
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

    def _iter_stored_records(self):
        """Yield the sandbox records held in the shared record store.

        The store keeps *both* registries under ``e2b:record:<id>``, so this
        skips anything that is not a sandbox record (volume records; a
        tombstone already comes back as ``None`` from the store) and any record
        whose shape cannot be read. One foreign record must never take out
        listing, tenant usage or the TTL sweep.
        """
        for record_id in self._record_store.keys():
            payload = self._record_store.get(record_id)
            if payload is None:
                continue
            if not _is_sandbox_record_payload(payload):
                logger.debug(
                    "skipping non-sandbox record %s in the shared store",
                    record_id,
                )
                continue
            try:
                yield self.get(record_id)
            except UnknownSandboxError:
                continue

    def tenant_usage(self) -> dict[str, dict[str, int]]:
        """Per-tenant usage from live records (independent of reservation
        ledger, so unconfigured tenants and admin-created records are also
        counted)."""
        usage: dict[str, dict[str, int]] = {}
        if self._record_store is not None:
            for record in self._iter_stored_records():
                self._accumulate_usage(usage, record)
        else:
            for record in self._sandboxes.values():
                self._accumulate_usage(usage, record)
        return usage

    @staticmethod
    def _accumulate_usage(
        usage: dict[str, dict[str, int]], record: SandboxRecord
    ) -> None:
        tenant = record.tenant_id
        entry = usage.setdefault(
            tenant,
            {"sandboxes": 0, "memoryMB": 0, "cpuPercent": 0, "diskMB": 0, "processes": 0},
        )
        entry["sandboxes"] += 1
        entry["memoryMB"] += record.memory_mb
        entry["cpuPercent"] += record.cpu_count * 100
        entry["diskMB"] += record.disk_size_mb
        entry["processes"] += record.max_processes

    def remove_expired(self, now: datetime | None = None) -> list[SandboxRecord]:
        """Reap sandboxes whose TTL elapsed and release their reservations."""
        if self._record_store is not None:
            expired = []
            for record in self._iter_stored_records():
                if self._ttl_reapable(record, now):
                    expired.append(record)
                    self._release(record)
            return expired
        now = now or utcnow()
        expired = [
            r for r in list(self._sandboxes.values()) if self._ttl_reapable(r, now)
        ]
        for record in expired:
            self._release(record)
        return expired

    @staticmethod
    def _ttl_reapable(record: SandboxRecord, now: datetime | None) -> bool:
        """Whether the TTL sweep may delete ``record``.

        Two states survive their deadline:

        * ``orphaned`` (E6.1) — the worker may still be running the sandbox,
          and deleting the workspace underneath it orphans live inodes;
        * ``paused`` (E9.2) — parking a sandbox is supposed to preserve the
          session, and a parked sandbox holds no admission reservation, so
          reaping it would buy capacity while destroying user state.
        """
        if record.state in ("orphaned", "paused"):
            return False
        return record.is_expired(now)

    def cleanup_workspace(self, record: SandboxRecord) -> None:
        if record.workspace_dir is not None:
            shutil.rmtree(record.workspace_dir, ignore_errors=True)
