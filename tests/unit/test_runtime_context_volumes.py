"""SandboxRuntimeContext chroot volume bind materialization (M4 Task 11)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import envd_service.runtime.context as context_mod
from envd_service.config import Settings
from envd_service.runtime.context import SandboxRuntimeContext
from envd_service.runtime.registry import RuntimeSandbox


def _record(tmp_path: Path, *, with_volume: bool) -> RuntimeSandbox:
    workspace = tmp_path / "sbx"
    workspace.mkdir(parents=True)
    volume = tmp_path / "vol"
    volume.mkdir(parents=True)
    volume_mounts = (
        [{"path": "mnt/data", "hostPath": str(volume)}] if with_volume else []
    )
    if with_volume:
        (workspace / "mnt").mkdir(parents=True)
        (workspace / "mnt" / "data").symlink_to(volume, target_is_directory=True)
    return RuntimeSandbox(
        sandbox_id="sbx_vol",
        access_token="at",
        workspace_dir=str(workspace),
        base_image="python-mcp:3.14",
        volume_mounts=volume_mounts,
    )


def _make_ctx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_volume: bool = True,
) -> SandboxRuntimeContext:
    monkeypatch.setattr(context_mod.sys, "platform", "linux")
    monkeypatch.setattr(context_mod.os, "geteuid", lambda: 0)
    record = _record(tmp_path, with_volume=with_volume)
    ctx = SandboxRuntimeContext(record, Settings(executor="local"))
    # The local executor has no image rootfs; fake the chroot shape after the
    # context is constructed (the executor attribute is duck-typed).
    ctx.executor = SimpleNamespace(_image_rootfs=Path("/rootfs"))
    return ctx


def test_chroot_volume_symlink_is_replaced_by_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 Task 11: chroot workers bind the real volume into the workspace so
    relative ``mnt/data`` paths resolve; shutdown unmounts the view."""
    mounts: list[tuple[str, str]] = []
    unmounted: list[str] = []
    monkeypatch.setattr(
        context_mod,
        "_bind_mount",
        lambda source, target: mounts.append((str(source), str(target))),
    )
    monkeypatch.setattr(
        context_mod, "_unmount", lambda target: unmounted.append(str(target))
    )
    ctx = _make_ctx(tmp_path, monkeypatch)
    ctx._materialize_chroot_volume_mounts()
    record = ctx.record
    target = Path(record.workspace_dir) / "mnt" / "data"
    assert not target.is_symlink()
    assert mounts == [
        (
            str(record.volume_mounts[0]["hostPath"]),
            str(target),
        )
    ]
    assert ctx._volume_bind_mounts == [(target, str(record.volume_mounts[0]["hostPath"]))]

    ctx.shutdown()
    assert unmounted == [str(target)]
    assert ctx._volume_bind_mounts == []
    # The provisioned symlink is restored so exports/migrations keep the
    # volume layout the provisioning step expects.
    assert target.is_symlink()
    assert target.readlink() == Path(record.volume_mounts[0]["hostPath"])


def test_chroot_volume_bind_failure_keeps_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that cannot mount keeps the provisioned symlink instead of
    breaking sandbox creation (non-root / restricted deployments)."""

    def _deny(*_args):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(context_mod, "_bind_mount", _deny)
    ctx = _make_ctx(tmp_path, monkeypatch)
    ctx._materialize_chroot_volume_mounts()
    target = Path(ctx.record.workspace_dir) / "mnt" / "data"
    assert target.is_symlink()
    assert ctx._volume_bind_mounts == []


def test_no_bind_without_chroot_image_rootfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pure-sandlock / local shapes keep the symlink untouched."""
    ctx = _make_ctx(tmp_path, monkeypatch)
    ctx.executor = SimpleNamespace(_image_rootfs=None)
    ctx._materialize_chroot_volume_mounts()
    target = Path(ctx.record.workspace_dir) / "mnt" / "data"
    assert target.is_symlink()
    assert ctx._volume_bind_mounts == []
