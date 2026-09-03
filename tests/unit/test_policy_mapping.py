"""E2B request to sandlock==0.8.6 API mapping."""

from __future__ import annotations

import pytest

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor


def test_policy_mapping_fields():
    executor = SandlockExecutor(
        workspace_dir="/tmp/ws",
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    sandbox = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/bash", "-c", "x"],
            env={"A": "b"},
            cwd="/tmp/ws",
            stdin_enabled=False,
        )
    )
    assert sandbox.max_memory == "512M"
    assert sandbox.max_cpu == 100
    assert sandbox.max_processes == 64
    assert sandbox.max_open_files == 4096
    assert "/tmp/ws" in sandbox.fs_writable
    # Pure Sandlock (no chroot): fs_readable is an allow-list, so /proc/kcore,
    # /sys and the shared /dev/shm are already unreachable and no denial rules
    # are needed. Issuing them would push writes onto sandlock's on-behalf open
    # path, where files end up owned by the supervisor instead of the sandbox
    # host uid -- voiding in-sandbox chmod and the per-uid isolation of shared
    # volumes. The denials therefore belong to the image-rootfs shape only.
    assert sandbox.fs_denied == []
    assert "/proc/kcore" not in sandbox.fs_readable
    assert "/dev/shm" not in sandbox.fs_readable
    assert sandbox.net_allow == []


def test_image_rootfs_shape_carries_the_shared_path_denials(tmp_path) -> None:
    """With an image rootfs the whole tree is readable, so carve them out."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    executor = SandlockExecutor(
        workspace_dir=str(tmp_path / "ws"),
        base_image="python:3.11-slim",
        image_rootfs=rootfs,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    sandbox = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/sh", "-c", "x"], env={}, cwd=str(tmp_path / "ws"),
            stdin_enabled=False,
        )
    )
    assert "/" in sandbox.fs_readable
    for denied in ("/proc/kcore", "/sys", "/dev/shm", "/dev/mqueue"):
        assert denied in sandbox.fs_denied


def test_network_enabled_maps_to_allowlist():
    executor = SandlockExecutor(
        workspace_dir="/tmp/ws",
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=True,
        enable_network=True,
    )
    sandbox = executor._build_sandbox(
        ExecConfig(
            cmd=["x"], env={}, cwd="/tmp/ws", stdin_enabled=False
        )
    )
    assert "pypi.org:443" in sandbox.net_allow


def test_bash_to_sh_fallback():
    assert SandlockExecutor.resolve_cmd(["/bin/bash", "-l", "-c", "echo hi"]) == [
        "/bin/sh",
        "-l",
        "-c",
        "echo hi",
    ]
    assert SandlockExecutor.resolve_cmd(["/usr/bin/python3"]) == ["/usr/bin/python3"]
