"""SandlockExecutor ``update_network`` D4=A semantics (recording fake).

The fork ``SandboxInstance`` is monkeypatched with a recording fake so the
applicability contract -- no-instance updates become the future static
policy, launched allowOut narrowings call ``instance.update_network(ips)``
with staleness logging, and everything inexpressible raises
``NetworkUpdateConflictError`` without calling the instance -- is
unit-testable on macOS / CI.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import envd_service.executors.sandlock as sl
from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor
from gateway_common.network import NetworkUpdateConflictError


class _RecordingInstance:
    """Fake fork instance: records ``update_network`` ip-set invocations."""

    def __init__(self, policy, name=None):
        self.policy = policy
        self.name = name or "sbx_rec"
        self.update_calls: list[list[str]] = []
        self.stale_child_ids: list[int] = []
        self.closed = False

    def update_network(self, ips):
        self.update_calls.append(list(ips))
        return list(self.stale_child_ids)

    def close(self):
        self.closed = True


class _FakePolicy:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _EOFStream:
    def read(self, _n: int) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def closed(self) -> bool:
        return False


class _FakeExecProcess:
    def __init__(self, pid: int = 4242, child_id: int = 7) -> None:
        self.pid = pid
        self.child_id = child_id
        self.stdin = _EOFStream()
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()
        self.pty = _EOFStream()

    def wait(self, timeout=None):  # noqa: ANN001
        return SimpleNamespace(exit_code=0, success=True)


class _ExecRecordingInstance(_RecordingInstance):
    def __init__(self, policy, name=None):
        super().__init__(policy, name=name)
        self.exec_calls: list[dict] = []

    def exec(self, cmd, stdio=..., **kwargs):  # noqa: ANN001
        self.exec_calls.append({"cmd": cmd, "stdio": stdio, "kwargs": kwargs})
        return _FakeExecProcess()


class _FakeStdio:
    PIPED = object()
    PTY = object()


def _executor(
    monkeypatch,
    *,
    network=None,
    allow_internet_access=False,
    sandbox_id="sbx_net",
) -> SandlockExecutor:
    monkeypatch.setattr(sl, "sandlock", object())
    monkeypatch.setattr(sl, "SandlockSandbox", _FakePolicy)
    monkeypatch.setattr(sl, "SandboxInstance", _RecordingInstance)
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
        allow_internet_access=allow_internet_access,
        enable_network=True,
        network=network,
        sandbox_id=sandbox_id,
    )


def _exec_ready_executor(
    monkeypatch,
    *,
    network=None,
    allow_internet_access=True,
    sandbox_id="sbx_net",
) -> SandlockExecutor:
    """Executor patched for the full start() path with a recording instance."""
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
        allow_internet_access=allow_internet_access,
        enable_network=True,
        network=network,
        sandbox_id=sandbox_id,
    )


def test_prelaunch_update_replaces_future_static_policy(monkeypatch) -> None:
    """No instance yet: every normalized update is applicable and simply
    becomes the static policy the future instance is built with."""
    ex = _executor(
        monkeypatch,
        network={"allowOut": ["8.8.8.8"]},
        allow_internet_access=True,
    )
    assert ex.instance_handle is None

    ex.update_network({"denyOut": ["10.0.0.0/8"], "allowInternetAccess": True})

    assert ex.instance_handle is None
    assert ex._network == {"denyOut": ["10.0.0.0/8"], "allowInternetAccess": True}
    assert ex._allow_internet_access is True


async def test_start_registers_child_id_pid_cmd_before_returning(
    monkeypatch,
) -> None:
    """``start()`` registers the fork child (id -> pid + resolved argv) so a
    later network update can log which running children keep their policy."""
    ex = _exec_ready_executor(monkeypatch)
    inst = ex._ensure_instance()
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "echo hi"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    running = await ex.start(cfg)
    try:
        assert ex.instance_handle is inst
        assert ex._child_registry[7] == (4242, ["/bin/sh", "-c", "echo hi"])
        assert running.pid == 4242
    finally:
        ex.close()


async def test_launched_allowout_subset_calls_instance_and_logs_staleness(
    monkeypatch, caplog
) -> None:
    """A launched allowOut subset narrowing binds the proposed IP set and logs
    each stale fork child plus a count summary at INFO."""
    ex = _exec_ready_executor(
        monkeypatch,
        network={"allowOut": ["8.8.8.8", "1.1.1.1"]},
        allow_internet_access=True,
    )
    inst = ex._ensure_instance()
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "echo hi"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    await ex.start(cfg)
    inst.stale_child_ids = [7]
    try:
        with caplog.at_level(
            logging.INFO, logger="envd_service.executors.sandlock"
        ):
            ex.update_network({"allowOut": ["8.8.8.8"]})

        assert inst.update_calls == [["8.8.8.8"]]
        assert ex._network == {"allowOut": ["8.8.8.8"]}
        messages = [r.getMessage() for r in caplog.records]
        assert (
            "sandbox_id=sbx_net instance_name=sbx_net stale_child_id=7 "
            "pid=4242 cmd=/bin/sh -c echo hi"
        ) in messages
        assert (
            "sandbox_id=sbx_net instance_name=sbx_net "
            "network_update stale_child_count=1"
        ) in messages
    finally:
        ex.close()


async def test_narrowing_to_empty_deny_all_for_new_execs(monkeypatch) -> None:
    """allowOut -> [] is an expressible narrowing that binds an empty ip set
    (deny-all for new execs); the executor records the applied state."""
    ex = _exec_ready_executor(
        monkeypatch,
        network={"allowOut": ["8.8.8.8"]},
        allow_internet_access=False,
    )
    inst = ex._ensure_instance()
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "true"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    await ex.start(cfg)
    try:
        ex.update_network({"allowOut": []})
        assert inst.update_calls == [[]]
        assert ex._network == {"allowOut": []}

        # Re-widening toward the launch ceiling is a 409 (D4=A contract:
        # after [] is applied, allowOut ["8.8.8.8"] is not expressible).
        with pytest.raises(NetworkUpdateConflictError):
            ex.update_network({"allowOut": ["8.8.8.8"]})
        assert inst.update_calls == [[]]
        assert ex._network == {"allowOut": []}
    finally:
        ex.close()


def test_equal_update_is_a_noop_without_instance_call(monkeypatch) -> None:
    """A full-state no-op on a launched instance stays 204 and never calls the
    instance (equal is expressible; domains/CIDRs are irrelevant when nothing
    changes)."""
    static = {"allowOut": ["8.8.8.8", "example.com"]}
    ex = _executor(monkeypatch, network=static, allow_internet_access=True)
    inst = ex._ensure_instance()
    try:
        ex.update_network(dict(static))
        assert inst.update_calls == []
        assert ex._network == static
    finally:
        ex.close()


@pytest.mark.parametrize(
    ("static", "static_allow_internet", "proposed", "expected_message"),
    [
        (
            {"allowOut": ["8.8.8.8"]},
            True,
            {"denyOut": ["10.0.0.0/8"]},
            "network egress model cannot change on a launched sandbox "
            "(allowOut -> denyOut)",
        ),
        (
            {"allowOut": ["8.8.8.8"]},
            True,
            {"allowOut": ["8.8.8.8", "1.1.1.1"]},
            "allowOut can only be narrowed on a launched sandbox",
        ),
        (
            {"allowOut": ["8.8.8.8", "example.com"]},
            True,
            {"allowOut": ["8.8.8.8"]},
            "allowOut entry 'example.com' cannot be expressed on a launched "
            "sandbox (ip literals only)",
        ),
        (
            {"allowOut": ["8.8.8.8", "example.com"]},
            True,
            {"allowOut": ["example.com"]},
            "allowOut entry 'example.com' cannot be expressed on a launched "
            "sandbox (ip literals only)",
        ),
        (
            {"allowOut": ["8.8.8.8"], "rules": {"api.example.com": []}},
            True,
            {"allowOut": ["8.8.8.8"]},
            "network field rules cannot change on a launched sandbox",
        ),
        (
            {"allowOut": ["8.8.8.8"]},
            False,
            {"allowOut": ["8.8.8.8"], "allowInternetAccess": True},
            "allowInternetAccess cannot change on a launched sandbox",
        ),
        (
            None,
            False,
            {"allowOut": ["8.8.8.8"]},
            "network egress model cannot change on a launched sandbox "
            "(deny-all implicit -> allowOut)",
        ),
        (
            {"denyOut": ["10.0.0.0/8"]},
            True,
            {"denyOut": ["10.0.0.0/8", "169.254.169.254"]},
            "adding denyOut entries on a launched sandbox is not expressible "
            "(the fork binds ip allowlists only)",
        ),
        (
            {"denyOut": ["10.0.0.0/8", "169.254.169.254"]},
            True,
            {"denyOut": ["10.0.0.0/8"]},
            "denyOut can only grow (deny more) on a launched sandbox",
        ),
    ],
)
def test_live_inexpressible_updates_raise_without_calling_instance(
    monkeypatch,
    static,
    static_allow_internet,
    proposed,
    expected_message,
) -> None:
    """Model flips, widenings, domain removal/keeping, static-field changes
    and deny-list edits raise the conflict exception without touching the
    instance or the executor's applied state."""
    ex = _executor(
        monkeypatch,
        network=static,
        allow_internet_access=static_allow_internet,
    )
    inst = ex._ensure_instance()
    try:
        with pytest.raises(NetworkUpdateConflictError) as excinfo:
            ex.update_network(proposed)
        assert str(excinfo.value) == expected_message
        assert inst.update_calls == []
        assert ex._network == (dict(static) if static else None)
    finally:
        ex.close()


def test_fork_permission_error_maps_to_conflict_without_persisting(
    monkeypatch,
) -> None:
    """Defense in depth: a fork PermissionError (EPERM) on an accepted update
    maps to the same 409 and leaves the executor policy untouched."""

    class _RefusingInstance(_RecordingInstance):
        def update_network(self, ips):
            raise PermissionError("update_network exceeds the ceiling (EPERM)")

    monkeypatch.setattr(sl, "sandlock", object())
    monkeypatch.setattr(sl, "SandlockSandbox", _FakePolicy)
    monkeypatch.setattr(sl, "SandboxInstance", _RefusingInstance)
    monkeypatch.setattr(sl, "ExecStdio", _FakeStdio)
    ex = SandlockExecutor(
        workspace_dir="/tmp/ws",
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=256,
        max_open_files=4096,
        allow_internet_access=True,
        enable_network=True,
        network={"allowOut": ["8.8.8.8"]},
        sandbox_id="sbx_net",
    )
    inst = ex._ensure_instance()
    try:
        with pytest.raises(NetworkUpdateConflictError) as excinfo:
            ex.update_network({"allowOut": []})
        assert str(excinfo.value) == "update_network exceeds the ceiling (EPERM)"
        assert ex._network == {"allowOut": ["8.8.8.8"]}
    finally:
        ex.close()


def test_close_clears_stale_child_registry_and_snapshot(monkeypatch) -> None:
    """Closing the instance releases the child mapping and the launch
    snapshot; a subsequent update is pre-launch applicable again."""
    ex = _executor(monkeypatch, network={"allowOut": ["8.8.8.8"]})
    inst = ex._ensure_instance()
    assert ex._instance_network_snapshot is not None
    ex._child_registry[7] = (4242, ["/bin/sh"])
    ex.close()
    assert inst.closed is True
    assert ex._child_registry == {}
    assert ex._instance_network_snapshot is None

    ex.update_network({"denyOut": ["10.0.0.0/8"]})
    assert ex.instance_handle is None
    assert ex._network == {"denyOut": ["10.0.0.0/8"]}
