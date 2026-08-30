"""Lightweight in-process sliding-window counters for fleet metrics."""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowCounter:
    """Count events in the last ``window_s`` seconds (thread-safe).

    In-memory per replica: with multiple control-plane replicas each exposes
    its own window, which is an acceptable approximation for autoscaling
    signals (the autoscaler can sum across replicas or read any one).
    """

    def __init__(self, window_s: float = 300.0) -> None:
        self._window = window_s
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def record(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._events.append(now)
            self._prune(now)

    def count(self) -> int:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return len(self._events)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
