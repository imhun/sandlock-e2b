"""Process table and event fan-out for the envd service."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from gateway_common.errors import ConnectError, not_found, resource_exhausted
from envd_service.executors.base import (
    ExecConfig,
    Executor,
    FailedRunningProcess,
    RunningProcess,
)
from envd_service.process.stream_budget import (
    STREAM_LIMIT_DEFAULT,
    STREAM_QUEUE_MAX_ITEMS,
    TRUNCATED_MARK,
    ByteBudgetQueue,
)
from envd_service.process.stream_budget import item_bytes as _item_bytes

logger = logging.getLogger(__name__)

# E4.1: per-stream capture cap for ``ManagedProcess.captured``. A sandbox
# command like ``cat /dev/zero`` would otherwise grow the bytearray without
# bound and exhaust the worker's memory. Once a stream crosses the cap the
# tail is replaced by the marker inside the cap, so replays and logs show
# exactly where output was dropped.
CAPTURE_LIMIT_DEFAULT = 10 * 1024 * 1024
TRUNCATED_MARK = b"\n... output truncated ...\n"

# SEC-K0S-003 (found 2026-09-30, fixed 2026-10-03): the budget of a *live*
# subscriber's queue, in bytes. ``capture_limit`` above only bounds what a
# replay can hand back -- it says nothing about the queue a streaming client
# reads from, which used to be an unbounded ``asyncio.Queue`` fed with
# ``put_nowait``. A consumer that fell behind (or a client that stopped
# reading) therefore grew the **worker's** heap with whatever the command
# printed: measured on the live cluster, one ``yes`` OOMKilled
# ``e2b-worker-0`` (exit 137). ``None`` = unlimited.
class SubscriberQueue(ByteBudgetQueue):
    """One subscriber's queue, bounded by bytes *and* items (SEC-K0S-003).

    ``put_nowait`` raises ``asyncio.QueueFull`` for a data item that would
    exceed the budget -- which is what turns :meth:`ManagedProcess._broadcast`'s
    drop branch from dead code into real backpressure. Control items (the end
    event, the truncation marker) are always admitted: a client that never
    learns the command ended would hang on the stream, and if the *item* bound
    is what stands in the way, the oldest data item makes room for them.

    ``dropped_bytes`` is the subscriber's own accounting, so a truncated stream
    is never silent: the producer logs the first drop and the marker lands in
    the stream at the cut point.
    """

    def put_nowait(self, item: tuple, *, force: bool = False) -> None:
        if force or not item or item[0] != "data":
            self.put_control(item)
            return
        super().put_nowait(item)


SIGNAL_MAP: dict[str, int] = {
    "SIGNAL_SIGTERM": signal.SIGTERM,
    "SIGNAL_SIGKILL": signal.SIGKILL,
    "SIGTERM": signal.SIGTERM,
    "SIGKILL": signal.SIGKILL,
    "15": signal.SIGTERM,
    "9": signal.SIGKILL,
}


def parse_signal(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    key = str(value or "")
    if key in SIGNAL_MAP:
        return SIGNAL_MAP[key]
    raise ConnectError(
        "invalid_argument", f"unsupported signal: {value}", 400
    )


@dataclass
class ManagedProcess:
    pid: int
    config: ExecConfig
    tag: str | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    captured: dict[str, bytearray] = field(
        default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray(), "pty": bytearray()}
    )
    # ``None`` = unlimited; otherwise the cap per stream in bytes.
    capture_limit: int | None = CAPTURE_LIMIT_DEFAULT
    # SEC-K0S-003: the budget of this subscriber-facing stream, in bytes.
    # ``None`` = unlimited. Separate from ``capture_limit`` on purpose: one
    # bounds the replay buffer the worker keeps, the other bounds what it will
    # hold for a *live* consumer that is not keeping up.
    stream_limit: int | None = STREAM_LIMIT_DEFAULT
    # Streams that crossed the capture cap (their replay ends with the marker).
    captured_truncated: set[str] = field(default_factory=set)
    #: Worker bookkeeping (the workspace writer's helpers), not a user command.
    #: ``pause_all`` cannot freeze one of these the way it freezes user work:
    #: see the note there.
    internal: bool = False
    ended: bool = False
    exit_code: int | None = None
    killed: bool = False
    _running: RunningProcess | None = field(default=None, repr=False)

    def subscribe(self, *, replay: bool) -> SubscriberQueue:
        queue = SubscriberQueue(max_bytes=self.stream_limit)
        if replay:
            for kind, buf in self.captured.items():
                data = bytes(buf)
                if data:
                    # The replay is already bounded by ``capture_limit``, so it
                    # goes in as a control item: a tight stream budget must not
                    # refuse the history the caller explicitly asked for.
                    queue.put_nowait(("data", kind, data), force=True)
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self.subscribers.remove(queue)
        except ValueError:
            return
        dropped = getattr(queue, "dropped_bytes", 0)
        if dropped:
            logger.warning(
                "process %s: a subscriber left with %d bytes dropped from its "
                "live stream (budget %s bytes); the marker in the stream says "
                "where the cut is",
                self.pid,
                dropped,
                getattr(queue, "max_bytes", None),
            )


def append_captured(proc: ManagedProcess, kind: str, chunk: bytes) -> bool:
    """Append ``chunk`` to the capped capture buffer for ``kind``.

    Keeps the first ``capture_limit`` bytes of each stream; the truncation
    marker replaces the tail inside the cap so a replay never exceeds the
    cap and still marks the drop point. Returns ``True`` when this call
    crossed the cap (the caller emits the marker to logs/subscribers).
    """
    if kind in proc.captured_truncated:
        return False
    buf = proc.captured[kind]
    limit = proc.capture_limit
    if limit is None:
        buf.extend(chunk)
        return False
    room = limit - len(buf)
    if room <= 0:
        # The buffer is already exactly full (e.g. chunk-aligned output like
        # /dev/zero reads): the marker still replaces the tail so replays
        # mark the truncation point.
        marker = TRUNCATED_MARK
        if len(marker) <= limit:
            del buf[limit - len(marker):]
            buf.extend(marker)
        proc.captured_truncated.add(kind)
        return True
    if len(chunk) <= room:
        buf.extend(chunk)
        return False
    buf.extend(chunk[:room])
    marker = TRUNCATED_MARK
    if len(marker) <= limit:
        del buf[limit - len(marker):]
        buf.extend(marker)
    proc.captured_truncated.add(kind)
    return True


class _CommandGate:
    """Per-sandbox command gate: bounded concurrency + bounded wait queue.

    At most ``max_concurrent`` commands hold a slot at once; further commands
    wait FIFO. When ``max_queued`` commands are already waiting, a new acquire
    fails fast with a 429 ``resource_exhausted`` error so an unbounded backlog
    cannot build up. A waiter that waits longer than ``queue_timeout`` seconds
    also fails with 429. ``remove()`` revokes the gate: every queued waiter
    and every future acquire raises ``not_found`` so no command can spawn on a
    deleted sandbox.
    """

    def __init__(
        self,
        sandbox_id: str,
        max_concurrent: int,
        max_queued: int,
        queue_timeout: float | None,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._max_concurrent = max_concurrent
        self._max_queued = max_queued
        self._queue_timeout = queue_timeout
        self._available = max_concurrent
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._removed = False

    def _removed_error(self) -> ConnectError:
        return not_found(
            f"sandbox {self._sandbox_id} removed; queued command canceled"
        )

    async def acquire(self) -> None:
        if self._removed:
            raise self._removed_error()
        if self._available > 0:
            self._available -= 1
            return
        if len(self._waiters) >= self._max_queued:
            raise resource_exhausted(
                f"sandbox {self._sandbox_id}: too many concurrent commands "
                f"(running limit {self._max_concurrent}, "
                f"queue limit {self._max_queued})"
            )
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            if self._queue_timeout is None:
                await waiter
            else:
                try:
                    await asyncio.wait_for(waiter, timeout=self._queue_timeout)
                except asyncio.TimeoutError:
                    self._discard_waiter(waiter)
                    raise resource_exhausted(
                        f"sandbox {self._sandbox_id}: command queue timed out "
                        f"after {self._queue_timeout:g}s"
                    )
        except asyncio.CancelledError:
            self._discard_waiter(waiter)
            raise
        if self._removed:
            raise self._removed_error()
        # The slot was handed over directly by release(); nothing to consume.

    def release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            waiter.set_result(None)
            return
        self._available += 1

    def remove(self) -> None:
        """Revoke the gate: queued and future acquires fail immediately."""
        self._removed = True
        err = self._removed_error()
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(err)

    def _discard_waiter(self, waiter: asyncio.Future[None]) -> None:
        queued = True
        try:
            self._waiters.remove(waiter)
        except ValueError:
            queued = False
        if not queued and waiter.done() and not waiter.cancelled():
            # The slot had already been handed to this waiter (e.g. it was
            # cancelled after release() woke it); give the slot back so the
            # queue cannot stall. Hand it to the next waiter if any -- the
            # slot is transferred, not freed, so _available must not also be
            # incremented (that would double-count the slot and break the
            # mutual-exclusion invariant).
            while self._waiters:
                next_waiter = self._waiters.popleft()
                if not next_waiter.done():
                    next_waiter.set_result(None)
                    return
            self._available += 1


class ProcessManager:
    """Owns all running commands/PTYs of one sandbox runtime."""

    def __init__(
        self,
        executor: Executor,
        max_command_timeout: int = 3600,
        on_command_log=None,
        *,
        sandbox_id: str | None = None,
        max_concurrent_commands: int = 1,
        max_queued_commands: int | None = None,
        queue_timeout_s: float | None = 30,
        capture_limit_bytes: int | None = CAPTURE_LIMIT_DEFAULT,
        stream_limit_bytes: int | None = STREAM_LIMIT_DEFAULT,
    ) -> None:
        self._executor = executor
        self._max_command_timeout = max_command_timeout
        self._processes: dict[int, ManagedProcess] = {}
        # Optional ``callback(proc, event, payload)`` for command output
        # logging; events are start/stdout/stderr/pty/end.
        self._on_command_log = on_command_log
        self._sandbox_id = sandbox_id or "default"
        self._max_concurrent_commands = max(1, int(max_concurrent_commands))
        if max_queued_commands is None:
            self._max_queued_commands = self._max_concurrent_commands
        else:
            self._max_queued_commands = max(0, int(max_queued_commands))
        self._queue_timeout = (
            None
            if queue_timeout_s is None
            else max(0.0, float(queue_timeout_s))
        )
        # E4.1: ``None`` = unlimited; the default is the 10MB cap. Settings
        # map ``E2B_COMMAND_CAPTURE_LIMIT_MB=0`` to ``None`` before this
        # point (repo convention: 0 disables a dimension).
        self._capture_limit_bytes = (
            None if capture_limit_bytes is None else max(0, int(capture_limit_bytes))
        )
        # SEC-K0S-003: ``None`` or a non-positive value = unlimited (the repo
        # convention: 0 disables a dimension). This is the live-stream budget,
        # separate from the replay cap above.
        self._stream_limit_bytes = (
            None
            if stream_limit_bytes is None or int(stream_limit_bytes) <= 0
            else int(stream_limit_bytes)
        )
        # Per-sandbox gates; one entry per sandbox served by this manager.
        self._locks: dict[str, _CommandGate] = {}
        self._removed_sandboxes: set[str] = set()

    def _gate(self) -> _CommandGate:
        if self._sandbox_id in self._removed_sandboxes:
            raise not_found(
                f"sandbox {self._sandbox_id} removed; command rejected"
            )
        gate = self._locks.get(self._sandbox_id)
        if gate is None:
            gate = _CommandGate(
                self._sandbox_id,
                self._max_concurrent_commands,
                self._max_queued_commands,
                self._queue_timeout,
            )
            self._locks[self._sandbox_id] = gate
        return gate

    def remove_sandbox(self, sandbox_id: str | None = None) -> None:
        """Revoke the per-sandbox gate (called on sandbox deletion).

        Queued commands fail with ``not_found`` instead of spawning on the
        deleted sandbox, and later commands are rejected the same way.
        """
        target = sandbox_id or self._sandbox_id
        gate = self._locks.pop(target, None)
        if gate is not None:
            gate.remove()
        self._removed_sandboxes.add(target)

    async def start(
        self,
        *,
        cmd: list[str],
        env: dict[str, str],
        cwd: str,
        stdin_enabled: bool,
        pty_size: tuple[int, int] | None = None,
        tag: str | None = None,
        internal: bool = False,
        max_file_size: int | None = None,
    ) -> ManagedProcess:
        """Start a command; ``internal`` marks worker bookkeeping (N28).

        The workspace writer runs *inside* the sandbox (that is what makes the
        sandbox the single writer of its tree), so its helper processes go
        through this same table -- but they are not user commands, and with
        both of the following differences:

        * they do **not** take the per-sandbox command gate. ``E2B_MAX_
          CONCURRENT_COMMANDS_PER_SANDBOX`` defaults to 1, so a long-running
          user command holds the gate for its whole lifetime; a write that
          queued behind it would time out (30s) and fail with 429 even though
          nothing is contended. The gate bounds *user* concurrency, and the
          writer is the platform acting as the sandbox.
        * they are **not** written to the command log. That log is the
          sandbox's command history, surfaced to the SDK as ``record.logs``;
          a ``cat > file`` the caller never ran must not appear there.
        """
        pty = pty_size is not None
        rows, cols = pty_size or (24, 80)
        config = ExecConfig(
            cmd=list(cmd),
            env=dict(env),
            cwd=cwd,
            stdin_enabled=stdin_enabled,
            pty=pty,
            rows=rows,
            cols=cols,
            max_file_size=max_file_size,
        )
        # Per-sandbox gate: commands to the same sandbox serialize on write
        # access. Concurrent commands queue; a full wait queue fails fast
        # with 429. The gate stays held until the command ends (released by
        # _drive), so with the default limit of 1 commands run strictly
        # serially.
        gate = None if internal else self._gate()
        if gate is not None:
            await gate.acquire()
        try:
            try:
                running = await self._executor.start(config)
            except FileNotFoundError:
                running = FailedRunningProcess(
                    f"envd: {cmd[0] if cmd else ''}: command not found\n"
                )
            except Exception as e:
                running = FailedRunningProcess(str(e) or type(e).__name__)
            proc = ManagedProcess(
                pid=running.pid,
                config=config,
                tag=tag,
                internal=internal,
                _running=running,
                capture_limit=self._capture_limit_bytes,
                stream_limit=self._stream_limit_bytes,
            )
            # An internal process still enters the table (shutdown's
            # ``kill_all`` and the pause/resume walk must see it), but with a
            # capture limit of 0: nothing replays it, so its output only needs
            # to be drained, not kept.
            if internal:
                proc.capture_limit = 0
                proc.captured = {}
            self._processes[proc.pid] = proc
            if self._on_command_log is not None and not internal:
                self._on_command_log(proc, "start", None)
            asyncio.create_task(self._drive(proc, running, gate, internal=internal))
            return proc
        except BaseException:
            # Cancel/error before _drive was scheduled must not leak the slot.
            if gate is not None:
                gate.release()
            raise

    async def _drive(
        self,
        proc: ManagedProcess,
        running: RunningProcess,
        gate: _CommandGate | None,
        *,
        internal: bool = False,
    ) -> None:
        try:
            timed_out = False

            async def _watchdog() -> None:
                nonlocal timed_out
                try:
                    await asyncio.sleep(self._max_command_timeout)
                    timed_out = True
                    running.kill(signal.SIGKILL)
                except asyncio.CancelledError:
                    pass

            watchdog = asyncio.create_task(_watchdog())
            try:
                async for kind, chunk in running.output():
                    if kind not in ("stdout", "stderr", "pty"):
                        continue
                    buf = proc.captured.get(kind)
                    if buf is not None:
                        truncated_now = append_captured(proc, kind, chunk)
                    else:
                        truncated_now = False
                    if self._on_command_log is not None:
                        if not internal:
                            self._on_command_log(proc, kind, chunk)
                    self._broadcast(proc, ("data", kind, chunk))
                    if truncated_now:
                        marker = TRUNCATED_MARK
                        if self._on_command_log is not None and not internal:
                            self._on_command_log(proc, kind, marker)
                        self._broadcast(proc, ("data", kind, marker))
                exit_code = await running.exit_code()
            except asyncio.CancelledError:
                running.kill(signal.SIGKILL)
                watchdog.cancel()
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("process %s output loop failed", proc.pid)
                exit_code = -1
            finally:
                watchdog.cancel()

            proc.exit_code = exit_code
            proc.ended = True
            status = "killed" if (timed_out or exit_code < 0) else "exited"
            if self._on_command_log is not None and not internal:
                self._on_command_log(proc, "end", exit_code)
            self._broadcast(proc, ("end", exit_code, status))
            self._processes.pop(proc.pid, None)
        finally:
            if gate is not None:
                gate.release()

    @staticmethod
    def _broadcast(proc: ManagedProcess, item: tuple) -> None:
        for queue in list(proc.subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                # SEC-K0S-003: this branch used to be dead code (the queue was
                # unbounded), which is how one command's output could grow the
                # worker until the OOM killer took it. Now it is the budget:
                # count the drop, and tell the subscriber *once* where its
                # stream was cut -- inline, with the same marker the replay
                # path uses, so a truncated stream is never silent.
                if item and item[0] != "data":
                    queue.put_nowait(item, force=True)
                    continue
                if not isinstance(queue, SubscriberQueue) or not queue.note_dropped(
                    _item_bytes(item)
                ):
                    continue
                logger.warning(
                    "process %s: a subscriber fell behind; dropping its output "
                    "from here on (budget %s bytes) and marking the stream "
                    "truncated",
                    proc.pid,
                    queue.max_bytes,
                )
                queue.put_nowait(
                    ("data", item[1], TRUNCATED_MARK), force=True
                )

    def get(self, pid: int) -> ManagedProcess:
        proc = self._processes.get(pid)
        if proc is None:
            raise not_found(f"Process {pid} not found")
        return proc

    def get_or_none(self, pid: int) -> ManagedProcess | None:
        return self._processes.get(pid)

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for proc in self._processes.values():
            cfg = proc.config
            item: dict[str, Any] = {
                "config": {
                    "cmd": cfg.cmd[0] if cfg.cmd else "",
                    "args": cfg.cmd[1:] if len(cfg.cmd) > 1 else [],
                    "envs": cfg.env,
                    "cwd": cfg.cwd,
                },
                "pid": proc.pid,
            }
            if proc.tag:
                item["tag"] = proc.tag
            out.append(item)
        return out

    def send_input(self, pid: int, data: bytes) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.send_stdin(data)

    async def feed_stdin(self, pid: int, data: bytes) -> None:
        """Feed stdin with backpressure (N28; see ``feed_stdin`` in base.py)."""
        proc = self.get(pid)
        if proc._running is None:
            raise not_found(f"Process {pid} not found")
        await proc._running.feed_stdin(data)

    def close_stdin(self, pid: int) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.close_stdin()

    def send_signal(self, pid: int, sig: int) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.kill(sig)
        if sig == signal.SIGKILL:
            # Deterministic second-kill semantics: mark dead immediately so a
            # subsequent kill returns NOT_FOUND while the end event still
            # reaches live streams through the driver.
            proc.killed = True
            self._processes.pop(pid, None)

    def update(self, pid: int, rows: int, cols: int) -> None:
        proc = self.get(pid)
        if proc._running is not None:
            proc._running.resize(rows, cols)

    def kill_all(self) -> None:
        for pid, proc in list(self._processes.items()):
            if proc._running is not None:
                proc._running.kill(signal.SIGKILL)
        self._processes.clear()

    def pause_all(self) -> None:
        """Freeze every running process tree (SIGSTOP).

        Group-first by design (M4 D5): under sandlock fork F1.7 every confined
        exec child is its own process-group leader (host pid == pgid), so
        ``killpg`` covers the shell and its descendants in one stop. Fall back
        to a direct per-process SIGSTOP only when the group is already gone or
        the group signal is not permitted -- and only for backends whose
        ``kill(sig)`` genuinely delivers the signal (``supports_signal_pause``).
        A backend that cannot signal-pause (sandlock: ``kill`` always SIGKILLs)
        is WARNINGed and skipped, so a pause never silently kills a child
        (FUP #8).
        """
        for proc in list(self._processes.values()):
            if proc._running is None:
                continue
            if proc.internal:
                # A workspace write that is already in flight (N28/B) is not
                # "frozen work" the way a user command is. Freezing it means
                # the caller's request hangs inside the sandbox until the
                # sandbox is resumed -- measured as an nginx 504 after its 60 s
                # timeout, with the write's own temp file left behind -- and a
                # paused sandbox that still has a write growing is exactly what
                # the pause exists to stop. Kill it instead: the writer turns
                # the signal into the same "paused, resume and retry" refusal
                # the gate hands out, and takes the temp file with it.
                self.send_signal(proc.pid, signal.SIGKILL)
                continue
            try:
                import os

                os.killpg(os.getpgid(proc.pid), signal.SIGSTOP)
            except (ProcessLookupError, PermissionError):
                if not proc._running.supports_signal_pause:
                    logger.warning(
                        "pause fallback skipped pid=%s cmd=%s: running "
                        "backend cannot signal-pause (kill(sig) SIGKILLs); "
                        "child stays running",
                        proc.pid,
                        " ".join(proc.config.cmd),
                    )
                    continue
                try:
                    proc._running.kill(signal.SIGSTOP)
                except Exception:
                    pass

    def resume_all(self) -> None:
        """Unfreeze every running process tree (SIGCONT).

        Mirrors ``pause_all``: SIGCONT the child's process group first, then
        fall back to the process itself when the group no longer exists or the
        group signal is not permitted (M4 D5). The same capability gate as
        ``pause_all`` applies: a backend whose ``kill`` cannot deliver SIGCONT
        (``supports_signal_pause`` False, e.g. sandlock's SIGKILL-only kill)
        is WARNINGed and skipped so a resume never silently kills a child
        (FUP #8).
        """
        for proc in list(self._processes.values()):
            if proc._running is None:
                continue
            try:
                import os

                os.killpg(os.getpgid(proc.pid), signal.SIGCONT)
            except (ProcessLookupError, PermissionError):
                if not proc._running.supports_signal_pause:
                    logger.warning(
                        "resume fallback skipped pid=%s cmd=%s: running "
                        "backend cannot signal-pause (kill(sig) SIGKILLs); "
                        "child stays paused",
                        proc.pid,
                        " ".join(proc.config.cmd),
                    )
                    continue
                try:
                    proc._running.kill(signal.SIGCONT)
                except Exception:
                    pass

    async def wait_ended(self, proc: ManagedProcess, queue: asyncio.Queue):
        """Consume a subscriber queue until the end event."""
        while True:
            item = await queue.get()
            if item[0] == "end":
                return item
