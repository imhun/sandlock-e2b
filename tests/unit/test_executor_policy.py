"""Sandlock executor policy for image-rootfs mode (chroot mounts, cwd, /dev).

Works without the native sandlock library: ``_build_sandbox`` falls back to a
plain kwargs namespace off-Linux, which is exactly what we assert on.
"""

from __future__ import annotations

from pathlib import Path

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor


def _executor(rootfs: Path, ws: Path) -> SandlockExecutor:
    return SandlockExecutor(
        workspace_dir=str(ws),
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


def _policy(executor: SandlockExecutor, *, cwd: str, pty: bool = False):
    return executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/sh"],
            env={},
            cwd=cwd,
            stdin_enabled=False,
            pty=pty,
        )
    )


def test_image_rootfs_mounts_workspace_dev_and_maps_cwd(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = _policy(_executor(rootfs, ws), cwd=str(ws))

    assert sb.chroot == str(rootfs)
    assert sb.fs_mount["/workspace"] == str(ws)
    assert sb.fs_mount["/home/user"] == str(ws)
    # PTY bridge + standard devices come from the container /dev.
    assert sb.fs_mount["/dev"] == "/dev"
    # A host workspace cwd maps to /workspace inside the chroot.
    assert sb.cwd == "/workspace"


def test_image_rootfs_keeps_explicit_chroot_cwd(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)

    assert _policy(executor, cwd="/workspace").cwd == "/workspace"
    assert _policy(executor, cwd="/tmp").cwd == "/tmp"


def test_dev_shared_paths_denied_but_ptmx_writable(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)

    sb = _policy(executor, cwd="/workspace", pty=True)
    assert "/dev/shm" in sb.fs_denied
    assert "/dev/mqueue" in sb.fs_denied
    # The PTY bridge path stays writable/readable.
    assert "/dev/ptmx" in sb.fs_writable
    assert "/dev/pts" in sb.fs_writable


def test_mcp_gateway_bind_port_from_env(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)
    sb = executor._build_sandbox(
        ExecConfig(
            cmd=["/usr/local/bin/python3", "/usr/bin/mcp-gateway", "--config", "{}"],
            env={"MCP_PORT": "51234"},
            cwd="/workspace",
            stdin_enabled=False,
        )
    )
    # Per-sandbox MCP port: the bind allowlist must match the allocated port
    # (sandboxes share the worker network namespace).
    assert sb.net_allow_bind == ["51234"]


def test_mcp_gateway_bind_default_port(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)
    sb = executor._build_sandbox(
        ExecConfig(
            cmd=["/usr/local/bin/python3", "/usr/bin/mcp-gateway", "--config", "{}"],
            env={},
            cwd="/workspace",
            stdin_enabled=False,
        )
    )
    assert sb.net_allow_bind == ["50005"]
