"""The process stream's in-band keepalive (N37).

A command that prints nothing for a minute is a *normal* shape -- a 4000-file
write on this deployment's NFS workspace takes ~94 s -- and the reverse proxy
in front of the API cuts a response body that has been silent for 60.0 s
(measured on the fleet; see ``gateway_common/keepalive.py``). The official SDK
asks for pings on exactly this stream (``Keepalive-Ping-Interval: 50``), so
``envd_service/rpc.py::_consume_stream`` has to answer with the protocol's
empty ``ProcessEvent.KeepAlive`` while the sandbox is quiet.

These tests pin the two halves that can drift independently: the resolved
interval (including the clamp), and the relay's behavior -- a ping per silent
interval, the real events relayed *verbatim*, and nothing sent after the end
event.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from envd_service.process.events import data_event, end_event, keepalive_event
from envd_service.rpc import _consume_stream
from gateway_common.keepalive import (
    EDGE_IDLE_CUT_S,
    SDK_KEEPALIVE_PING_INTERVAL_S,
    STREAM_KEEPALIVE_MAX_S,
    STREAM_KEEPALIVE_S,
    stream_keepalive_interval_s,
)


class _FakeProc:
    """Just enough of ``ManagedProcess`` for the relay's ``finally``."""

    def __init__(self) -> None:
        self.unsubscribed: list[object] = []

    def unsubscribe(self, queue: object) -> None:
        self.unsubscribed.append(queue)


def test_the_sdk_interval_is_what_the_edge_leaves_room_for() -> None:
    """The three numbers only work in this order, so pin the order.

    The SDK asks for ``SDK_KEEPALIVE_PING_INTERVAL_S``; we may answer sooner
    (never later than ``STREAM_KEEPALIVE_MAX_S``); and both have to sit inside
    the edge's idle window, which is the *given* this fix exists for. A change
    to any one of them without the others is exactly the regression
    (``test_server_keepalive.py`` pins its own pair the same way).
    """
    assert SDK_KEEPALIVE_PING_INTERVAL_S == 50.0
    assert STREAM_KEEPALIVE_S < STREAM_KEEPALIVE_MAX_S
    assert STREAM_KEEPALIVE_MAX_S < EDGE_IDLE_CUT_S


def test_a_client_that_asks_for_an_interval_gets_it() -> None:
    assert stream_keepalive_interval_s("5") == 5.0
    assert stream_keepalive_interval_s("0.5") == 0.5


def test_a_long_requested_interval_is_clamped_below_the_idle_cut() -> None:
    # The SDK's own 50 s would leave 10 s of margin against a 60 s cut; asking
    # for more than the clamp is answered with the clamp, because pinging more
    # often than asked is free and pinging later is the bug.
    assert stream_keepalive_interval_s("50") == STREAM_KEEPALIVE_MAX_S
    assert stream_keepalive_interval_s("3600") == STREAM_KEEPALIVE_MAX_S
    assert stream_keepalive_interval_s("30") == STREAM_KEEPALIVE_MAX_S


def test_a_missing_or_unusable_interval_falls_back_to_the_default() -> None:
    assert stream_keepalive_interval_s(None) == STREAM_KEEPALIVE_S
    assert stream_keepalive_interval_s("") == STREAM_KEEPALIVE_S
    assert stream_keepalive_interval_s("   ") == STREAM_KEEPALIVE_S
    assert stream_keepalive_interval_s("0") == STREAM_KEEPALIVE_S
    assert stream_keepalive_interval_s("-1") == STREAM_KEEPALIVE_S
    assert stream_keepalive_interval_s("soon") == STREAM_KEEPALIVE_S


def test_the_keepalive_event_is_the_protocols_empty_message() -> None:
    assert keepalive_event() == {"event": {"keepalive": {}}}


async def test_a_silent_stream_pings_once_per_interval() -> None:
    """No output for a whole interval means exactly one ping, after the wait.

    The lower bound is the load-bearing half: a relay that pinged
    *immediately* (or on every loop turn) would also keep the connection
    alive, but it would flood a stream that is merely between two writes.
    """
    interval = 0.05
    proc = _FakeProc()
    queue: asyncio.Queue = asyncio.Queue()
    stream = _consume_stream(proc, queue, interval)

    started = time.monotonic()
    first = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    waited = time.monotonic() - started
    assert first == {"event": {"keepalive": {}}}
    assert waited >= interval
    assert waited < 1.0

    started = time.monotonic()
    second = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    assert second == {"event": {"keepalive": {}}}
    assert time.monotonic() - started >= interval

    await stream.aclose()
    assert proc.unsubscribed == [queue]


async def test_a_busy_stream_is_relayed_verbatim_and_never_pings_early() -> None:
    interval = 5.0
    proc = _FakeProc()
    queue: asyncio.Queue = asyncio.Queue()
    stream = _consume_stream(proc, queue, interval)

    queue.put_nowait(("data", "stdout", b"hello"))
    queue.put_nowait(("data", "stderr", b"oops"))
    queue.put_nowait(("end", 3, "exited"))

    # Output on hand is relayed as it is: the ping only covers silence, so
    # these three events arrive well inside one interval and unchanged.
    assert await asyncio.wait_for(stream.__anext__(), timeout=2.0) == {
        "event": {"data": {"stdout": "aGVsbG8="}}
    }
    assert await asyncio.wait_for(stream.__anext__(), timeout=2.0) == {
        "event": {"data": {"stderr": "b29wcw=="}}
    }
    assert await asyncio.wait_for(stream.__anext__(), timeout=2.0) == end_event(3)

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    assert proc.unsubscribed == [queue]


async def test_the_data_and_end_events_are_unchanged_by_the_ping() -> None:
    """The relay is a pass-through: same builders, same order, then a stop."""
    proc = _FakeProc()
    queue: asyncio.Queue = asyncio.Queue()
    stream = _consume_stream(proc, queue, 5.0)

    queue.put_nowait(("data", "pty", b"\x00\x01"))
    queue.put_nowait(("end", 0, "exited"))

    assert await asyncio.wait_for(stream.__anext__(), timeout=2.0) == data_event(
        "pty", b"\x00\x01"
    )
    assert await asyncio.wait_for(stream.__anext__(), timeout=2.0) == end_event(0)
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
