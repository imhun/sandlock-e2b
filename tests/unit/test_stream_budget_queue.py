"""SEC-K0S-003: the shared byte/item bound every output hop uses.

The audit found one unbounded queue; the live acceptance then showed the same
shape on two more hops (the executor's reader-thread queue and the relay's
*sent* bytes). They now share one class, so these pin the class itself: bytes,
items, the marker's reserved headroom, control items that must never be
dropped, and the accounting that makes a truncated stream visible.
"""

from __future__ import annotations

import asyncio

import pytest

from envd_service.process.stream_budget import (
    TRUNCATED_MARK,
    ByteBudgetQueue,
)


def test_a_data_item_over_the_budget_is_refused():
    queue = ByteBudgetQueue(max_bytes=1024)
    for _ in range(3):  # 768 bytes + the marker's 26-byte reserve fits
        queue.put_nowait(("stdout", b"x" * 256))

    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(("stdout", b"x" * 256))

    assert queue.queued_bytes == 768
    assert queue.qsize() == 3


def test_the_marker_headroom_is_reserved_so_the_cut_always_fits():
    queue = ByteBudgetQueue(max_bytes=1024)
    for _ in range(3):
        queue.put_nowait(("stdout", b"x" * 256))
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(("stdout", b"y" * 256))

    assert queue.note_dropped(256) is True
    queue.put_control(("stdout", TRUNCATED_MARK))

    payload = b"".join(item_bytes_of(queue.get_nowait()) for _ in range(4))
    assert len(payload) <= 1024
    assert payload.count(TRUNCATED_MARK) == 1
    assert queue.dropped_bytes == 256


def test_a_control_item_evicts_the_oldest_data_item_when_full():
    """The end sentinel must land even in a queue that is already at its bound."""
    queue = ByteBudgetQueue(max_bytes=None, max_items=3)
    for index in range(3):
        # Control puts (no reserve rule) are how a full queue is reached; a
        # data put always leaves one slot free for the marker.
        queue.put_control(("stdout", bytes([65 + index]) * 8))

    queue.put_control(("__eof__", "stdout"))

    kinds = [queue.get_nowait() for _ in range(3)]
    assert kinds == [
        ("stdout", b"B" * 8),
        ("stdout", b"C" * 8),
        ("__eof__", "stdout"),
    ]
    assert queue.dropped_bytes == 8


def test_the_item_bound_catches_tiny_writes():
    queue = ByteBudgetQueue(max_bytes=1024 * 1024, max_items=4)
    for _ in range(3):
        queue.put_nowait(("stdout", b"x"))

    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(("stdout", b"x"))


def test_getting_returns_room_to_the_budget():
    queue = ByteBudgetQueue(max_bytes=1024)
    for _ in range(3):
        queue.put_nowait(("stdout", b"x" * 256))
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(("stdout", b"x" * 256))

    queue.get_nowait()

    queue.put_nowait(("stdout", b"x" * 256))
    assert queue.queued_bytes == 768


def test_an_unlimited_queue_only_bounds_items():
    queue = ByteBudgetQueue(max_bytes=None, max_items=2)
    queue.put_nowait(("stdout", b"x" * (1024 * 1024)))

    assert queue.queued_bytes == 1024 * 1024
    assert queue.max_bytes is None


def item_bytes_of(item: tuple) -> bytes:
    """The payload of one ``(kind, chunk)`` item, for byte-exact assertions."""
    return item[1] if isinstance(item[1], bytes) else b""
