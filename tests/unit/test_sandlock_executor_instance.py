"""SandlockExecutor long-lived exec-instance lifecycle (M4 D1/D2).

The native sandlock library is Linux-only; ``SandboxInstance`` is
monkeypatched with a recording fake so the lifecycle contract -- lazy single
creation, stable identity, idempotent close, and rebuild-once after a
closed/dead launch -- is unit-testable on macOS / CI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from types import SimpleNamespace

import pytest

import envd_service.executors.sandlock as sl
from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import (
    SandlockExecutor,
    SandlockRunningProcess,
)
from gateway_common.errors import ConnectError


class _FakeInstance:
    def __init__(self, policy, name=None):
        self.policy = policy
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


class _FakeClosedError(RuntimeError):
    """Stand-in for ``sandlock.InstanceClosedError``.

    The real classes ship with the Linux wheel; the rebuild-once decision is a
    *type* check against the map the executor builds from them (B1 review,
    minor-3), so the suite substitutes its own types instead of matching text.
    """


class _FakeDeadError(RuntimeError):
    """Stand-in for ``sandlock.InstanceDeadError``."""


def _no_root_note(sandbox_id: str) -> str:
    """The N14 S5 shape note a rootless pure executor logs before anything else.

    These lifecycles build the pure shape with neither an image rootfs nor a
    synthesized one, so the executor says once per call that the real root has
    nothing to pivot into for it (`SandlockExecutor._policy_ceiling`). It is a
    WARNING on the same logger, so an exact-records assertion has to carry it.
    """
    return (
        f"sandbox {sandbox_id} has no image rootfs and no synthesized root "
        "(pure shape): the real root has nothing to pivot into for it"
    )


@pytest.fixture
def typed_instance_gone(monkeypatch):
    """Classify the stand-in types as session-gone for this test."""
    monkeypatch.setattr(
        sl,
        "_INSTANCE_GONE_REASONS",
        {_FakeClosedError: "closed", _FakeDeadError: "dead"},
    )
    return _FakeClosedError, _FakeDeadError


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


def test_instance_created_logs_ceiling_summary(monkeypatch, caplog) -> None:
    """D10: instance creation logs sandbox_id/instance_name plus a short
    ceiling summary (memory, process budget, chroot yes/no)."""
    ex = _executor(monkeypatch, "sbx_log")
    with caplog.at_level(
        logging.INFO, logger="envd_service.executors.sandlock"
    ):
        ex._ensure_instance()
    assert [r.message for r in caplog.records] == [
        _no_root_note("sbx_log"),
        "sandlock instance created sandbox_id=sbx_log instance_name=sbx_log "
        "max_memory=512M max_processes=256 chroot=no"
    ]


def test_close_is_idempotent_and_releases_handle(monkeypatch) -> None:
    ex = _executor(monkeypatch)
    inst = ex._ensure_instance()
    ex.close()
    ex.close()
    assert inst.closed is True
    assert ex.instance_handle is None


def test_close_logs_once_and_second_close_is_silent(monkeypatch, caplog) -> None:
    """D10: explicit close logs one INFO line; an idempotent second close
    (no live instance) adds nothing."""
    ex = _executor(monkeypatch)
    ex._ensure_instance()
    with caplog.at_level(
        logging.INFO, logger="envd_service.executors.sandlock"
    ):
        ex.close()
        ex.close()
    assert [r.message for r in caplog.records] == [
        _no_root_note("sbx_abc"),
        "sandlock instance closed sandbox_id=sbx_abc instance_name=sbx_abc"
    ]


def test_close_then_ensure_creates_new_instance_with_same_name(monkeypatch) -> None:
    """Closing releases the handle; the next ``_ensure_instance`` (e.g. after
    a delete/evict round-trip on a reused executor) launches a fresh instance
    carrying the same stable sandbox-id name (M4 D1)."""
    ex = _executor(monkeypatch, "sbx_recreate")
    first = ex._ensure_instance()
    assert first.name == "sbx_recreate"

    ex.close()
    assert ex.instance_handle is None

    second = ex._ensure_instance()
    assert second is not first
    assert second.name == "sbx_recreate"
    assert first.closed is True
    assert second.closed is False


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


@pytest.mark.parametrize("kind", ["closed", "dead"])
def test_ensure_instance_rebuilds_once_after_closed_or_dead_launch(
    monkeypatch, kind, caplog, typed_instance_gone
) -> None:
    closed_error, dead_error = typed_instance_gone
    error_type = closed_error if kind == "closed" else dead_error
    attempts = [0]

    class _ClosedOnce(_FakeInstance):
        def __init__(self, policy, name=None):
            attempts[0] += 1
            if attempts[0] == 1:
                raise error_type(f"sandlock instance is {kind}")
            super().__init__(policy, name=name)

    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _ClosedOnce)
    with caplog.at_level(
        logging.INFO, logger="envd_service.executors.sandlock"
    ):
        inst = ex._ensure_instance()
    assert attempts[0] == 2
    assert inst.name == "sbx_abc"
    assert [r.message for r in caplog.records] == [
        _no_root_note("sbx_abc"),
        "sandlock instance relaunching after "
        f"{kind} sandbox_id=sbx_abc instance_name=sbx_abc",
        "sandlock instance created sandbox_id=sbx_abc instance_name=sbx_abc "
        "max_memory=512M max_processes=256 chroot=no",
    ]


def test_second_closed_launch_failure_bubbles(monkeypatch, typed_instance_gone) -> None:
    closed_error, _ = typed_instance_gone
    attempts = [0]

    class _AlwaysClosed(_FakeInstance):
        def __init__(self, policy, name=None):
            attempts[0] += 1
            raise closed_error("sandlock instance is closed")

    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _AlwaysClosed)
    # The typed error survives the retry: a host still sees *why* the second
    # attempt failed (nothing is re-wrapped into a bare RuntimeError).
    with pytest.raises(closed_error, match=r"^sandlock instance is closed$"):
        ex._ensure_instance()
    assert attempts[0] == 2
    assert ex.instance_handle is None


def test_message_text_never_decides_the_rebuild(monkeypatch, caplog) -> None:
    """B1 minor-3 regression pin.

    Since fork SL-12 a launch failure carries the core's own prose; a message
    that merely *mentions* closed/dead (a refusal naming the mediation shape,
    a confinement error quoting a closed fd) must not be mistaken for a
    session-gone failure: no rebuild, and the error propagates unchanged.
    """
    attempts = [0]
    prose = (
        "sandlock_instance_launch failed: process error: child process error: "
        "the init channel closed after the main-exit container end / dead "
        "listener: see route B for the remedy"
    )

    class _ProseLaunchFailure(_FakeInstance):
        def __init__(self, policy, name=None):
            attempts[0] += 1
            raise RuntimeError(prose)

    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _ProseLaunchFailure)
    with caplog.at_level(
        logging.WARNING, logger="envd_service.executors.sandlock"
    ):
        with pytest.raises(RuntimeError, match="route B for the remedy") as info:
            ex._ensure_instance()
    assert not isinstance(info.value, (_FakeClosedError, _FakeDeadError))
    assert attempts[0] == 1, "a text match must not trigger a rebuild"
    assert [r.message for r in caplog.records] == [
        _no_root_note("sbx_abc"),
        "sandlock instance launch failed sandbox_id=sbx_abc "
        f"instance_name=sbx_abc error={prose}"
    ]


def test_unrelated_runtime_error_does_not_retry(monkeypatch, caplog) -> None:
    attempts = [0]

    class _LaunchFailed(_FakeInstance):
        def __init__(self, policy, name=None):
            attempts[0] += 1
            raise RuntimeError("sandlock_instance_launch failed")

    ex = _executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _LaunchFailed)
    with caplog.at_level(
        logging.WARNING, logger="envd_service.executors.sandlock"
    ):
        with pytest.raises(RuntimeError, match=r"^sandlock_instance_launch failed$"):
            ex._ensure_instance()
    assert attempts[0] == 1
    assert [r.message for r in caplog.records] == [
        _no_root_note("sbx_abc"),
        "sandlock instance launch failed sandbox_id=sbx_abc "
        "instance_name=sbx_abc error=sandlock_instance_launch failed"
    ]


# --- Task 2 (M4 D3): per-exec start path on the held instance ---------------


def test_set_mcp_bind_port_lands_on_ensure_instance_policy(monkeypatch) -> None:
    """The pre-allocated MCP port gates the instance bind ceiling
    (``net_allow_bind``), and per-command fields stay off the policy."""
    ex = _executor(monkeypatch)
    ex.set_mcp_bind_port(51234)
    inst = ex._ensure_instance()
    assert inst.policy.net_allow_bind == [51234]
    assert getattr(inst.policy, "cwd", None) is None
    assert getattr(inst.policy, "env", None) in (None, {})
    assert getattr(inst.policy, "clean_env", None) in (None, False)


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
    # N15 keeps the *host* workspace path here on purpose: the fork chdir's to
    # `chroot_root.join(cwd)` for real, and the pure shape's root is "/". The
    # mediator maps the host path back to /home/user through the mount table.
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
    monkeypatch.setattr(sl, "sandlock", None)
    with pytest.raises(ConnectError, match="not available on this platform"):
        await ex.start(
            ExecConfig(
                cmd=["/bin/echo"],
                env={},
                cwd="/tmp/ws",
                stdin_enabled=False,
            )
        )


async def test_start_rebuilds_once_after_closed_exec_and_retries(
    monkeypatch, typed_instance_gone
) -> None:
    """I1: idle/24h expiry surfaces as a closed RuntimeError from
    ``inst.exec``; start() rebuilds the instance exactly once and retries the
    exec. The reaped child is also dropped from the staleness registry."""
    closed_error, _ = typed_instance_gone
    attempts = [0]
    instances: list = []

    class _ClosedOnceExec(_ExecRecordingInstance):
        def __init__(self, policy, name=None):
            super().__init__(policy, name=name)
            instances.append(self)

        def exec(self, cmd, stdio=..., **kwargs):  # noqa: ANN001
            attempts[0] += 1
            if attempts[0] == 1:
                raise closed_error("sandlock instance is closed")
            return super().exec(cmd, stdio=stdio, **kwargs)

    ex = _exec_ready_executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _ClosedOnceExec)
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "echo retried"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    running = await ex.start(cfg)

    assert attempts[0] == 2
    assert len(instances) == 2
    assert ex.instance_handle is instances[1]
    assert len(instances[1].exec_calls) == 1
    # The retried child was registered for staleness reporting...
    assert ex._child_registry == {7: (4242, ["/bin/sh", "-c", "echo retried"])}
    # ...and dropped again once the process is reaped.
    assert await running.exit_code() == 0
    assert ex._child_registry == {}


async def test_start_second_closed_exec_failure_propagates(
    monkeypatch, typed_instance_gone
) -> None:
    """I1: if the rebuilt instance's exec also raises closed/dead, the
    second failure propagates unchanged (no endless rebuild loop)."""
    closed_error, _ = typed_instance_gone
    created = [0]

    class _AlwaysClosedExec(_ExecRecordingInstance):
        def __init__(self, policy, name=None):
            super().__init__(policy, name=name)
            created[0] += 1

        def exec(self, cmd, stdio=..., **kwargs):  # noqa: ANN001
            raise closed_error("sandlock instance is closed")

    ex = _exec_ready_executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _AlwaysClosedExec)
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "true"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    with pytest.raises(closed_error, match=r"^sandlock instance is closed$"):
        await ex.start(cfg)
    assert created[0] == 2


async def test_start_after_close_fails_without_rebuilding(monkeypatch) -> None:
    """I1: an explicit close()/shutdown must not leak a fresh instance -- a
    later start() fails loudly instead of rebuilding."""
    created = [0]

    class _CountingInstance(_ExecRecordingInstance):
        def __init__(self, policy, name=None):
            super().__init__(policy, name=name)
            created[0] += 1

    ex = _exec_ready_executor(monkeypatch)
    monkeypatch.setattr(sl, "SandboxInstance", _CountingInstance)
    ex._ensure_instance()
    ex.close()
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "true"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    with pytest.raises(RuntimeError, match="shut down"):
        await ex.start(cfg)
    assert created[0] == 1
    assert ex.instance_handle is None


def test_child_registry_exit_removal_is_pid_guarded(monkeypatch) -> None:
    """I1/cheap-win: a reaped child leaves the staleness registry, but an
    older exit must not remove a newer entry if the fork recycled the child
    id after an instance rebuild."""
    ex = _exec_ready_executor(monkeypatch)
    ex._child_registry[7] = (4242, ["/bin/old"])
    ex._child_exited(7, 4242)
    assert ex._child_registry == {}

    ex._child_registry[7] = (9999, ["/bin/new"])
    ex._child_exited(7, 4242)
    assert ex._child_registry == {7: (9999, ["/bin/new"])}
    ex._child_exited(7, 9999)
    assert ex._child_registry == {}


async def test_consume_filters_internal_eof_markers() -> None:
    """Cheap-win: ``("__eof__", kind)`` stream markers are internal and never
    surface to consumers (parity with the local executor)."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    running = SandlockRunningProcess(
        proc=_FakeExecProcess(),
        queue=queue,
        loop=loop,
        stdin_queue=asyncio.Queue(maxsize=1),
    )
    for item in (
        ("stderr", b"x"),
        ("__eof__", "stderr"),
        ("stdout", b"y"),
        ("__eof__", "stdout"),
        None,
    ):
        queue.put_nowait(item)
    events = [item async for item in running.output()]
    assert events == [("stderr", b"x"), ("stdout", b"y")]


# --------------------------------------------------------------------------
# Route-B refusals: classified by the fork's stable refusal *code*
# --------------------------------------------------------------------------
#
# A ``sandlock-supervise`` slot is a separate process, so its refusal cannot
# be a typed native error: the frame carries the prose plus a stable code,
# and the wheel surfaces the pair as ``sandlock.exceptions.SlotRefusal``
# (``third_party/sandlock/crates/sandlock-core/src/error.rs``,
# ``RefusalCode``). These cases pin the three-way decision the executor makes
# on that code alone -- and that it never reads the prose.


#: The fork's unified closed-instance prose, verbatim (``error.rs``). Used
#: *against* the code below on purpose: a refusal whose sentence says "closed"
#: but whose code does not must not be rebuilt (and vice versa), which is what
#: "the text never decides" means.
CLOSED_REFUSAL_TEXT = (
    "instance exec failed: process error: instance is closed (shut down, or "
    "the init channel closed after the main-exit container end); no new work "
    "is accepted"
)


class _CodedRefusal(Exception):
    """Stand-in for ``sandlock.exceptions.SlotRefusal`` (a ``SandboxError``).

    Only ``.code`` is read by the executor; the class deliberately does *not*
    subclass the real ``SandboxError`` so this suite keeps running on macOS
    (where the wheel is absent), exactly like the ``typed_instance_gone``
    stand-ins above.
    """

    def __init__(self, message: str, code: str | None) -> None:
        super().__init__(message)
        self.code = code


def _refusing_executor(monkeypatch, refusal, times: int, sandbox_id="sbx_coded"):
    """An exec-ready executor whose first ``times`` execs raise ``refusal``."""
    attempts = [0]
    instances: list = []
    error = refusal

    class _RefusingExec(_ExecRecordingInstance):
        def __init__(self, policy, name=None):
            super().__init__(policy, name=name)
            instances.append(self)

        def exec(self, cmd, stdio=..., **kwargs):  # noqa: ANN001
            attempts[0] += 1
            if attempts[0] <= times:
                raise error
            return super().exec(cmd, stdio=stdio, **kwargs)

    ex = _exec_ready_executor(monkeypatch, sandbox_id)
    monkeypatch.setattr(sl, "SandboxInstance", _RefusingExec)
    return ex, attempts, instances


@pytest.mark.parametrize("code", ["generation_closed", "generation_dead"])
async def test_coded_session_gone_refusal_rebuilds_once_and_succeeds(
    monkeypatch, code, caplog
) -> None:
    """``generation_closed`` / ``generation_dead`` ⇒ rebuild once, then run.

    These are the two codes that mean "the generation can no longer take
    work" (``_REFUSAL_GONE_REASONS``); both go through the *existing*
    rebuild-once path shared with the typed in-process errors -- the fork's
    refusal code replaced the ``stats``/``InstancePhase`` reverse-inference,
    it did not change what the executor does with it.
    """
    refusal = _CodedRefusal(f"instance exec failed: refused ({code})", code)
    ex, attempts, instances = _refusing_executor(monkeypatch, refusal, times=1)
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "echo retried"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    with caplog.at_level("INFO", logger="envd_service.executors.sandlock"):
        running = await ex.start(cfg)

    assert attempts[0] == 2, "exactly one rebuild-and-retry"
    assert len(instances) == 2, "the retry runs on a freshly built instance"
    assert ex.instance_handle is instances[1]
    expected = code.removeprefix("generation_")
    assert [
        record.message
        for record in caplog.records
        if record.message.startswith("sandlock instance ")
        and "rebuilding once" in record.message
    ] == [
        f"sandlock instance {expected} during exec; rebuilding once "
        f"sandbox_id=sbx_coded instance_name=sbx_coded argv=['/bin/sh', '-c', "
        "'echo retried']"
    ]
    assert ex._child_registry == {7: (4242, ["/bin/sh", "-c", "echo retried"])}
    assert await running.exit_code() == 0


async def test_coded_live_refusal_propagates_without_rebuilding(
    monkeypatch, caplog
) -> None:
    """``policy_denied`` ⇒ the refusal reaches the caller unchanged.

    A Live session refusing a wider-than-ceiling request is *not* a gone
    session; rebuilding would silently retry a command the deployment
    refused. The same object must surface, and no instance may be created.
    """
    refusal = _CodedRefusal(
        "instance exec failed: process error: exec params exceed the "
        "instance policy ceiling: bind_ports 65000 is outside the allowed "
        "set (EPERM)",
        "policy_denied",
    )
    ex, attempts, instances = _refusing_executor(monkeypatch, refusal, times=99)
    cfg = ExecConfig(
        cmd=["/bin/true"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    with caplog.at_level("INFO", logger="envd_service.executors.sandlock"):
        with pytest.raises(_CodedRefusal) as raised:
            await ex.start(cfg)
    assert raised.value is refusal, "the refusal must surface unchanged"
    assert attempts[0] == 1, "no retry"
    assert len(instances) == 1, "no rebuild"
    assert [
        record.message
        for record in caplog.records
        if "rebuilding once" in record.message
    ] == []


async def test_the_refusal_text_never_decides_the_rebuild(monkeypatch) -> None:
    """The reverse-inference pin, in code: the *code* decides, never prose.

    Two refusals with the fork's verbatim closed-instance sentence: the one
    whose code says ``policy_denied`` must not be rebuilt (a Live refusal),
    and the one whose code says ``generation_closed`` must be -- even though
    their sentences are identical. This is the pin the SL-12 worker could not
    have (it had no code, so it had to ask the slot for ``stats``).
    """
    live = _CodedRefusal(CLOSED_REFUSAL_TEXT, "policy_denied")
    ex, attempts, instances = _refusing_executor(
        monkeypatch, live, times=99, sandbox_id="sbx_text_live"
    )
    cfg = ExecConfig(
        cmd=["/bin/true"], env={}, cwd="/tmp/ws", stdin_enabled=False
    )
    with pytest.raises(_CodedRefusal):
        await ex.start(cfg)
    assert (attempts[0], len(instances)) == (1, 1)

    gone = _CodedRefusal(CLOSED_REFUSAL_TEXT, "generation_closed")
    ex2, attempts2, instances2 = _refusing_executor(
        monkeypatch, gone, times=1, sandbox_id="sbx_text_gone"
    )
    running = await ex2.start(cfg)
    assert (attempts2[0], len(instances2)) == (2, 2)
    assert await running.exit_code() == 0


async def test_an_uncoded_refusal_is_not_guessed_from_its_text(
    monkeypatch, caplog
) -> None:
    """A wheel older than the code field ⇒ no rebuild, failure visible.

    The fork's ``code`` rides ``ControlResponse``; a wheel built before it
    answers with prose alone, which is exactly the shape that used to force
    the ``stats`` reverse-inference. The executor refuses to guess: the
    sentence below is the closed-instance one verbatim, and an uncoded
    refusal carrying it must still propagate (fail visible) rather than
    silently rebuild on a matched substring. envd and ``sandlock-supervise``
    ship in the same wheel, so this shape only appears mid-upgrade -- and its
    symptom is a *visible* failure, never a wrong rebuild.
    """
    uncoded = _CodedRefusal(CLOSED_REFUSAL_TEXT, None)
    ex, attempts, instances = _refusing_executor(monkeypatch, uncoded, times=99)
    cfg = ExecConfig(
        cmd=["/bin/true"], env={}, cwd="/tmp/ws", stdin_enabled=False
    )
    with caplog.at_level("INFO", logger="envd_service.executors.sandlock"):
        with pytest.raises(_CodedRefusal) as raised:
            await ex.start(cfg)
    assert raised.value is uncoded
    assert (attempts[0], len(instances)) == (1, 1)
    assert [
        record.message for record in caplog.records if "rebuilding" in record.message
    ] == []


async def test_a_refusal_is_classified_without_ever_stringifying_it(
    monkeypatch,
) -> None:
    """A refusal whose ``__str__`` explodes must still be classified.

    The strongest form of "the executor never reads the refusal's text": this
    stand-in raises from ``__str__``, so any code path that formats it --
    the old substring match, a "reason = str(exc)" shortcut, an eager log
    line -- turns into a loud failure here instead of a silent misdecision.
    """

    class _PoisonText(_CodedRefusal):
        def __str__(self) -> str:
            raise AssertionError("the refusal's text must never be read")

    refusal = _PoisonText("unused", "generation_closed")
    ex, attempts, instances = _refusing_executor(monkeypatch, refusal, times=1)
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "true"], env={}, cwd="/tmp/ws", stdin_enabled=False
    )
    running = await ex.start(cfg)
    assert attempts[0] == 2
    assert len(instances) == 2
    assert await running.exit_code() == 0
