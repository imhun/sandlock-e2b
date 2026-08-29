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
    assert "/proc/kcore" in sandbox.fs_denied
    assert sandbox.net_allow == []


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
