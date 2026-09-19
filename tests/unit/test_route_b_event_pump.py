"""N25: the worker end of the slot's pushed events.

The slot writes one JSON object per line to a descriptor of its own (not the
control channel, which is request/response). The worker side has to be able to
say "no such channel" without failing, and to keep reading after a line it
cannot parse -- an events stream is a side channel, and a side channel that
can take the accounting down with it is worse than no side channel.
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from envd_service.route_b import RouteBInstance, SlotHandle


class _Pool:
    """The smallest thing `RouteBInstance` needs (it is not driven here)."""

    channel_factory = None

    def retire(self, handle):  # pragma: no cover - not exercised
        return None


def _instance(*, with_events: bool) -> tuple[RouteBInstance, socket.socket | None]:
    if with_events:
        reader, writer = socket.socketpair()
    else:
        reader = writer = None
    handle = SlotHandle(
        sandbox_id="sbx_ev",
        uid=10000,
        name="slot-events",
        events_socket=reader,
    )
    return RouteBInstance(pool=_Pool(), handle=handle, name="slot-events"), writer


def test_a_slot_without_an_events_channel_says_so():
    instance, _ = _instance(with_events=False)

    assert instance.start_event_pump(lambda event: None) is False


def test_events_are_parsed_and_delivered_in_order():
    instance, writer = _instance(with_events=True)
    seen: list[dict] = []
    started = instance.start_event_pump(seen.append)
    assert started is True

    for seq in range(3):
        line = json.dumps(
            {"v": 1, "event": "append", "seq": seq, "bytes": 1000 * (seq + 1)}
        )
        writer.sendall((line + "\n").encode())

    deadline = time.monotonic() + 5
    while len(seen) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert [event["seq"] for event in seen] == [0, 1, 2]
    assert [event["bytes"] for event in seen] == [1000, 2000, 3000]


def test_a_garbage_line_does_not_stop_the_stream():
    instance, writer = _instance(with_events=True)
    seen: list[dict] = []
    assert instance.start_event_pump(seen.append) is True

    writer.sendall(b"this is not json\n")
    writer.sendall(b'{"v": 1, "event": "append", "bytes": 7}\n')

    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)

    assert seen == [{"v": 1, "event": "append", "bytes": 7}]


def test_a_consumer_that_raises_does_not_stop_the_stream():
    instance, writer = _instance(with_events=True)
    seen: list[dict] = []

    def consume(event: dict) -> None:
        seen.append(event)
        if len(seen) == 1:
            raise RuntimeError("accounting hiccup")

    assert instance.start_event_pump(consume) is True
    writer.sendall(b'{"v": 1, "event": "append", "bytes": 1}\n')
    writer.sendall(b'{"v": 1, "event": "append", "bytes": 2}\n')

    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert [event["bytes"] for event in seen] == [1, 2]


def test_closing_the_channel_ends_the_pump():
    instance, writer = _instance(with_events=True)
    seen: list[dict] = []
    assert instance.start_event_pump(seen.append) is True
    writer.sendall(b'{"v": 1, "event": "append", "bytes": 5}\n')

    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen

    writer.close()
    instance.close()
    thread = instance._events_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive(), "the pump must end when its channel closes"


def test_a_second_pump_is_refused():
    instance, _writer = _instance(with_events=True)
    assert instance.start_event_pump(lambda event: None) is True
    # One stream, one reader: a second pump would race the first for lines and
    # each would see half of them.
    assert instance.start_event_pump(lambda event: None) is False
