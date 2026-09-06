"""E2B executor request to sandlock policy API mapping.

The long-lived instance policy (``_build_instance_policy``) maps the
command-independent ceiling; per-command ``cwd``/``env``/``clean_env`` and
bind allowances are exec parameters (``_exec_params``) and never appear on
the policy object.
"""

from __future__ import annotations

import os

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


def test_root_image_rootfs_policy_carries_supervisor_mediation(
    monkeypatch, tmp_path
) -> None:
    """Root worker + image-rootfs shape opts into the supervisor mediation tier
    (pre-M4 baseline: explicit downgrade tier restoring F9-era semantics; to be
    removed once route-B per-sandbox supervision lands)."""
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
    sandbox = _policy(executor)
    assert sandbox.mediation_run_as == "supervisor"


def test_nonroot_policy_keeps_caller_mediation(monkeypatch) -> None:
    """Non-root worker keeps the fork's default caller mediation tier."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
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
    assert sandbox.mediation_run_as == "caller"


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
