"""SandlockExecutor long-lived exec-instance lifecycle (M4 D1/D2).

The native sandlock library is Linux-only; ``SandboxInstance`` is
monkeypatched with a recording fake so the lifecycle contract -- lazy single
creation, stable identity, idempotent close, and rebuild-once after a
closed/dead launch -- is unit-testable on macOS / CI.
"""

from __future__ import annotations

import hashlib

import pytest

import envd_service.executors.sandlock as sl
from envd_service.executors.sandlock import SandlockExecutor


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
