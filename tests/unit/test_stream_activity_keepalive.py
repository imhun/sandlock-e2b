"""E9.1: an open stream keeps the sandbox active while it is open.

Idle detection (and the idle->pause sweep built on it) reads
``last_active_at``: the worker reports in-sandbox activity on its heartbeat,
and the control plane merges it. A request that *streams* -- ``Process/Start``
running ``sleep 300``, a slow ``make``, an exec waiting on a remote API -- is
one request that stays open for minutes with no further traffic and (for
``sleep``) no measurable CPU. Without this keep-alive, such a sandbox looks
idle and gets frozen mid-command.

The mark is coalesced inside ``RuntimeRegistry`` (10 s), so a keep-alive that
re-stamps at that same cadence costs at most one entry per 10 s per stream.
"""

from __future__ import annotations

import asyncio

import pytest

from envd_service.connect import router
from envd_service.connect.codec import encode_message


class _CountingRegistry:
    """A runtime registry that only counts ``mark_active`` calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.running: dict[str, object] = {"sbx_a": _Runtime()}

    def get(self, sandbox_id: str):
        return self.running.get(sandbox_id)

    def mark_active(self, sandbox_id: str) -> None:
        self.calls.append(sandbox_id)


class _Runtime:
    access_token = "tok"
    state = "running"
    sandbox_id = "sbx_a"


class _App:
    def __init__(self, registry) -> None:
        self.state = _State(registry)


class _State:
    def __init__(self, registry) -> None:
        self.runtime_registry = registry


class _Request:
    """The three things ``handle_stream`` touches on a Request."""

    def __init__(self, registry, raw: bytes) -> None:
        self.app = _App(registry)
        self.headers = {
            "E2b-Sandbox-Id": "sbx_a",
            "X-Access-Token": "tok",
        }
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw


def test_a_silent_stream_keeps_the_sandbox_active(monkeypatch):
    monkeypatch.setattr(router, "ACTIVITY_KEEPALIVE_S", 0.01)
    registry = _CountingRegistry()

    async def run():
        task = asyncio.create_task(
            router._keep_active_while_streaming(registry, "sbx_a")
        )
        await asyncio.sleep(0.035)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert registry.calls[0] == "sbx_a"
    assert len(registry.calls) >= 3


def test_the_keepalive_stops_when_the_stream_closes(monkeypatch):
    monkeypatch.setattr(router, "ACTIVITY_KEEPALIVE_S", 0.01)
    registry = _CountingRegistry()
    request = _Request(registry, encode_message({"process": {"start": {}}}))

    async def handler(_request, _payload, _sandbox):
        async def events():
            # A silent stretch, then the stream ends: this is the shape a
            # ``sleep 300`` command has (no chunks are produced while it runs).
            await asyncio.sleep(0.05)
            yield {"event": "done"}

        return events()

    async def run() -> int:
        response = await router.handle_stream(
            request, "process.Process/Start", handler, require_sandbox=True
        )
        async for _chunk in response.body_iterator:
            pass
        during = len(registry.calls)
        await asyncio.sleep(0.05)
        return during, len(registry.calls)

    during, after = asyncio.run(run())
    assert during >= 3
    assert after == during
