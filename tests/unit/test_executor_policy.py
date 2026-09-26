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


def test_minimal_dev_mirror_matches_native_helper() -> None:
    """The off-Linux ``_MINIMAL_DEV_MOUNTS`` mirror must stay identical to
    the fork's ``sandlock.minimal_dev()`` so policy-shape assertions cannot
    drift from the real chroot /dev set (skip the assert when the native
    module is not importable, i.e. off-Linux)."""
    import envd_service.executors.sandlock as sandlock_mod

    if sandlock_mod.sandlock is None:
        pytest.skip("native sandlock module unavailable off-Linux")
    assert (
        sandlock_mod._MINIMAL_DEV_MOUNTS == sandlock_mod.sandlock.minimal_dev()
    )


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


def test_image_rootfs_mounts_workspace_minimal_dev_and_maps_cwd(
    tmp_path: Path,
) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)
    sb = _policy(executor)

    assert sb.chroot == str(rootfs)
    # Declaration order is load-bearing (fork A2: `host_to_virtual` breaks
    # host-source ties by declaration order), so the shared workspace
    # directory's canonical alias is the first one declared: /home/user.
    assert sb.fs_mount["/home/user"] == str(ws)
    assert sb.fs_mount["/workspace"] == str(ws)
    assert list(sb.fs_mount)[:2] == ["/home/user", "/workspace"]
    # minimal_dev replaces the whole-tree host /dev mount: only the six
    # single-node mounts (ptmx, pts, null, urandom, zero, tty) are visible
    # under the chroot's /dev, so /dev/shm and /dev/mqueue cannot leak in.
    minimal_dev_keys = {
        "/dev/ptmx",
        "/dev/pts",
        "/dev/null",
        "/dev/urandom",
        "/dev/zero",
        "/dev/tty",
    }
    assert {k for k in sb.fs_mount if k == "/dev" or k.startswith("/dev/")} == (
        minimal_dev_keys
    )
    # The rootfs carries the /dev parent dir for traversal and listings.
    assert (rootfs / "dev").is_dir()
    # cwd is a per-exec parameter now, never part of the ceiling.
    assert getattr(sb, "cwd", None) is None
    assert getattr(sb, "env", None) in (None, {})
    # A host workspace cwd (and an empty default) maps to the canonical
    # /home/user alias inside the chroot.
    params = _params(executor, cmd=["/bin/sh"], cwd=str(ws))
    assert params["cwd"] == "/home/user"
    assert _params(executor, cmd=["/bin/sh"], cwd="")["cwd"] == "/home/user"


def test_image_rootfs_keeps_explicit_chroot_cwd(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    executor = _executor(rootfs, ws)

    # Explicit in-sandbox paths pass through unchanged (the alias is only the
    # *default* spelling, not a rewrite of what the caller asked for).
    assert _params(executor, cmd=["/bin/sh"], cwd="/workspace")["cwd"] == "/workspace"
    assert _params(executor, cmd=["/bin/sh"], cwd="/home/user")["cwd"] == "/home/user"
    assert _params(executor, cmd=["/bin/sh"], cwd="/tmp")["cwd"] == "/tmp"


def test_non_chroot_cwd_is_the_host_path_the_fork_can_chdir_to() -> None:
    """N15: the pure shape still answers with the *host* path, and that is a
    decision, not an oversight.

    The fork's launch cwd is a real ``chdir`` on ``chroot_root.join(cwd)``. The
    image shape therefore answers with the virtual ``/home/user`` (its rootfs
    has that directory); the pure shape's root is "/", so the same answer would
    be the host's own ``/home/user``, which need not exist. It answers with the
    host workspace path -- which the mediator then maps back to ``/home/user``
    through the mount table, so the sandbox still reports the canonical alias.
    """
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
    other = _params(executor, cmd=["/bin/sh"], cwd="/tmp")
    assert other["cwd"] == "/tmp"
    # No cwd at all means the workspace, not "leave the worker's cwd inherited".
    default = _params(executor, cmd=["/bin/sh"], cwd="")
    assert default["cwd"] == "/tmp/ws"


def test_dev_shared_paths_absent_with_minimal_dev_mounts(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = _policy(_executor(rootfs, ws))
    # minimal_dev keeps only the six single-node /dev mounts: /dev/shm and
    # /dev/mqueue never exist in the sandbox view, so the carve-out denials
    # are gone; /proc/kcore and /sys stay as defensive entries.
    assert set(sb.fs_denied) == {"/proc/kcore", "/sys"}
    assert "/dev/shm" not in sb.fs_denied
    assert "/dev/mqueue" not in sb.fs_denied
    # Native ExecStdio.PTY is host-side, so the chroot grants no pty device
    # nodes in the sandbox view.
    assert "/dev/ptmx" not in sb.fs_writable
    assert "/dev/pts" not in sb.fs_writable
    assert "/dev/ptmx" not in sb.fs_readable
    assert "/dev/pts" not in sb.fs_readable


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
    assert params["cwd"] == "/home/user"
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
        pid_ns=False,
        bind_inject=True,
        port_mappings={"50006": "8080"},
    )
    sb = _policy(executor)
    assert sb.net_isolation is True
    assert sb.fd_inject_connect is True
    # S2.5 bind injection rides with the mappings: the mapped port becomes a
    # socket the sandbox itself listens on, so the supervisor leaves the
    # accept/readiness path.
    assert sb.net_bind_inject is True
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
        pid_ns=False,
        bind_inject=False,
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
        pid_ns=False,
        bind_inject=False,
    )
    executor.set_mcp_bind_port(51234)
    sb = _policy(executor)
    assert sb.net_isolation is True
    assert sb.fd_inject_connect is True
    assert sb.net_allow_bind == [51234]
    assert sb.port_mappings == {51234: 51234}
