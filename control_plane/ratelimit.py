"""In-process sliding-window rate limiter for control-plane endpoints.

Single-process accounting (per API key). ``limit == 0`` disables the limiter.
Multi-replica deployments that need strict global limits should move this to
the shared Redis ledger; for the single control-plane deployment shape this
is sufficient to stop per-key create amplification.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record one hit for ``key``; True when within the budget."""
        if self._limit <= 0:
            return True
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
