"""E4.1: command output capture is capped (``cat /dev/zero`` proof)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from envd_service.executors.base import ExecConfig, Executor, RunningProcess
from envd_service.process.logs import CommandLogWriter
from envd_service.process.manager import (
    CAPTURE_LIMIT_DEFAULT,
    TRUNCATED_MARK,
    ProcessManager,
)


class _ChunkProcess(RunningProcess):
    """Queue-fed stdout process: the test controls each emitted chunk."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    @property
    def pid(self) -> int:
        return self._pid

    async def output(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield ("stdout", chunk)

    def send_stdin(self, data: bytes) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    def resize(self, rows: int, cols: int) -> None:
        pass

    def kill(self, sig: int) -> None:
        pass

    async def exit_code(self) -> int:
        return 0


class _ChunkExecutor(Executor):
    def __init__(self) -> None:
        self.live: dict[int, _ChunkProcess] = {}
        self._next_pid = 6000

    async def start(self, config: ExecConfig) -> RunningProcess:
        pid = self._next_pid
        self._next_pid += 1
        proc = _ChunkProcess(pid)
        self.live[pid] = proc
        return proc

    def emit(self, pid: int, chunk: bytes) -> None:
        self.live[pid]._queue.put_nowait(chunk)

    def end(self, pid: int) -> None:
        self.live[pid]._queue.put_nowait(None)


def _start_kwargs() -> dict:
    return {"cmd": ["cat"], "env": {}, "cwd": "/workspace", "stdin_enabled": False}


async def _wait_end(manager: ProcessManager, proc, queue, timeout: float = 5):
    return await asyncio.wait_for(
        manager.wait_ended(proc, queue), timeout=timeout
    )


def _replay_data(proc) -> bytes:
    """Read the replay queue after the process ended.

    ``subscribe(replay=True)`` enqueues the captured bytes synchronously;
    the end event was already broadcast to earlier subscribers, so the
    replay queue only ever holds data.
    """
    queue = proc.subscribe(replay=True)
    items: list[bytes] = []
    while not queue.empty():
        item = queue.get_nowait()
        assert item[0] == "data"
        items.append(item[2])
    return b"".join(items)


async def test_capture_crossing_replaces_tail_with_marker():
    executor = _ChunkExecutor()
    manager = ProcessManager(executor, sandbox_id="sbx_cap", capture_limit_bytes=1024)
    proc = await manager.start(**_start_kwargs())

    executor.emit(proc.pid, b"a" * 700)
    executor.emit(proc.pid, b"b" * 700)
    executor.emit(proc.pid, b"c" * 700)
    executor.end(proc.pid)

    queue = proc.subscribe(replay=False)
    assert await _wait_end(manager, proc, queue) == ("end", 0, "exited")

    buf = proc.captured["stdout"]
    assert len(buf) == 1024
    assert "stdout" in proc.captured_truncated
    assert buf.count(TRUNCATED_MARK) == 1
    # Head kept, tail replaced by the marker inside the cap.
    assert bytes(buf) == b"a" * 700 + b"b" * 298 + TRUNCATED_MARK

    assert _replay_data(proc) == bytes(buf)


async def test_exact_boundary_is_not_truncated():
    executor = _ChunkExecutor()
    manager = ProcessManager(executor, sandbox_id="sbx_edge", capture_limit_bytes=1024)
    proc = await manager.start(**_start_kwargs())

    executor.emit(proc.pid, b"x" * 512)
    executor.emit(proc.pid, b"y" * 512)
    executor.end(proc.pid)

    queue = proc.subscribe(replay=False)
    assert await _wait_end(manager, proc, queue) == ("end", 0, "exited")

    assert proc.captured_truncated == set()
    assert bytes(proc.captured["stdout"]) == b"x" * 512 + b"y" * 512


async def test_chunk_aligned_full_buffer_still_marks_truncation():
    """A stream that fills the cap exactly then continues must still mark it."""
    executor = _ChunkExecutor()
    manager = ProcessManager(
        executor, sandbox_id="sbx_aligned", capture_limit_bytes=1024 * 1024
    )
    proc = await manager.start(**_start_kwargs())

    for _ in range(17):  # 16 chunks fill 1MiB exactly; the 17th crosses.
        executor.emit(proc.pid, b"z" * 65536)
    executor.end(proc.pid)

    queue = proc.subscribe(replay=False)
    assert await _wait_end(manager, proc, queue) == ("end", 0, "exited")

    limit = 1024 * 1024
    buf = proc.captured["stdout"]
    assert len(buf) == limit
    assert buf.count(TRUNCATED_MARK) == 1
    assert bytes(buf[: -len(TRUNCATED_MARK)]) == b"z" * (limit - len(TRUNCATED_MARK))
    assert bytes(buf[-len(TRUNCATED_MARK):]) == TRUNCATED_MARK


async def test_dev_zero_stream_stays_capped():
    """A ``cat /dev/zero`` style unbounded stream cannot grow the buffer."""
    executor = _ChunkExecutor()
    manager = ProcessManager(executor, sandbox_id="sbx_zero", capture_limit_bytes=1024)
    proc = await manager.start(**_start_kwargs())

    for _ in range(300):
        executor.emit(proc.pid, b"\0" * 65536)
    executor.end(proc.pid)

    queue = proc.subscribe(replay=False)
    assert await _wait_end(manager, proc, queue) == ("end", 0, "exited")

    buf = proc.captured["stdout"]
    assert len(buf) == 1024
    assert buf.count(TRUNCATED_MARK) == 1
    assert bytes(buf[: -len(TRUNCATED_MARK)]) == b"\0" * (1024 - len(TRUNCATED_MARK))
    assert bytes(buf[-len(TRUNCATED_MARK):]) == TRUNCATED_MARK


async def test_live_subscribers_see_full_stream_plus_marker():
    executor = _ChunkExecutor()
    manager = ProcessManager(executor, sandbox_id="sbx_live", capture_limit_bytes=1024)
    proc = await manager.start(**_start_kwargs())
    collect_queue = proc.subscribe(replay=False)
    wait_queue = proc.subscribe(replay=False)

    executor.emit(proc.pid, b"a" * 700)
    await asyncio.sleep(0)
    executor.emit(proc.pid, b"b" * 700)
    await asyncio.sleep(0)
    executor.emit(proc.pid, b"c" * 700)
    await asyncio.sleep(0)
    executor.end(proc.pid)
    assert await _wait_end(manager, proc, wait_queue) == ("end", 0, "exited")

    items: list[tuple] = []
    while not collect_queue.empty():
        items.append(collect_queue.get_nowait())
    kinds = [item[0] for item in items]
    assert kinds == ["data", "data", "data", "data", "end"]
    data = [item[2] for item in items if item[0] == "data"]
    assert data == [b"a" * 700, b"b" * 700, TRUNCATED_MARK, b"c" * 700]


async def test_truncation_marker_written_to_command_logs(tmp_path):
    executor = _ChunkExecutor()
    writer = CommandLogWriter(tmp_path)

    def on_log(proc, event, payload):
        if event == "start":
            writer.start(proc.pid, proc.config.cmd)
        elif event in ("stdout", "stderr", "pty"):
            writer.write(proc.pid, event, payload)
        elif event == "end":
            writer.end(proc.pid, payload)

    manager = ProcessManager(
        executor,
        sandbox_id="sbx_log",
        capture_limit_bytes=1024,
        on_command_log=on_log,
    )
    proc = await manager.start(**_start_kwargs())

    executor.emit(proc.pid, b"a" * 700)
    executor.emit(proc.pid, b"b" * 700)
    executor.end(proc.pid)

    queue = proc.subscribe(replay=False)
    assert await _wait_end(manager, proc, queue) == ("end", 0, "exited")

    lines = [
        json.loads(raw)["line"]
        for raw in (tmp_path / "command-logs.jsonl").read_text().splitlines()
    ]
    # Capture cap crossed while the 1M-char log cap did not: the log shows the
    # full data plus exactly one truncation marker, then the exit line.
    assert lines == [
        "> cat",
        "a" * 700 + "b" * 700,
        "... output truncated ...",
        "exit: 0",
    ]


async def test_default_capture_limit_is_10mb():
    executor = _ChunkExecutor()
    manager = ProcessManager(executor, sandbox_id="sbx_default")
    proc = await manager.start(**_start_kwargs())
    assert proc.capture_limit == CAPTURE_LIMIT_DEFAULT
    executor.end(proc.pid)
    queue = proc.subscribe(replay=False)
    assert await _wait_end(manager, proc, queue) == ("end", 0, "exited")


async def test_capture_limit_none_is_unlimited():
    executor = _ChunkExecutor()
    manager = ProcessManager(
        executor, sandbox_id="sbx_unlimited", capture_limit_bytes=None
    )
    proc = await manager.start(**_start_kwargs())

    for _ in range(40):
        executor.emit(proc.pid, b"\0" * 65536)
    executor.end(proc.pid)

    queue = proc.subscribe(replay=False)
    assert await _wait_end(manager, proc, queue) == ("end", 0, "exited")

    assert proc.capture_limit is None
    assert proc.captured_truncated == set()
    assert len(proc.captured["stdout"]) == 40 * 65536


# -- SEC-K0S-003: the *live* path gets a budget too ------------------------
#
# ``capture_limit`` bounds what a replay can hand back; it says nothing about
# the queue a live subscriber reads from. That queue used to be an unbounded
# ``asyncio.Queue`` fed with ``put_nowait``, so a consumer that fell behind --
# or a client that stopped reading -- grew the *worker's* heap with whatever
# the command printed. Measured 2026-09-30: one ``yes`` OOMKilled
# ``e2b-worker-0`` (exit 137). These pin the budget, the inline marker, and the
# one event that must never be dropped.


def _drain(queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


async def _wait_ended(proc, timeout: float = 5.0) -> None:
    """Wait for the *producer* to finish without draining the subscriber."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not proc.ended:
        assert loop.time() < deadline, "the command never reached its end"
        await asyncio.sleep(0.01)


async def test_a_slow_subscriber_is_bounded_and_told_where_it_was_cut():
    executor = _ChunkExecutor()
    limit = 1024 * 1024
    manager = ProcessManager(
        executor, sandbox_id="sbx_slow", stream_limit_bytes=limit
    )
    proc = await manager.start(**_start_kwargs())
    queue = proc.subscribe(replay=False)

    chunk = b"y" * 65536
    produced = 64  # 4 MiB, four times the budget
    for _ in range(produced):
        executor.emit(proc.pid, chunk)
    executor.end(proc.pid)
    await _wait_ended(proc)

    items = _drain(queue)
    payload = b"".join(item[2] for item in items if item[0] == "data")
    delivered = len(payload) - len(TRUNCATED_MARK)

    assert items[-1] == ("end", 0, "exited")
    assert len(payload) <= limit
    assert payload.count(TRUNCATED_MARK) == 1
    assert queue.max_bytes == limit
    assert queue.dropped_bytes == produced * len(chunk) - delivered


async def test_a_subscriber_that_keeps_up_sees_every_byte():
    """A consumer that drains as the command produces loses nothing.

    The producer yields between chunks, as a real one does (each chunk comes
    off a pipe read, which suspends): a burst pushed within a single event-loop
    turn would outrun *any* consumer, which is the slow-consumer case the
    budget exists for -- not this one.
    """
    executor = _ChunkExecutor()
    manager = ProcessManager(
        executor, sandbox_id="sbx_fast", stream_limit_bytes=1024 * 1024
    )
    proc = await manager.start(**_start_kwargs())
    queue = proc.subscribe(replay=False)

    chunk = b"y" * 65536
    seen = bytearray()
    end: tuple | None = None

    async def consume() -> None:
        nonlocal end
        while True:
            item = await queue.get()
            if item[0] == "data":
                seen.extend(item[2])
            else:
                end = item
                return

    task = asyncio.create_task(consume())
    for _ in range(64):
        executor.emit(proc.pid, chunk)
        await asyncio.sleep(0)  # the producer yields; the consumer drains
    executor.end(proc.pid)
    await asyncio.wait_for(task, 5)

    assert bytes(seen) == chunk * 64
    assert queue.dropped_bytes == 0
    assert end == ("end", 0, "exited")


async def test_the_end_event_survives_the_budget():
    """A client that never learns the command ended would hang on the stream."""
    executor = _ChunkExecutor()
    manager = ProcessManager(
        executor, sandbox_id="sbx_tiny", stream_limit_bytes=1024
    )
    proc = await manager.start(**_start_kwargs())
    queue = proc.subscribe(replay=False)

    for _ in range(8):
        executor.emit(proc.pid, b"z" * 65536)
    executor.end(proc.pid)
    await _wait_ended(proc)

    items = _drain(queue)
    assert items[-1] == ("end", 0, "exited")
    payload = b"".join(item[2] for item in items if item[0] == "data")
    assert payload == TRUNCATED_MARK
    assert queue.dropped_bytes == 8 * 65536
