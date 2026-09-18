"""Optional Redis-backed shared state for multi-replica control planes.

When ``E2B_REDIS_URL`` is configured, sandbox and node registries persist
their records in Redis and reserve/release quotas with atomic Lua scripts,
so multiple control-plane replicas share one consistent accounting ledger
(no over-commit across processes). Without Redis, registries stay purely
in-memory (single-process mode).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[assignment]

#: Marker stored under a record key to remember a deletion. It is not valid
#: JSON, so ``get()`` naturally treats the key as absent, while
#: ``is_tombstoned()`` lets backfill logic distinguish "never existed"
#: from "was deleted" (deleted must never be resurrected from disk).
TOMBSTONE = "__deleted__"


class RedisQuotaStore:
    """Atomic quota reservations shared across replicas."""

    def __init__(self, client: Any, namespace: str) -> None:
        self._client = client
        self._ns = namespace

    def _key(self, name: str) -> str:
        return f"{self._ns}:quota:{name}"

    def reserve(
        self,
        name: str,
        limits: dict[str, int],
        dims: dict[str, int],
    ) -> bool:
        """Atomically check capacity and reserve under a WATCH transaction.

        Equivalent to the Lua script used in production: concurrent replicas
        cannot both pass the check and over-commit.
        """
        key = self._key(name)
        with self._client.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.hgetall(key)
                    used = {
                        k.decode(): int(v) if isinstance(k, bytes) else int(v)
                        for k, v in raw.items()
                    }
                    for dim, limit in limits.items():
                        if limit > 0 and used.get(dim, 0) + dims[dim] > limit:
                            pipe.unwatch()
                            return False
                    pipe.multi()
                    for dim, value in dims.items():
                        pipe.hincrby(key, dim, value)
                    pipe.execute()
                    return True
                except redis.WatchError:  # type: ignore[attr-defined]
                    continue
                except redis.exceptions.WatchError:  # pragma: no cover
                    continue

    def release(self, name: str, dims: dict[str, int]) -> None:
        key = self._key(name)
        with self._client.pipeline() as pipe:
            for dim, value in dims.items():
                pipe.hincrby(key, dim, -value)
            pipe.execute()

    def get(self, name: str) -> dict[str, int]:
        """Current reserved values for a name ({} when never reserved)."""
        key = self._key(name)
        raw = self._client.hgetall(key)
        return {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in raw.items()
        }


class RedisRecordStore:
    """JSON records shared across replicas."""

    def __init__(self, client: Any, namespace: str) -> None:
        self._client = client
        self._ns = namespace

    def _key(self, record_id: str) -> str:
        return f"{self._ns}:record:{record_id}"

    def put(self, record_id: str, payload: dict[str, Any], ttl: int | None = None) -> None:
        key = self._key(record_id)
        self._client.set(key, json.dumps(payload, separators=(",", ":")))
        if ttl:
            self._client.expire(key, ttl)

    def get(self, record_id: str) -> dict[str, Any] | None:
        raw = self._client.get(self._key(record_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def delete(self, record_id: str) -> None:
        self._client.delete(self._key(record_id))

    def tombstone(self, record_id: str) -> None:
        """Keep the key but mark the record as deleted.

        Unlike ``delete``, the key stays present so a replica's stale
        on-disk copy can never be mirrored back over the deletion (see
        ``VolumeRegistry._ensure_backfilled``).
        """
        self._client.set(self._key(record_id), TOMBSTONE)

    def is_tombstoned(self, record_id: str) -> bool:
        raw = self._client.get(self._key(record_id))
        return raw == TOMBSTONE or raw == TOMBSTONE.encode()

    def keys(self) -> list[str]:
        pattern = f"{self._ns}:record:*"
        keys = [k.decode() for k in self._client.keys(pattern)]
        return [k.split(":record:", 1)[1] for k in keys]


class RedisUidLedger:
    """Fleet-wide host-uid allocations, authoritative outside the volume.

    Per-sandbox host uids used to be allocated by scanning ``sandbox.json`` in
    every sandbox tree on the shared workspace (``envd_service.uid_pool``).
    That made a **file inside the volume** the source of truth for a
    fleet-level invariant, and any root on any mounting node can rewrite such
    a file (OBS-9): editing ``host_uid`` was enough to make two sandboxes share
    a uid and remove the cross-uid isolation wall.

    This ledger keeps it in Redis instead: the uid is claimed with ``HSETNX``
    (so two replicas cannot hand out the same one), the owner is recorded in
    the reverse hash, and a release drops both. The on-disk copy stays as a
    cache for the worker's own bookkeeping.
    """

    def __init__(self, client: Any, namespace: str) -> None:
        self._client = client
        self._ns = namespace

    @property
    def _by_uid(self) -> str:
        return f"{self._ns}:uid:by-uid"

    @property
    def _by_sandbox(self) -> str:
        return f"{self._ns}:uid:by-sandbox"

    def allocate(self, *, start: int, size: int, sandbox_id: str) -> int | None:
        """Claim the lowest free uid, or ``None`` when the pool is exhausted."""
        existing = self._client.hget(self._by_sandbox, sandbox_id)
        if existing is not None:
            return int(existing)
        for uid in range(start, start + size):
            if self._client.hsetnx(self._by_uid, uid, sandbox_id):
                self._client.hset(self._by_sandbox, sandbox_id, uid)
                return uid
        return None

    def release(self, sandbox_id: str) -> int | None:
        """Drop ``sandbox_id``'s claim; returns the uid it held, if any."""
        raw = self._client.hget(self._by_sandbox, sandbox_id)
        if raw is None:
            return None
        uid = int(raw)
        with self._client.pipeline() as pipe:
            pipe.hdel(self._by_uid, uid)
            pipe.hdel(self._by_sandbox, sandbox_id)
            pipe.execute()
        return uid

    def taken(self) -> dict[int, str]:
        raw = self._client.hgetall(self._by_uid)
        return {
            int(k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }


def create_redis_client(url: str | None):
    if not url or redis is None:
        return None
    return redis.from_url(url, decode_responses=False)
