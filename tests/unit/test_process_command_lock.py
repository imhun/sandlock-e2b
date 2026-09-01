"""Per-sandbox command serialization (E2.3): mutual exclusion + 429 queue limit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from envd_service.executors.base import ExecConfig, Executor, RunningProcess
from envd_service.executors.local import LocalExecutor
from envd_service.process.manager import ProcessManager
from gateway_common.errors import ConnectError


class _FakeProcess(RunningProcess):
    """Synthetic process that ends when its ``done`` event is set."""

    def __init__(self, pid: int, done: asyncio.Event) -> None:
        self._pid = pid
        self._done = done

    @property
    def pid(self) -> int:
        return self._pid

    async def output(self) -> AsyncIterator[tuple[str, bytes]]:
        await self._done.wait()
        yield ("stdout", b"")

    def send_stdin(self, data: bytes) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    def resize(self, rows: int, cols: int) -> None:
        pass

    def kill(self, sig: int) -> None:
        self._done.set()

    async def exit_code(self) -> int:
        return 0


class _FakeExecutor(Executor):
    """Tracks command enter/end order; processes end on ``finish(pid)``."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.live: dict[int, _FakeProcess] = {}
        self._names: dict[int, str] = {}
        self._next_pid = 1000

    async def start(self, config: ExecConfig) -> RunningProcess:
        name = config.cmd[0]
        self.events.append(f"enter:{name}")
        pid = self._next_pid
        self._next_pid += 1
        proc = _FakeProcess(pid, asyncio.Event())
        self.live[pid] = proc
        self._names[pid] = name
        return proc

    def finish(self, pid: int) -> None:
        self.events.append(f"end:{self._names[pid]}")
        self.live[pid]._done.set()


async def _wait_end(
    manager: ProcessManager, proc, queue, timeout: float = 5
) -> tuple:
    return await asyncio.wait_for(
        manager.wait_ended(proc, queue), timeout=timeout
    )


def _start_kwargs(cmd: str) -> dict:
    return {"cmd": [cmd], "env": {}, "cwd": "/workspace", "stdin_enabled": False}


@pytest.mark.asyncio
async def test_same_sandbox_commands_run_serially():
    executor = _FakeExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_serial",
        max_concurrent_commands=1,
        max_queued_commands=1,
    )
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    assert executor.events == ["enter:a"]
    queue_a = proc_a.subscribe(replay=False)

    task_b = asyncio.create_task(manager.start(**_start_kwargs("b")))
    await asyncio.sleep(0)  # b reaches the gate and queues behind a
    assert executor.events == ["enter:a"]  # b must not start while a is live
    assert not task_b.done()

    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")
    proc_b = await asyncio.wait_for(task_b, timeout=5)
    queue_b = proc_b.subscribe(replay=False)
    executor.finish(proc_b.pid)
    assert await _wait_end(manager, proc_b, queue_b) == ("end", 0, "exited")

    # Strict serial order: b entered only after a ended.
    assert executor.events == ["enter:a", "end:a", "enter:b", "end:b"]
    assert manager.get_or_none(proc_a.pid) is None
    assert manager.get_or_none(proc_b.pid) is None


@pytest.mark.asyncio
async def test_queue_over_default_limit_returns_429():
    executor = _FakeExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_429",
        max_concurrent_commands=1,  # max_queued defaults to 1
    )
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)
    task_b = asyncio.create_task(manager.start(**_start_kwargs("b")))
    await asyncio.sleep(0)  # b queues behind a

    with pytest.raises(ConnectError) as exc_info:
        await asyncio.wait_for(manager.start(**_start_kwargs("c")), timeout=5)
    err = exc_info.value
    assert err.code == "resource_exhausted"
    assert err.http_status == 429
    assert "sbx_429" in err.message

    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")
    proc_b = await asyncio.wait_for(task_b, timeout=5)
    queue_b = proc_b.subscribe(replay=False)
    executor.finish(proc_b.pid)
    assert await _wait_end(manager, proc_b, queue_b) == ("end", 0, "exited")

    # c was rejected and never started; a and b both ran and completed.
    assert executor.events == ["enter:a", "end:a", "enter:b", "end:b"]


@pytest.mark.asyncio
async def test_zero_queued_rejects_second_concurrent_command():
    executor = _FakeExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_noqueue",
        max_concurrent_commands=1,
        max_queued_commands=0,
    )
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)

    with pytest.raises(ConnectError) as exc_info:
        await manager.start(**_start_kwargs("b"))
    assert exc_info.value.code == "resource_exhausted"
    assert exc_info.value.http_status == 429

    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")
    assert executor.events == ["enter:a", "end:a"]


@pytest.mark.asyncio
async def test_max_concurrent_above_one_allows_parallel_commands():
    executor = _FakeExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_parallel",
        max_concurrent_commands=2,
        max_queued_commands=2,
    )
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)
    task_b = asyncio.create_task(manager.start(**_start_kwargs("b")))
    proc_b = await asyncio.wait_for(task_b, timeout=5)
    # b started while a was still running: parallelism, no wait for a's end.
    assert executor.events == ["enter:a", "enter:b"]
    queue_b = proc_b.subscribe(replay=False)

    executor.finish(proc_a.pid)
    executor.finish(proc_b.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")
    assert await _wait_end(manager, proc_b, queue_b) == ("end", 0, "exited")


@pytest.mark.asyncio
async def test_remove_sandbox_drops_lock_registry_entry():
    executor = _FakeExecutor()
    manager = ProcessManager(executor, sandbox_id="sbx_cleanup")
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)
    assert list(manager._locks) == ["sbx_cleanup"]

    manager.remove_sandbox()
    assert manager._locks == {}

    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")


@pytest.mark.asyncio
async def test_remove_sandbox_cancels_queued_command():
    executor = _FakeExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_removed",
        max_concurrent_commands=1,
        max_queued_commands=1,
    )
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)
    assert executor.events == ["enter:a"]

    task_b = asyncio.create_task(manager.start(**_start_kwargs("b")))
    await asyncio.sleep(0)  # b queues behind a
    assert not task_b.done()

    manager.remove_sandbox()

    with pytest.raises(ConnectError) as exc_info:
        await asyncio.wait_for(task_b, timeout=5)
    err = exc_info.value
    assert err.code == "not_found"
    assert err.http_status == 404
    assert "sbx_removed" in err.message

    # b was canceled before spawning; a still completes normally.
    assert executor.events == ["enter:a"]
    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")
    assert executor.events == ["enter:a", "end:a"]


@pytest.mark.asyncio
async def test_start_after_remove_sandbox_rejected():
    executor = _FakeExecutor()
    manager = ProcessManager(executor, sandbox_id="sbx_gone")
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)

    manager.remove_sandbox()

    with pytest.raises(ConnectError) as exc_info:
        await manager.start(**_start_kwargs("b"))
    assert exc_info.value.code == "not_found"
    assert "sbx_gone" in exc_info.value.message
    assert executor.events == ["enter:a"]  # b never spawned

    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")


@pytest.mark.asyncio
async def test_queue_timeout_returns_429_and_slot_not_lost():
    executor = _FakeExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_timeout",
        max_concurrent_commands=1,
        max_queued_commands=1,
        queue_timeout_s=0.05,
    )
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)

    task_b = asyncio.create_task(manager.start(**_start_kwargs("b")))
    await asyncio.sleep(0)  # b queues behind a
    assert not task_b.done()

    with pytest.raises(ConnectError) as exc_info:
        await asyncio.wait_for(task_b, timeout=5)
    err = exc_info.value
    assert err.code == "resource_exhausted"
    assert err.http_status == 429
    assert "sbx_timeout" in err.message
    assert "timed out" in err.message

    # b timed out without ever spawning; c must be able to queue afterwards
    # (the timed-out waiter did not consume the slot).
    assert executor.events == ["enter:a"]
    task_c = asyncio.create_task(manager.start(**_start_kwargs("c")))
    await asyncio.sleep(0)
    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")
    proc_c = await asyncio.wait_for(task_c, timeout=5)
    queue_c = proc_c.subscribe(replay=False)
    executor.finish(proc_c.pid)
    assert await _wait_end(manager, proc_c, queue_c) == ("end", 0, "exited")
    assert executor.events == ["enter:a", "end:a", "enter:c", "end:c"]


@pytest.mark.asyncio
async def test_cancel_after_slot_handoff_does_not_overcount_available():
    """Cancelling a waiter after release() handed it the slot must transfer
    (not also free) that slot: a new acquire must queue, never return
    immediately while another command holds the slot (E2.3 Critical)."""
    executor = _FakeExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_handoff",
        max_concurrent_commands=1,
        max_queued_commands=3,
        queue_timeout_s=None,
    )
    task_a = asyncio.create_task(manager.start(**_start_kwargs("a")))
    proc_a = await asyncio.wait_for(task_a, timeout=5)
    queue_a = proc_a.subscribe(replay=False)
    assert executor.events == ["enter:a"]

    task_b = asyncio.create_task(manager.start(**_start_kwargs("b")))
    task_c = asyncio.create_task(manager.start(**_start_kwargs("c")))
    await asyncio.sleep(0)  # b and c queue behind a
    assert not task_b.done()
    assert not task_c.done()
    assert executor.events == ["enter:a"]

    # a ends: _drive broadcasts the end event before gate.release(), so our
    # _wait_end wakeup is queued ahead of b's wakeup. release() has already
    # handed the slot to b (waiter popped and resolved) but b has not
    # resumed yet -- the deterministic handoff window.
    executor.finish(proc_a.pid)
    assert await _wait_end(manager, proc_a, queue_a) == ("end", 0, "exited")
    task_b.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task_b, timeout=5)

    # b's slot must be transferred to c, not also returned to _available:
    # the gate must not expose a free slot while c is the sole holder.
    assert manager._locks["sbx_handoff"]._available == 0
    proc_c = await asyncio.wait_for(task_c, timeout=5)
    queue_c = proc_c.subscribe(replay=False)
    assert executor.events == ["enter:a", "end:a", "enter:c"]

    # A new acquire must queue behind c, not return immediately.
    task_d = asyncio.create_task(manager.start(**_start_kwargs("d")))
    await asyncio.sleep(0)
    assert not task_d.done()
    assert executor.events == ["enter:a", "end:a", "enter:c"]

    executor.finish(proc_c.pid)
    assert await _wait_end(manager, proc_c, queue_c) == ("end", 0, "exited")
    proc_d = await asyncio.wait_for(task_d, timeout=5)
    queue_d = proc_d.subscribe(replay=False)
    executor.finish(proc_d.pid)
    assert await _wait_end(manager, proc_d, queue_d) == ("end", 0, "exited")
    assert executor.events == [
        "enter:a",
        "end:a",
        "enter:c",
        "end:c",
        "enter:d",
        "end:d",
    ]


@pytest.mark.asyncio
async def test_real_executor_commands_run_serially(workspace):
    executor = LocalExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_local",
        max_concurrent_commands=1,
    )

    async def run(script: str) -> tuple[tuple, int]:
        proc = await manager.start(
            cmd=["/bin/sh", "-c", script],
            env={},
            cwd=str(workspace),
            stdin_enabled=False,
        )
        queue = proc.subscribe(replay=False)
        return (await _wait_end(manager, proc, queue), proc.pid)

    task_a = asyncio.create_task(run("sleep 0.3; printf 'a\\n' >> out.txt"))
    task_b = asyncio.create_task(run("printf 'b\\n' >> out.txt"))
    (end_a, pid_a), (end_b, pid_b) = await asyncio.gather(task_a, task_b)

    assert end_a == ("end", 0, "exited")
    assert end_b == ("end", 0, "exited")
    assert pid_a != pid_b
    # b's writer only ran after a's command ended: strict serial order.
    assert (workspace / "out.txt").read_text() == "a\nb\n"
