"""Sandlock executor instance-ceiling policy (image-rootfs chroot shape).

The instance policy (``_build_instance_policy``) carries only the
command-independent ceiling -- fs/chroot/network/bind allowances -- while
``cwd``/``env``/``clean_env``/``bind_ports`` travel per exec via
``_exec_params``. Works without the native sandlock library: the builder
falls back to a plain kwargs namespace off-Linux, which is exactly what we
assert on.
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


def _policy(executor: SandlockExecutor):
    return executor._build_instance_policy()


def _params(
    executor: SandlockExecutor,
    *,
    cmd: list[str],
    cwd: str,
    env: dict | None = None,
    bind_ports=None,
):
    config = ExecConfig(
        cmd=cmd,
        env=dict(env or {}),
        cwd=cwd,
        stdin_enabled=False,
    )
    return executor._exec_params(config, bind_ports=bind_ports)


def test_image_rootfs_mounts_workspace_dev_and_maps_cwd(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)
    sb = _policy(executor)

    assert sb.chroot == str(rootfs)
    assert sb.fs_mount["/workspace"] == str(ws)
    assert sb.fs_mount["/home/user"] == str(ws)
    # The whole-tree /dev mount stays in place until the minimal_dev swap
    # (a later task, M4 D7); this task only moves cwd out of the policy.
    assert sb.fs_mount["/dev"] == "/dev"
    # cwd is a per-exec parameter now, never part of the ceiling.
    assert getattr(sb, "cwd", None) is None
    assert getattr(sb, "env", None) in (None, {})
    # A host workspace cwd maps to /workspace inside the chroot.
    params = _params(executor, cmd=["/bin/sh"], cwd=str(ws))
    assert params["cwd"] == "/workspace"


def test_image_rootfs_keeps_explicit_chroot_cwd(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)

    assert _params(executor, cmd=["/bin/sh"], cwd="/workspace")["cwd"] == "/workspace"
    assert _params(executor, cmd=["/bin/sh"], cwd="/tmp")["cwd"] == "/tmp"


def test_non_chroot_cwd_passes_through_unchanged() -> None:
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
    params = _params(executor, cmd=["/bin/sh"], cwd="/tmp/ws")
    assert params["cwd"] == "/tmp/ws"


def test_dev_shared_paths_denied_but_ptmx_writable(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = _policy(_executor(rootfs, ws))
    assert "/dev/shm" in sb.fs_denied
    assert "/dev/mqueue" in sb.fs_denied
    # The instance may serve pty commands at any point, so the pty device
    # grants are part of the ceiling (they leave with minimal_dev in the
    # later /dev task; native ExecStdio.PTY is host-side and needs none).
    assert "/dev/ptmx" in sb.fs_writable
    assert "/dev/pts" in sb.fs_writable
    assert "/dev/ptmx" in sb.fs_readable
    assert "/dev/pts" in sb.fs_readable


def test_set_mcp_bind_port_controls_instance_bind_ceiling(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)
    # Without an allocated MCP port no bind allowance exists on the ceiling.
    # (real fork default is an empty list, off-Linux namespace omits the key).
    assert getattr(_policy(executor), "net_allow_bind", None) in (None, [])
    executor.set_mcp_bind_port(51234)
    assert _policy(executor).net_allow_bind == [51234]


def test_mcp_gateway_bind_allowance_moves_to_exec_params(tmp_path: Path) -> None:
    """The gateway port is no longer sniffed from command env; the allocated
    port lands on the instance ceiling and the gateway exec carries it as a
    per-exec ``bind_ports`` allowance."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)
    executor.set_mcp_bind_port(51234)
    gateway_cfg = ExecConfig(
        cmd=[
            "/usr/local/bin/python3",
            "/usr/bin/mcp-gateway",
            "--config",
            "{}",
        ],
        env={},
        cwd="/workspace",
        stdin_enabled=False,
    )
    params = executor._exec_params(
        gateway_cfg, bind_ports=executor._bind_ports_for(gateway_cfg)
    )
    assert params["bind_ports"] == [51234]

    # Ordinary commands exec without any bind allowance.
    plain = ExecConfig(
        cmd=["/bin/sh"],
        env={},
        cwd="/workspace",
        stdin_enabled=False,
    )
    assert executor._bind_ports_for(plain) is None
    plain_params = executor._exec_params(plain, bind_ports=executor._bind_ports_for(plain))
    assert "bind_ports" not in plain_params


def test_exec_params_mapping_pty_and_clean_env(tmp_path: Path) -> None:
    """Per-exec params carry the full env plus clean_env; a pty config maps
    to the same params (the stdio -> ExecStdio.PTY selection happens in
    ``start()``, exercised in test_sandlock_executor_instance.py)."""
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "cat"],
        env={"A": "b"},
        cwd=str(ws),
        stdin_enabled=True,
        pty=True,
    )
    params = executor._exec_params(cfg)
    assert params["clean_env"] is True
    assert params["env"] == {"A": "b"}
    assert params["cwd"] == "/workspace"
    assert "bind_ports" not in params


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
    sb = _policy(executor)
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
    sb = _policy(executor)
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
    executor.set_mcp_bind_port(51234)
    sb = _policy(executor)
    assert sb.net_isolation is True
    assert sb.fd_inject_connect is True
    assert sb.net_allow_bind == [51234]
    assert sb.port_mappings == {51234: 51234}
