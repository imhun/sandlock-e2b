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
from typing import Any, Sequence

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

#: How many WATCH conflicts ``RedisQuotaStore.release_once`` retries before it
#: gives up. A conflict means another replica moved one of the watched keys
#: between this replica's read and its write, so the loop re-reads and
#: re-decides. The bound exists so a hot key cannot spin forever: dozens of
#: conflicts in a row is not contention, it is a wedged replica.
RELEASE_ONCE_MAX_ATTEMPTS = 64


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

    def release_once(
        self,
        rows: Sequence[tuple[str, dict[str, int]]],
        marker_key: str,
        *,
        marker_ttl_s: int,
        tombstone_ttl_s: int | None = None,
        record_key: str | None = None,
    ) -> bool:
        """Give every row in ``rows`` back exactly once per reservation episode.

        ``release`` just subtracts, which is only safe for a caller that
        already knows the reservation is held. ``pause`` is where that breaks
        (N41): it moves the rows, then saves the record that carries
        ``quota_released``, so a replica that reads the record in between
        concludes the reservation is still held and returns it a second time.
        Under-counting the ledger is the direction that over-sells the fleet,
        so "this reservation is already back" has to be a store-side fact
        rather than a flag the loser has not read yet.

        Two copies of that fact are written by **one** WATCH/MULTI over the
        record key, the marker key and every ledger key:

        * ``marker_key`` (TTL'd, ``marker_ttl_s``) is the cheap shared claim.
          It is what refuses a rival whose stale read predates the release --
          including one whose record object is all it has left, because the
          delete path removes the record before it releases.
        * the record's own ``quota_released`` flag, set on the copy already in
          the store, is the copy that survives the marker. A replica that dies
          between the release and its ``save`` leaves that record saying "held"
          and a paused record is never expired by the TTL sweep, so the marker
          alone would let a much later delete return the rows again, one TTL
          after the bug was supposed to be closed.

        ``rows`` is a sequence of ``(scope, dims)`` pairs rather than a single
        ledger because one reservation may sit in two of them (``global`` and
        ``tenant:<id>``); giving them back in two calls would need two markers
        and leave room for an episode to be half-returned.

        Returns ``True`` when this call moved the rows; ``False`` when either
        guard says the reservation is already back, in which case nothing is
        written. Store trouble is deliberately *not* swallowed (unlike
        :func:`try_claim` and the rate limiter's window): a release that cannot
        reach the store has moved no rows, and reporting it as done would be
        the under-count direction again, while the caller's pause/delete can
        simply fail and be retried, leaving the reservation booked
        (over-counted -- capacity that is only recovered by retrying).

        ``tombstone_ttl_s`` is what the marker gets when there is **no durable
        record left to consult or mark** -- the delete path removes the record
        before it releases (N53's ordering), which leaves the 600 s marker as
        the *only* guard. A second release of the same episode arriving after
        that TTL then has nothing in its way, and the row is decremented twice
        (N78: measured ``e2b:quota:global`` at -1024/-100/-1024/-256 with an
        empty fleet). A marker that outlives any possible sandbox lifetime
        closes that window; it costs only key space, because the key names one
        episode (``sandbox_id`` + ``client_id``, minted per create).
        """
        ledger_keys = [self._key(name) for name, _ in rows]
        watched = [*ledger_keys, marker_key]
        if record_key is not None:
            watched.append(record_key)
        with self._client.pipeline() as pipe:
            for _ in range(RELEASE_ONCE_MAX_ATTEMPTS):
                try:
                    pipe.watch(*watched)
                    if pipe.exists(marker_key):
                        pipe.unwatch()
                        return False
                    record_value: str | None = None
                    if record_key is not None:
                        raw = pipe.get(record_key)
                        marked, record_value = self._released_record(raw)
                        if marked:
                            pipe.unwatch()
                            return False
                    # N78: read the rows inside the WATCH and clamp at zero. A
                    # row that would cross zero means something released this
                    # episode already; a negative row is *free capacity*, the
                    # direction that over-sells the fleet, so it is clamped and
                    # named rather than written.
                    clamped: dict[str, dict[str, int]] = {}
                    for name, dims in rows:
                        current = self.get(name)
                        below = {
                            dim: int(current.get(dim, 0)) - value
                            for dim, value in dims.items()
                            if int(current.get(dim, 0)) - value < 0
                        }
                        if below:
                            logger.warning(
                                "quota release for %s would take %s below zero "
                                "(%s); clamping to zero -- something released "
                                "this reservation already",
                                marker_key,
                                name,
                                ", ".join(
                                    f"{dim}: {current.get(dim, 0)} -"
                                    f" {dims[dim]} = {below[dim]}"
                                    for dim in sorted(below)
                                ),
                            )
                            clamped[name] = {
                                dim: max(0, int(current.get(dim, 0)) - value)
                                for dim, value in dims.items()
                            }
                    pipe.multi()
                    ttl = (
                        tombstone_ttl_s
                        if record_value is None and tombstone_ttl_s is not None
                        else marker_ttl_s
                    )
                    pipe.set(marker_key, "1", ex=max(1, int(ttl)))
                    if record_value is not None:
                        pipe.set(record_key, record_value)
                    clamped_keys = {self._key(name) for name in clamped}
                    for (_, dims), key in zip(rows, ledger_keys):
                        if key in clamped_keys:
                            # Written whole (clamped) below, not decremented.
                            continue
                        for dim, value in dims.items():
                            pipe.hincrby(key, dim, -value)
                    for name, dims in clamped.items():
                        pipe.hset(self._key(name), mapping=dims)
                    pipe.execute()
                    return True
                except (redis.WatchError, redis.exceptions.WatchError):  # type: ignore[union-attr]
                    continue
        raise RuntimeError(  # pragma: no cover - defensive
            f"quota release for {marker_key} kept losing its WATCH race"
        )

    @staticmethod
    def _released_record(raw: Any) -> tuple[bool, str | None]:
        """Read the store's copy of a record for the release transaction.

        Returns ``(already_released, value_to_write)``. A record that is absent,
        unparseable or not a sandbox record (the namespace is shared with the
        volume registry) yields ``(False, None)``: there is no durable copy to
        consult or to mark, and the marker is left to guard the window. Only the
        stored payload is touched -- the caller's own object keeps whatever the
        calling path put in it, and another replica's concurrent edits to other
        fields are not clobbered with a stale copy.
        """
        if not raw:
            return False, None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False, None
        if not isinstance(payload, dict) or "quota_released" not in payload:
            return False, None
        if payload["quota_released"]:
            return True, None
        payload["quota_released"] = True
        return False, json.dumps(payload, separators=(",", ":"))

    def get(self, name: str) -> dict[str, int]:
        """Current reserved values for a name ({} when never reserved)."""
        key = self._key(name)
        raw = self._client.hgetall(key)
        return {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in raw.items()
        }

    def reconcile(self, name: str, dims: dict[str, int]) -> dict[str, int]:
        """Make one ledger row equal ``dims``; return the deltas applied.

        The one caller is a node **re-registration** (``_rebuild_node_reservations``):
        the sandbox records are authoritative for that node at that instant --
        its runtime has just been (re)created, and every reservation that is
        serving a sandbox there has a record -- so the ledger is set to the
        records' sum, in one WATCH/MULTI so a concurrent ``reserve`` cannot be
        interleaved.

        Why this exists (N59's operational half): the ledger had **no
        reconciliation path at all** and no TTL, so a leaked reservation stayed
        until an operator deleted the Redis hash by hand -- and a few leaked
        slots jammed the whole fleet with ``503``. That jam was the *amplifier*
        N60 removed: today an unpinned ``select_and_reserve`` hands the
        placement to the next candidate when this store refuses, while a
        volume-**pinned** one still answers ``503`` by design. Either way a
        drifted row over-reports a node's usage and can refuse work the node
        could take, so the row still has to heal. The in-memory view already
        healed on registration (``NodeRegistry.set_reserved``); this is the
        same healing for the half that is shared across replicas.

        The deltas are returned signed (negative = lowered) so the caller can
        name what it corrected instead of silently rewriting a ledger. A
        *downward* correction is the direction worth reading: it can only drop
        a reservation that no record accounts for, which at registration means
        a leak (or, in a narrow window, a create in flight against a node that
        is being re-registered -- named in ``_rebuild_node_reservations``).
        """
        key = self._key(name)
        with self._client.pipeline() as pipe:
            for _ in range(RELEASE_ONCE_MAX_ATTEMPTS):
                try:
                    pipe.watch(key)
                    raw = pipe.hgetall(key)
                    used = {
                        (k.decode() if isinstance(k, bytes) else k): int(v)
                        for k, v in raw.items()
                    }
                    deltas = {
                        dim: int(want) - used.get(dim, 0)
                        for dim, want in dims.items()
                        if int(want) != used.get(dim, 0)
                    }
                    if not deltas:
                        pipe.unwatch()
                        return {}
                    pipe.multi()
                    for dim, delta in deltas.items():
                        pipe.hincrby(key, dim, delta)
                    pipe.execute()
                    return deltas
                except (redis.WatchError, redis.exceptions.WatchError):  # type: ignore[union-attr]
                    continue
        raise RuntimeError(  # pragma: no cover - defensive
            f"quota reconciliation for {key} kept losing its WATCH race"
        )


class RedisRecordStore:
    """JSON records shared across replicas."""

    def __init__(self, client: Any, namespace: str) -> None:
        self._client = client
        self._ns = namespace

    def _key(self, record_id: str) -> str:
        return f"{self._ns}:record:{record_id}"

    def record_key(self, record_id: str) -> str:
        """The shared key ``record_id`` lives under.

        Public because the quota release transaction has to WATCH -- and mark
        -- the stored record atomically with the ledger rows it guards (N41);
        a second copy of this layout would drift.
        """
        return self._key(record_id)

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


class RedisNodeStore:
    """The fleet's node view, shared across control-plane replicas (F11 step 1).

    The node registry used to keep health, address, capacity and reservations
    in process memory, and *that* is what made a second replica dangerous: two
    replicas could answer "healthy" and "unhealthy" about the same node in the
    same moment (each sweeping its own heartbeat timestamps), a placement
    decision made on one was invisible to the other, and a worker that
    heartbeated replica B after registering with A got a 404 and re-registered.

    The view lives here instead. Health is still *derived* -- from the shared
    ``heartbeat_at``, by every reader, with the same timeout -- so both
    replicas compute the same answer from the same bytes. The TTL is what
    retires a node whose worker is gone for good: a view nobody refreshes
    disappears on its own, and both replicas lose it at the same moment (the
    Redis clock), rather than each deciding for itself.

    The in-process (``local://``) node deliberately does **not** live here:
    it is a worker embedded in *this* replica, so publishing it would let
    another replica place work on a worker it cannot reach, and the two
    replicas' rows would collide on the id ``local``.
    """

    def __init__(self, client: Any, namespace: str) -> None:
        self._client = client
        self._ns = namespace

    def _key(self, node_id: str) -> str:
        return f"{self._ns}:node:view:{node_id}"

    def put(
        self, node_id: str, payload: dict[str, Any], ttl: int | None = None
    ) -> None:
        key = self._key(node_id)
        with self._client.pipeline() as pipe:
            pipe.set(key, json.dumps(payload, separators=(",", ":")))
            if ttl:
                # ``ex`` on the write keeps the refresher from having to make a
                # second round trip; a heartbeat is the hot path of the fleet.
                pipe.expire(key, ttl)
            pipe.execute()

    def get(self, node_id: str) -> dict[str, Any] | None:
        raw = self._client.get(self._key(node_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def list(self) -> list[dict[str, Any]]:
        pattern = f"{self._ns}:node:view:*"
        out: list[dict[str, Any]] = []
        for raw in self._client.mget(sorted(self._client.keys(pattern))):
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    def delete(self, node_id: str) -> None:
        self._client.delete(self._key(node_id))


class RedisNodeReservationStore:
    """Per-node reservation counters, in their own atomic hash (N70).

    ``RedisNodeStore`` keeps a whole node view as **one JSON string**, so a
    heartbeat and a delete both do read-modify-write on it and two replicas
    lose each other's updates. The live shape: the view's ``reserved_*`` came
    out *higher* than the sandbox records (worker-0 holding a phantom 1024 MB),
    the shared quota ledger was clean, no warning was logged, and only a
    re-registration pressed the view back down.

    The counters therefore live in a hash of their own, where ``HINCRBY`` is
    atomic, and ``NodeRegistry`` merges them over the row on every read. The row
    keeps carrying ``reserved_*`` as well (that is the pre-N70 shape): a replica
    that has never written a counter for a node reads the row, so a rolling
    upgrade stays self-consistent in both directions.

    Negative counters are **named, never clamped**: a value below zero means
    the counter and the sandbox records disagree, and quietly flooring it to
    zero would hide exactly the drift this class exists to make visible.
    """

    #: The four dimensions, in the order the warnings name them (stable text).
    DIMENSIONS = ("memory", "cpu", "disk", "processes")

    def __init__(self, client: Any, namespace: str) -> None:
        self._client = client
        self._ns = namespace

    def _key(self, node_id: str) -> str:
        return f"{self._ns}:node:res:{node_id}"

    def add(self, node_id: str, dims: dict[str, int]) -> None:
        """Move each dimension by ``dims`` atomically; name a negative result.

        A reserve passes positive values, a release negated ones -- either way
        it is one ``HINCRBY`` per dimension inside one pipeline, so no replica
        can read a half-applied reservation and write it back over another's.
        """
        key = self._key(node_id)
        with self._client.pipeline() as pipe:
            for dim in self.DIMENSIONS:
                pipe.hincrby(key, dim, int(dims.get(dim, 0)))
            values = pipe.execute()
        self._name_negatives(
            node_id, dict(zip(self.DIMENSIONS, (int(v) for v in values)))
        )

    def set(self, node_id: str, dims: dict[str, int]) -> None:
        """Set every dimension (registration reconciliation); name a negative."""
        values = {dim: int(dims.get(dim, 0)) for dim in self.DIMENSIONS}
        self._client.hset(self._key(node_id), mapping=values)
        self._name_negatives(node_id, values)

    def get(self, node_id: str) -> dict[str, int] | None:
        """Every dimension, or ``None`` when this node has no counter yet.

        ``None`` is the fallback signal the registry needs: a node without a
        counter hash (an older replica's row, or a write that has not landed)
        must read its reservations from the JSON view row, not from zero.
        """
        return self.get_many([node_id])[node_id]

    def get_many(
        self, node_ids: Sequence[str]
    ) -> dict[str, dict[str, int] | None]:
        """The counter for each id in one round trip (``None`` when absent)."""
        ids = list(node_ids)
        if not ids:
            return {}
        with self._client.pipeline() as pipe:
            for node_id in ids:
                pipe.hgetall(self._key(node_id))
            rows = pipe.execute()
        out: dict[str, dict[str, int] | None] = {}
        for node_id, raw in zip(ids, rows):
            if not raw:
                out[node_id] = None
                continue
            values = {
                (k.decode() if isinstance(k, bytes) else k): int(v)
                for k, v in raw.items()
            }
            self._name_negatives(node_id, values)
            out[node_id] = values
        return out

    def delete(self, node_id: str) -> None:
        self._client.delete(self._key(node_id))

    @staticmethod
    def _name_negatives(node_id: str, values: dict[str, int]) -> None:
        """Name a negative counter instead of clamping it to zero."""
        negative = ", ".join(
            f"{dim}={values[dim]}"
            for dim in RedisNodeReservationStore.DIMENSIONS
            if values.get(dim, 0) < 0
        )
        if negative:
            logger.warning(
                "node %s reservation counter is negative (%s); the shared "
                "counter and the sandbox records disagree, and the counter is "
                "read as it stands rather than clamped to 0",
                node_id,
                negative,
            )


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


def try_claim(client: Any, key: str, *, ttl_s: int) -> bool:
    """One fleet-wide claim: True when this process owns ``key`` for ``ttl_s``.

    The shape every periodic job needs once the control plane can run as more
    than one replica (F11 steps 2 and 4): a TTL'd key is the whole protocol --
    whoever sets it wins the round, a winner that dies mid-round costs the
    fleet exactly one round, and there is no lock to release. Without a client
    there is one process, which is the winner by definition.

    Failure to reach the store errs on the side of *doing the work*: a round
    that runs twice is duplicated effort, while a round that never runs is a
    resource that never comes back.
    """
    if client is None:
        return True
    try:
        return bool(client.set(key, "1", nx=True, ex=max(1, int(ttl_s))))
    except Exception:  # pragma: no cover - defensive
        logger.warning("claim %s failed; doing the work anyway", key, exc_info=True)
        return True
