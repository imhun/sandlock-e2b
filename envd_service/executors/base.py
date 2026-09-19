"""Executor interface shared by local and Sandlock backends."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecConfig:
    """Normalized command configuration."""

    cmd: list[str]
    env: dict[str, str]
    cwd: str
    stdin_enabled: bool
    pty: bool = False
    rows: int = 24
    cols: int = 80
    #: N25/C: tighten this command's ``RLIMIT_FSIZE`` to this many bytes.
    #:
    #: Only the sandlock backend consumes it (a per-exec ceiling inside the
    #: instance's own), and only ever as a *tightening*: the caller passes how
    #: much of a budget is left, and a write past it fails with ``EFBIG``
    #: instead of being noticed afterwards. ``None`` keeps the instance
    #: ceiling -- which is what every non-sandlock backend and every caller
    #: without a fresh measurement does.
    max_file_size: int | None = None


class RunningProcess:
    """A live process with streamed output and control methods."""

    # M4 D5 / FUP #8: whether ``kill(sig)`` can genuinely deliver a pause
    # signal (SIGSTOP/SIGCONT). Local backends send the requested signal, so
    # ProcessManager may fall back to a direct ``kill(SIGSTOP)`` when the
    # process-group signal fails. SandlockRunningProcess overrides this to
    # ``False`` because its ``kill(sig)`` always SIGKILLs the child subtree:
    # the fallback must WARNING and skip that child instead of silently
    # turning a pause into a kill.
    supports_signal_pause: bool = True

    @property
    def pid(self) -> int:
        raise NotImplementedError

    def output(self) -> AsyncIterator[tuple[str, bytes]]:
        """Yield ``("stdout"|"stderr"|"pty", chunk)`` until EOF."""
        raise NotImplementedError

    def send_stdin(self, data: bytes) -> None:
        raise NotImplementedError

    async def feed_stdin(self, data: bytes) -> None:
        """Feed ``data`` to the child's stdin, waiting for room (N28).

        ``send_stdin`` is the interactive path: it must never block the event
        loop, so a backend whose input buffer is full *drops* the bytes (the
        sandlock backend logs the drop). A workspace write cannot drop
        anything, so the writer uses this variant instead: it returns only
        once the backend has accepted the bytes, and raises when the child has
        no stdin at all (a silently-absent stdin would turn a streamed upload
        into an empty file, which is worse than a refusal).

        The base class derives it from ``send_stdin``: a backend that cannot
        apply backpressure (or cannot tell) keeps the old behaviour, and the
        two backends that can -- local and sandlock -- override it.
        """
        self.send_stdin(data)

    def close_stdin(self) -> None:
        raise NotImplementedError

    def resize(self, rows: int, cols: int) -> None:
        raise NotImplementedError

    def kill(self, sig: int) -> None:
        raise NotImplementedError

    async def exit_code(self) -> int:
        """Return the process exit code (negative = killed by signal)."""
        raise NotImplementedError


class Executor:
    """Creates confined processes for a sandbox runtime."""

    async def start(self, config: ExecConfig) -> RunningProcess:
        raise NotImplementedError

    def close(self) -> None:
        """Release executor-held resources (no-op for stateless backends)."""

    @staticmethod
    def resolve_cmd(cmd: list[str]) -> list[str]:
        return cmd


SignalWriter = Callable[[bytes], None]


class FailedRunningProcess(RunningProcess):
    """Synthetic process for a spawn failure (e.g. missing executable).

    Emits ``stderr`` with the failure reason and exits with code 127,
    matching shell semantics, so the SDK still receives a start event
    followed by an end event.
    """

    _counter = 0

    def __init__(self, reason: str) -> None:
        # uint32-safe synthetic pid: never negative, and far above real OS
        # pids so it cannot collide with a live process.
        type(self)._counter += 1
        self._pid = 1_000_000_000 + type(self)._counter
        self._reason = reason
        self._done = False

    @property
    def pid(self) -> int:
        return self._pid

    async def output(self) -> AsyncIterator[tuple[str, bytes]]:
        if not self._done:
            self._done = True
            yield ("stderr", self._reason.encode("utf-8", "replace"))

    def send_stdin(self, data: bytes) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    def resize(self, rows: int, cols: int) -> None:
        pass

    def kill(self, sig: int) -> None:
        pass

    async def exit_code(self) -> int:
        return 127
