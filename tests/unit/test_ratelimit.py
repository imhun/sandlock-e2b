"""Sliding-window rate limiter behavior."""

from __future__ import annotations

import time

from control_plane.ratelimit import SlidingWindowRateLimiter


def test_allows_within_budget():
    limiter = SlidingWindowRateLimiter(3, window_s=60.0)
    assert [limiter.allow("k") for _ in range(3)] == [True, True, True]
    assert limiter.allow("k") is False


def test_per_key_independent():
    limiter = SlidingWindowRateLimiter(1, window_s=60.0)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True


def test_window_slides():
    limiter = SlidingWindowRateLimiter(1, window_s=0.05)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    time.sleep(0.07)
    assert limiter.allow("k") is True


def test_disabled_always_allows():
    limiter = SlidingWindowRateLimiter(0)
    for _ in range(10):
        assert limiter.allow("k") is True
