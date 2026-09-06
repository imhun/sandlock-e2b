"""ProcessManager pause/resume fallback semantics (FUP #8).

``pause_all``/``resume_all`` fall back to a direct per-process signal only
when the process-group signal fails. The sandlock backend's ``kill(sig)``
ignores the requested signal and always SIGKILLs, so routing a SIGSTOP or
SIGCONT through it would silently kill the child. Backends advertise the
capability through ``supports_signal_pause``; the manager WARNINGs and skips
children whose backend cannot signal-pause instead of killing them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import pytest

from envd_service.executors.base import RunningProcess
from envd_service.executors.local import LocalRunningProcess
from envd_service.executors.sandlock import SandlockRunningProcess
from envd_service.process.manager import ExecConfig, ManagedProcess, ProcessManager


class _FakeExecProcess:
    """Minimal fork ``ExecProcess`` recording kill calls."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.child_id = 7
        self.kill_calls = 0

    def kill(self) -> None:
        self.kill_calls += 1


class _FakeRunning(RunningProcess):
    """Running-process stand-in recording every ``kill(sig)`` call."""

    supports_signal_pause = True

    def __init__(self, pid: int = 4242) -> None:
        self._pid = pid
        self.kill_calls: list[int] = []

    @property
    def pid(self) -> int:
        return self._pid

    def kill(self, sig: int) -> None:
        self.kill_calls.append(sig)


class _NoSignalPauseRunning(_FakeRunning):
    """Sandlock-shaped backend: kill(sig) is destructive, not signal-aware."""

    supports_signal_pause = False

    def kill(self, sig: int) -> None:
        # Mirrors SandlockRunningProcess: always SIGKILL regardless of sig.
        self.kill_calls.append(signal.SIGKILL)


def _manager_with(running: RunningProcess) -> ProcessManager:
    manager = ProcessManager(executor=object())  # type: ignore[arg-type]
    proc = ManagedProcess(
        pid=running.pid,
        config=ExecConfig(
            cmd=["sleep", "60"],
            env={},
            cwd="/workspace",
            stdin_enabled=False,
        ),
        _running=running,
    )
    manager._processes[proc.pid] = proc
    return manager


def _force_group_signal_failure(monkeypatch) -> None:
    """Make the group-first killpg path fail so the fallback runs."""

    def _raise_process_lookup(*_args) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(os, "killpg", _raise_process_lookup)


def test_pause_fallback_warns_and_skips_kill_when_backend_cannot_signal_pause(
    monkeypatch, caplog
) -> None:
    running = _NoSignalPauseRunning()
    manager = _manager_with(running)
    _force_group_signal_failure(monkeypatch)
    caplog.set_level(logging.WARNING, logger="envd_service.process.manager")

    manager.pause_all()

    assert running.kill_calls == []
    assert [r.message for r in caplog.records] == [
        "pause fallback skipped pid=4242 cmd=sleep 60: running backend "
        "cannot signal-pause (kill(sig) SIGKILLs); child stays running",
    ]


def test_resume_fallback_warns_and_skips_kill_when_backend_cannot_signal_pause(
    monkeypatch, caplog
) -> None:
    running = _NoSignalPauseRunning()
    manager = _manager_with(running)
    _force_group_signal_failure(monkeypatch)
    caplog.set_level(logging.WARNING, logger="envd_service.process.manager")

    manager.resume_all()

    assert running.kill_calls == []
    assert [r.message for r in caplog.records] == [
        "resume fallback skipped pid=4242 cmd=sleep 60: running backend "
        "cannot signal-pause (kill(sig) SIGKILLs); child stays paused",
    ]


def test_pause_resume_fallback_sends_signal_when_backend_supports_signal_pause(
    monkeypatch,
) -> None:
    """A local-shaped backend (marker True) still gets the direct SIGSTOP /
    SIGCONT fallback when the group signal fails."""
    running = _FakeRunning()
    manager = _manager_with(running)
    _force_group_signal_failure(monkeypatch)

    manager.pause_all()
    manager.resume_all()

    assert running.kill_calls == [signal.SIGSTOP, signal.SIGCONT]


def test_running_process_backends_expose_capability_markers() -> None:
    assert RunningProcess.supports_signal_pause is True
    assert LocalRunningProcess.supports_signal_pause is True
    assert SandlockRunningProcess.supports_signal_pause is False


async def test_sandlock_kill_ignores_signum_and_always_sigkills() -> None:
    """SandlockRunningProcess.kill routes SIGSTOP to the fork's SIGKILL-only
    ExecProcess.kill -- which is why the pause fallback must skip it."""
    loop = asyncio.get_running_loop()
    fork_proc = _FakeExecProcess()
    running = SandlockRunningProcess(
        proc=fork_proc,
        queue=asyncio.Queue(),
        loop=loop,
        stdin_queue=asyncio.Queue(),
    )

    running.kill(signal.SIGSTOP)

    assert fork_proc.kill_calls == 1
