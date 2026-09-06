"""SandlockExecutor long-lived exec-instance lifecycle (M4 D1/D2).

The native sandlock library is Linux-only; ``SandboxInstance`` is
monkeypatched with a recording fake so the lifecycle contract -- lazy single
creation, stable identity, idempotent close, and rebuild-once after a
closed/dead launch -- is unit-testable on macOS / CI.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import envd_service.executors.sandlock as sl
from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor
from gateway_common.errors import ConnectError


class _FakeInstance:
    def __init__(self, policy, name=None):
        self.policy = policy
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


def _executor(monkeypatch, sandbox_id="sbx_abc", workspace_dir="/tmp/ws"):
    monkeypatch.setattr(sl, "SandboxInstance", _FakeInstance)
    return SandlockExecutor(
        workspace_dir=workspace_dir,
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=256,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=sandbox_id,
    )


def test_lazy_instance_created_once_with_sandbox_id_name(monkeypatch) -> None:
    ex = _executor(monkeypatch, "sbx_lazy")
    assert ex.instance_handle is None
    assert ex.instance_name == "sbx_lazy"
    inst1 = ex._ensure_instance()
    inst2 = ex._ensure_instance()
    assert inst1 is inst2
    assert inst1.name == "sbx_lazy"


def test_close_is_idempotent_and_releases_handle(monkeypatch) -> None:
    ex = _executor(monkeypatch)
    inst = ex._ensure_instance()
    ex.close()
    ex.close()
    assert inst.closed is True
    assert ex.instance_handle is None


def test_long_sandbox_id_derives_stable_64b_name(monkeypatch) -> None:
    sandbox_id = "z" * 80
    ex = _executor(monkeypatch, sandbox_id)
    inst = ex._ensure_instance()
    assert len(inst.name.encode()) <= 64
    assert inst.name == "sbx_" + hashlib.sha256(sandbox_id.encode()).hexdigest()[:16]


def test_instance_name_falls_back_to_workspace_dir_name(monkeypatch) -> None:
    ex = _executor(monkeypatch, sandbox_id=None, workspace_dir="/tmp/ws")
    inst = ex._ensure_instance()
    assert ex.instance_name == "ws"
    assert inst.name == "ws"


def test_ensure_instance_returns_none_without_sandlock(monkeypatch) -> None:
    """D11: no native library -> lazy ensure is a silent no-op."""
    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", None)
    assert ex._ensure_instance() is None
    assert ex.instance_handle is None


@pytest.mark.parametrize(
    "message", ["sandlock instance is closed", "sandlock instance is dead"]
)
def test_ensure_instance_rebuilds_once_after_closed_or_dead_launch(
    monkeypatch, message
) -> None:
    attempts = [0]

    class _ClosedOnce(_FakeInstance):
        def __init__(self, policy, name=None):
            attempts[0] += 1
            if attempts[0] == 1:
                raise RuntimeError(message)
            super().__init__(policy, name=name)

    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _ClosedOnce)
    inst = ex._ensure_instance()
    assert attempts[0] == 2
    assert inst.name == "sbx_abc"


def test_second_closed_launch_failure_bubbles(monkeypatch) -> None:
    attempts = [0]

    class _AlwaysClosed(_FakeInstance):
        def __init__(self, policy, name=None):
            attempts[0] += 1
            raise RuntimeError("sandlock instance is closed")

    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _AlwaysClosed)
    with pytest.raises(RuntimeError, match=r"^sandlock instance is closed$"):
        ex._ensure_instance()
    assert attempts[0] == 2
    assert ex.instance_handle is None


def test_unrelated_runtime_error_does_not_retry(monkeypatch) -> None:
    attempts = [0]

    class _LaunchFailed(_FakeInstance):
        def __init__(self, policy, name=None):
            attempts[0] += 1
            raise RuntimeError("sandlock_instance_launch failed")

    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _LaunchFailed)
    with pytest.raises(RuntimeError, match=r"^sandlock_instance_launch failed$"):
        ex._ensure_instance()
    assert attempts[0] == 1


# --- Task 2 (M4 D3): per-exec start path on the held instance ---------------


def test_set_mcp_bind_port_lands_on_ensure_instance_policy(monkeypatch) -> None:
    """The pre-allocated MCP port gates the instance bind ceiling
    (``net_allow_bind``), and per-command fields stay off the policy."""
    ex = _executor(monkeypatch)
    ex.set_mcp_bind_port(51234)
    inst = ex._ensure_instance()
    assert inst.policy.net_allow_bind == [51234]
    assert getattr(inst.policy, "cwd", None) is None
    assert getattr(inst.policy, "env", None) is None
    assert getattr(inst.policy, "clean_env", None) is None


class _FakePolicy:
    """Callable stand-in for ``sandlock.Sandbox`` off-Linux."""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _EOFStream:
    """Fake blocking stream: EOF immediately, records writes."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def read(self, _n: int) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def closed(self) -> bool:
        return False


class _FakeExecProcess:
    """Recording fake for the fork ``ExecProcess`` surface Task 2 consumes."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.child_id = 7
        self.stdin = _EOFStream()
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()
        self.pty = _EOFStream()
        self.resize_calls: list[tuple[int, int]] = []
        self.kill_calls = 0
        self.wait_calls = 0

    def resize(self, rows: int, cols: int) -> None:
        self.resize_calls.append((rows, cols))

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout=None):  # noqa: ANN001
        self.wait_calls += 1
        return SimpleNamespace(exit_code=0, success=True)


class _ExecRecordingInstance(_FakeInstance):
    def __init__(self, policy, name=None):
        super().__init__(policy, name=name)
        self.exec_calls: list[dict] = []
        self.processes: list[_FakeExecProcess] = []

    def exec(self, cmd, stdio=..., **kwargs):  # noqa: ANN001
        self.exec_calls.append({"cmd": cmd, "stdio": stdio, "kwargs": kwargs})
        proc = _FakeExecProcess()
        self.processes.append(proc)
        return proc


class _FakeStdio:
    PIPED = object()
    PTY = object()


def _exec_ready_executor(monkeypatch, sandbox_id="sbx_exec"):
    """Executor patched for the full start() path (native library + stdio)."""
    monkeypatch.setattr(sl, "sandlock", object())
    monkeypatch.setattr(sl, "SandlockSandbox", _FakePolicy)
    monkeypatch.setattr(sl, "SandboxInstance", _ExecRecordingInstance)
    monkeypatch.setattr(sl, "ExecStdio", _FakeStdio)
    return SandlockExecutor(
        workspace_dir="/tmp/ws",
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=256,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=sandbox_id,
    )


async def test_start_execs_onto_held_instance_with_per_exec_params(
    monkeypatch,
) -> None:
    """start() routes the command through the held instance's exec() with
    PIPED stdio and per-exec cwd/env/clean_env -- no fresh per-command
    Sandbox, no PTY bridge wrapper."""
    ex = _exec_ready_executor(monkeypatch)
    inst = ex._ensure_instance()
    cfg = ExecConfig(
        cmd=["/bin/bash", "-c", "echo hi"],
        env={"A": "b"},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    running = await ex.start(cfg)

    assert ex.instance_handle is inst
    assert len(inst.exec_calls) == 1
    call = inst.exec_calls[0]
    # resolve_cmd still translates /bin/bash for slim images.
    assert call["cmd"] == ["/bin/sh", "-c", "echo hi"]
    assert call["stdio"] is sl.ExecStdio.PIPED
    assert call["kwargs"]["cwd"] == "/tmp/ws"
    assert call["kwargs"]["env"] == {"A": "b"}
    assert call["kwargs"]["clean_env"] is True
    assert "bind_ports" not in call["kwargs"]
    assert running.pid == 4242
    assert await running.exit_code() == 0
    # The cached pid survives wait() (ExecProcess.pid goes None then).
    assert running.pid == 4242


async def test_start_uses_native_pty_stdio_and_routes_resize_and_kill(
    monkeypatch,
) -> None:
    """A pty command execs with ExecStdio.PTY; resize goes straight to
    ExecProcess.resize and kill to ExecProcess.kill (no bridge frames). The
    create-time rows/cols are applied once at start (the removed bridge used
    to set the initial window size)."""
    ex = _exec_ready_executor(monkeypatch)
    inst = ex._ensure_instance()
    cfg = ExecConfig(
        cmd=["/bin/bash"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=True,
        pty=True,
        rows=24,
        cols=80,
    )
    running = await ex.start(cfg)

    call = inst.exec_calls[0]
    assert call["stdio"] is sl.ExecStdio.PTY
    proc = inst.processes[0]
    assert proc.resize_calls == [(24, 80)]
    running.resize(40, 120)
    assert proc.resize_calls == [(24, 80), (40, 120)]
    running.kill(9)
    assert proc.kill_calls == 1
    assert await running.exit_code() == 0
    assert running.pid == proc.pid


async def test_start_raises_unimplemented_without_native_sandlock(
    monkeypatch,
) -> None:
    """Without the native library start() fails loudly (macOS / D11)."""
    ex = _executor(monkeypatch)
    with pytest.raises(ConnectError, match="not available on this platform"):
        await ex.start(
            ExecConfig(
                cmd=["/bin/echo"],
                env={},
                cwd="/tmp/ws",
                stdin_enabled=False,
            )
        )
