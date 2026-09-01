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
