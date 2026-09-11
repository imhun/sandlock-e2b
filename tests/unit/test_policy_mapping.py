"""E2B executor request to sandlock policy API mapping.

The long-lived instance policy (``_build_instance_policy``) maps the
command-independent ceiling; per-command ``cwd``/``env``/``clean_env`` and
bind allowances are exec parameters (``_exec_params``) and never appear on
the policy object.
"""

from __future__ import annotations

import os

import envd_service.executors.sandlock as sl
from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor


def _policy(executor: SandlockExecutor):
    return executor._build_instance_policy()


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
    sandbox = _policy(executor)
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
    # Per-command fields are exec params, never policy fields.
    # (real fork defaults: cwd=None, env={}, clean_env=False, net_allow_bind=[])
    assert getattr(sandbox, "cwd", None) is None
    assert getattr(sandbox, "env", None) in (None, {})
    assert getattr(sandbox, "clean_env", None) in (None, False)
    assert getattr(sandbox, "net_allow_bind", None) in (None, [])


def test_image_rootfs_shape_keeps_only_defensive_denials(tmp_path) -> None:
    """With an image rootfs the whole tree is readable, so deny the two
    defensive paths; minimal_dev removed the /dev/shm + /dev/mqueue carve-out
    requirement (those paths never exist in the chroot's /dev view)."""
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
    sandbox = _policy(executor)
    assert "/" in sandbox.fs_readable
    assert set(sandbox.fs_denied) == {"/proc/kcore", "/sys"}
    assert "/dev/shm" not in sandbox.fs_denied
    assert "/dev/mqueue" not in sandbox.fs_denied


def test_volume_views_map_under_both_workspace_aliases(tmp_path) -> None:
    """A4: the runtime context registers every volume view under both
    workspace aliases (``/workspace/<rel>`` and ``/home/user/<rel>``); the
    chroot policy must carry both spellings, and the shared workspace
    directory must stay declared first so the fork's declaration-order tie
    break keeps ``/home/user`` canonical.
    """
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    volume = str(tmp_path / "vol")
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
        fs_mounts={
            "/workspace/mnt/data": volume,
            "/home/user/mnt/data": volume,
        },
    )
    sandbox = _policy(executor)
    assert sandbox.fs_mount["/home/user"] == str(ws)
    assert sandbox.fs_mount["/workspace"] == str(ws)
    assert list(sandbox.fs_mount)[:2] == ["/home/user", "/workspace"]
    assert sandbox.fs_mount["/workspace/mnt/data"] == volume
    assert sandbox.fs_mount["/home/user/mnt/data"] == volume
    # The rootfs carries the mount-point parents the chroot needs for chdir.
    assert (rootfs / "workspace/mnt/data").is_dir()
    assert (rootfs / "home/user/mnt/data").is_dir()


def test_chroot_policy_sends_no_mediation_tier(monkeypatch, tmp_path) -> None:
    """The supervisor downgrade tier is gone (2026-09-10).

    E2B used to send `mediation_run_as='supervisor'` for a root worker with the
    image-rootfs shape -- which is precisely what made mediated writes belong to
    the worker instead of the sandbox (T5). Chroot sandboxes now run on a
    supervise slot whose euid *is* the sandbox's host uid, so no tier is sent at
    all and the fork keeps its fail-closed `caller` default: an in-process
    chroot create is refused rather than silently degrading ownership.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 0)
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
    ceiling = executor._policy_ceiling()
    assert "mediation_run_as" not in ceiling
    # Mediation really is in play in this shape -- which is why the fork's
    # default would refuse it in-process, and why route B exists.
    assert ceiling["fs_denied"] == ["/proc/kcore", "/sys"]
    assert ceiling["chroot"] == str(rootfs)
    assert executor._route_b_active is False, (
        "no route-B config was passed here, so this is the disclosed shape"
    )


def test_one_shot_builder_sends_no_mediation_tier(monkeypatch, tmp_path) -> None:
    """The one-shot builder is the second place the tier used to be set.

    Off Linux ``_build_sandbox`` hands back the kwargs mirror, where absence is
    directly visible; on Linux the SDK object always carries the field, so the
    assertion becomes "it is the fork's fail-closed default", which is the same
    fact from the other side.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 0)
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
    one_shot = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/true"], env={}, cwd=str(tmp_path / "ws"), stdin_enabled=False
        )
    )
    if sl.sandlock is None:
        assert "mediation_run_as" not in vars(one_shot)
    else:
        assert one_shot.mediation_run_as == "caller"


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
    sandbox = _policy(executor)
    assert "pypi.org:443" in sandbox.net_allow


def test_bash_to_sh_fallback():
    assert SandlockExecutor.resolve_cmd(["/bin/bash", "-l", "-c", "echo hi"]) == [
        "/bin/sh",
        "-l",
        "-c",
        "echo hi",
    ]
    assert SandlockExecutor.resolve_cmd(["/usr/bin/python3"]) == ["/usr/bin/python3"]
