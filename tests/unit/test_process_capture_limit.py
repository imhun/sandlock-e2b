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
