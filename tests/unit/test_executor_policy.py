"""Sandlock executor policy for image-rootfs mode (chroot mounts, cwd, /dev).

Works without the native sandlock library: ``_build_sandbox`` falls back to a
plain kwargs namespace off-Linux, which is exactly what we assert on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_net_isolation_fd_inject_and_port_mappings_passthrough(tmp_path: Path) -> None:
    """E7.2: net_isolation / fd_inject_connect / port_mappings flow into the
    sandlock Sandbox kwargs; port_mappings requires net_isolation."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = SandlockExecutor(
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
        enable_net_isolation=True,
        fd_inject_connect=True,
        port_mappings={"50006": "8080"},
    )
    sb = _policy(executor, cwd="/workspace")
    assert sb.net_isolation is True
    assert sb.fd_inject_connect is True
    assert sb.port_mappings == {50006: 8080}


def test_fd_inject_without_net_isolation_passthrough(tmp_path: Path) -> None:
    """S2.1 shape: shared netns + fd injection stays available independently."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = SandlockExecutor(
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
        fd_inject_connect=True,
    )
    sb = _policy(executor, cwd="/workspace")
    assert getattr(sb, "net_isolation", False) is False
    assert sb.fd_inject_connect is True


def test_port_mappings_require_net_isolation(tmp_path: Path) -> None:
    """S2.5 mappings without net_isolation fail closed at executor creation."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError, match="net isolation"):
        SandlockExecutor(
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
            port_mappings={"50006": "8080"},
        )


def test_mcp_gateway_netns_identity_mapping(tmp_path: Path) -> None:
    """E7.1: under net_isolation the MCP gateway port is mapped onto the
    sandbox's own listener (host 50005+ -> same sandbox port), so the /mcp
    proxy keeps dialing 127.0.0.1:<port> on the worker."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = SandlockExecutor(
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
        enable_net_isolation=True,
        fd_inject_connect=True,
    )
    sb = executor._build_sandbox(
        ExecConfig(
            cmd=["/usr/local/bin/python3", "/usr/bin/mcp-gateway", "--config", "{}"],
            env={"MCP_PORT": "51234"},
            cwd="/workspace",
            stdin_enabled=False,
        )
    )
    assert sb.net_isolation is True
    assert sb.fd_inject_connect is True
    assert sb.port_mappings == {51234: 51234}
