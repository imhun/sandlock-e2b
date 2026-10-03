"""SEC-K0S-003: every queue that carries command output is bounded.

The audit found one unbounded queue (`ManagedProcess.subscribers`) and the
first fix bounded it plus what the relay sends. The live acceptance then found
the incident was still reproducible, because the *same* shape sits one hop
earlier: the executor's own output queue, filled from a reader thread through
``loop.call_soon_threadsafe(queue.put_nowait, chunk)`` and never bounded.
Measured 2026-10-03 on ``0.1.0-970``: a 256 MiB command with a frozen client
grew ``e2b-worker-0``'s anonymous memory from 59 MiB to 330 MiB while both new
counters (the subscriber queue's bytes and the relay's sent bytes) stayed
untouched -- the bytes were in the executor queue.

So the bound is one class, used by every hop: bytes *and* items (tiny writes
cost Python object overhead a byte count cannot see), a first-drop flag so the
producer can inject one ``TRUNCATED_MARK`` inline, and per-queue counters so a
truncated stream is never silent. ``None`` = unlimited (repo convention: an
env var of ``0`` means "off").
"""

from __future__ import annotations

import asyncio
import threading

#: How much output one hop may hold for one consumer before dropping.
STREAM_LIMIT_DEFAULT = 32 * 1024 * 1024
#: Hard item bound, so a stream of *tiny* writes is bounded in object count as
#: well as in bytes (each queued item costs ~100 B of Python object overhead).
STREAM_QUEUE_MAX_ITEMS = 1024
#: Written into the stream where output was dropped -- the same marker the
#: replay path uses, so every truncation looks alike.
TRUNCATED_MARK = b"\n... output truncated ...\n"


def item_bytes(item: object) -> int:
    """Payload bytes of one queued item.

    Handles the shapes the command path uses -- ``("data", kind, chunk)``,
    ``(kind, chunk)`` -- and anything else (``None`` sentinels, ints) as zero,
    so a control item never has to special-case its accounting.
    """
    if item is None:
        return 0
    if isinstance(item, (bytes, bytearray)):
        return len(item)
    if not isinstance(item, tuple):
        return 0
    return sum(len(part) for part in item if isinstance(part, (bytes, bytearray)))


class ByteBudgetQueue(asyncio.Queue):
    """An ``asyncio.Queue`` bounded by bytes and items, with drop accounting.

    ``put_nowait`` raises ``asyncio.QueueFull`` for a data item that does not
    fit -- that is the whole backpressure signal, and every producer in the
    command path catches it, counts the drop and injects the marker once.
    ``put_control`` is for items that must never be dropped (an end-of-stream
    marker): it makes room by discarding the oldest data item.
    """

    def __init__(
        self, *, max_bytes: int | None, max_items: int = STREAM_QUEUE_MAX_ITEMS
    ) -> None:
        super().__init__(maxsize=max_items)
        self.max_bytes = max_bytes
        self.dropped_bytes = 0
        self._bytes = 0
        self._marked = False

    @property
    def queued_bytes(self) -> int:
        return self._bytes

    def _fits(self, size: int) -> bool:
        # One byte-slot and one item-slot stay free for the truncation marker:
        # the cut point must always be deliverable, and the consumer's payload
        # must never exceed the budget it was given.
        if self.qsize() >= self.maxsize - 1:
            return False
        if self.max_bytes is None:
            return True
        return self._bytes + size + len(TRUNCATED_MARK) <= self.max_bytes

    def put_nowait(self, item: tuple) -> None:
        size = item_bytes(item)
        if not self._fits(size):
            raise asyncio.QueueFull
        super().put_nowait(item)
        self._bytes += size

    def put_control(self, item: tuple) -> None:
        """Enqueue an item that must arrive even when the budget is spent."""
        while self.qsize() >= self.maxsize:
            oldest = self.get_nowait()
            self.dropped_bytes += item_bytes(oldest)
        super().put_nowait(item)
        self._bytes += item_bytes(item)

    def note_dropped(self, size: int) -> bool:
        """Count a dropped item; ``True`` the first time (inject the marker)."""
        self.dropped_bytes += size
        first = not self._marked
        self._marked = True
        return first

    def get_nowait(self):
        item = super().get_nowait()
        self._bytes -= item_bytes(item)
        return item

    async def get(self):
        item = await super().get()
        self._bytes -= item_bytes(item)
        return item


class ThreadHandoff:
    """A byte-bounded hand-off from a reader *thread* to the event loop.

    ``loop.call_soon_threadsafe(queue.put_nowait, chunk)`` is itself a queue,
    and it is invisible to every bound above: the callbacks (each carrying its
    chunk) pile up in the event loop's ready queue while the loop is busy.
    Measured 2026-10-03 on ``0.1.0-972``: with both queue bounds in place, a
    frozen client plus ``yes`` still OOMKilled ``e2b-worker-0``
    (13:33:34Z, exit 137) and the queue bound never fired once -- the bytes
    were in the ready queue.

    So the thread waits here for room instead. That is *real* backpressure and
    it loses nothing: the thread stops reading the command's pipe, the pipe
    fills, and the command's own writes block, exactly as they would on a slow
    terminal. A consumer that stops reading entirely therefore slows the
    sandbox down rather than growing the worker.
    """

    def __init__(self, *, max_bytes: int | None) -> None:
        self._max_bytes = max_bytes
        self._outstanding = 0
        self._closed = False
        self._condition = threading.Condition()

    @property
    def outstanding_bytes(self) -> int:
        with self._condition:
            return self._outstanding

    def acquire(self, size: int) -> None:
        """Block until ``size`` more bytes may be handed to the loop."""
        if self._max_bytes is None:
            return
        with self._condition:
            while (
                not self._closed
                and self._outstanding + size > self._max_bytes
            ):
                # A timeout keeps a lost ``release`` (a crashed consumer)
                # from wedging the pump forever; the loop re-checks the state.
                self._condition.wait(timeout=0.5)
            self._outstanding += size

    def release(self, size: int) -> None:
        """Called by the loop side once the chunk has been handled."""
        if self._max_bytes is None:
            return
        with self._condition:
            self._outstanding = max(0, self._outstanding - size)
            self._condition.notify_all()

    def close(self) -> None:
        """Unblock every waiter (the process ended or is being torn down)."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
