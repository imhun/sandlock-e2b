"""Sliding-window rate limiter for control-plane endpoints.

``limit == 0`` disables the limiter. The window is kept per process by default,
and in the shared Redis ledger when the deployment has one (F11 step 4): with
two replicas an in-process limiter enforces the configured limit *per replica*,
which is how a "60 creates a minute" ceiling quietly becomes 120. The Redis
window is the same algorithm over a ZSET, so the limit means one thing fleet-wide.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque

try:  # pragma: no cover - the import is what decides the local fallback
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        *,
        name: str | None = None,
        redis_client=None,
        namespace: str = "e2b",
    ) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._name = name or "default"
        self._redis = redis_client
        self._ns = namespace

    def _key(self, key: str) -> str:
        return f"{self._ns}:ratelimit:{self._name}:{key}"

    def _allow_shared(self, key: str) -> bool | None:
        """The shared window, or ``None`` when the store cannot answer.

        A ZSET per key: the score is the moment of the hit, members older than
        the window are trimmed, and the count is how many are left. Check and
        insert have to be one step -- otherwise two replicas can both find one
        slot free and both take it -- so this is WATCH/MULTI, the same shape
        ``RedisQuotaStore.reserve`` already uses for node quota.

        Scores are wall-clock seconds, not ``time.monotonic``: the score has to
        mean the same thing in every replica, which puts inter-replica clock
        skew into the accuracy of the limit. That is the trade for a window
        every replica agrees on.
        """
        if self._redis is None:
            return None
        redis_key = self._key(key)
        member = f"{time.time():.6f}:{uuid.uuid4().hex}"
        now = time.time()
        try:
            with self._redis.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(redis_key)
                        pipe.zremrangebyscore(redis_key, 0, now - self._window)
                        used = pipe.zcard(redis_key)
                        if used >= self._limit:
                            pipe.unwatch()
                            return False
                        pipe.multi()
                        pipe.zadd(redis_key, {member: now})
                        pipe.expire(redis_key, int(self._window) + 1)
                        pipe.execute()
                        return True
                    except (redis.WatchError, redis.exceptions.WatchError):  # type: ignore[union-attr]
                        continue
        except Exception:  # pragma: no cover - defensive
            # The store is down: answer with the local window rather than
            # failing the request. The limit is protection, not a contract the
            # caller negotiated, and the local window is strictly stricter
            # than nothing.
            logger.warning("shared rate-limit window failed; using the local one", exc_info=True)
            return None

    def allow(self, key: str) -> bool:
        """Record one hit for ``key``; True when within the budget."""
        if self._limit <= 0:
            return True
        shared = self._allow_shared(key)
        if shared is not None:
            return shared
        return self._allow_local(key)

    def _allow_local(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > self._window:
                dq.popleft()
            if len(dq) >= self._limit:
                return False
            dq.append(now)
            return True

    def remaining(self, key: str) -> int:
        if self._limit <= 0:
            return -1
        if self._redis is not None:
            try:
                redis_key = self._key(key)
                self._redis.zremrangebyscore(redis_key, 0, time.time() - self._window)
                return max(0, self._limit - int(self._redis.zcard(redis_key)))
            except Exception:  # pragma: no cover - defensive
                logger.warning("shared rate-limit read failed", exc_info=True)
        now = time.monotonic()
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return self._limit
            while dq and now - dq[0] > self._window:
                dq.popleft()
            return max(0, self._limit - len(dq))


def enforce_resource_limit(
    request,
    *,
    limiter: SlidingWindowRateLimiter,
    tenant_limiter: SlidingWindowRateLimiter,
    message: str,
) -> None:
    """Admission for a *resource-creating* endpoint: per key, then per tenant.

    The same shape sandbox create uses (E3.5/E9.3): the caller's key spends one
    slot first, then the tenant's when tenant isolation is configured. Admin
    keys and single-tenant compatible mode skip the tenant half.

    Every endpoint that allocates durable platform state calls this -- sandbox
    create, snapshot create and volume create. Leaving any of them out made an
    authenticated key able to loop that one for free while the others were
    throttled; the limiters are per endpoint so a burst on one cannot spend
    another's budget.

    Raises ``OfficialError(429)`` on refusal. Imported lazily because
    ``control_plane.api.errors`` and ``control_plane.auth`` import this module's
    siblings's dependents; a module-level import would close the cycle.
    """
    from control_plane.api.errors import OfficialError
    from control_plane.auth import tenant_of

    key = request.headers.get("X-API-Key") or request.headers.get("X-API-KEY", "")
    if not limiter.allow(key):
        raise OfficialError(429, message)
    settings = request.app.state.settings
    if not settings.tenants_enabled:
        return
    tenant, is_admin = tenant_of(request)
    if tenant is not None and not is_admin and not tenant_limiter.allow(tenant):
        raise OfficialError(429, message)
