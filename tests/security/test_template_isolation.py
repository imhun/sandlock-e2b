"""Template-image isolation (Linux + Docker + Landlock ABI >= 6 only)."""

from __future__ import annotations

import os
import shutil

import pytest


@pytest.mark.usefixtures("require_sandlock")
def test_image_rootfs_execution():
    image = os.environ.get("E2B_BASE_IMAGE") or os.environ.get("E2B_TEMPLATE_IMAGES")
    if not image:
        pytest.skip("no base image configured for template isolation test")
    if shutil.which("docker") is None:
        pytest.skip("docker CLI required for image rootfs resolution")

    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    import tempfile

    image_name = image
    cache = tempfile.mkdtemp()
    rootfs = resolve_image_rootfs(image_name, cache)
    ws = tempfile.mkdtemp()
    executor = SandlockExecutor(
        workspace_dir=ws,
        base_image=image_name,
        image_rootfs=rootfs,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    # The command must run inside the image rootfs, not the host.
    result = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/sh", "-c", "cat /etc/os-release || true"],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/bin/sh", "-c", "cat /etc/os-release || true"])
    assert result.exit_code == 0
    assert b"Debian" in result.stdout or b"Ubuntu" in result.stdout


@pytest.mark.usefixtures("require_sandlock")
def test_image_rootfs_cannot_reach_host_filesystem():
    """The chroot restricts the path space: host paths are not visible, and
    the Landlock "/" rule only covers the image rootfs, not the host root."""
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    import tempfile

    rootfs = resolve_image_rootfs("python:3.11-slim", tempfile.mkdtemp())
    ws = tempfile.mkdtemp()
    executor = SandlockExecutor(
        workspace_dir=ws,
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
    probe = (
        f"if [ -e {tempfile.mkdtemp(prefix='e2b-host-marker-')} ]; then echo HOST_VISIBLE; "
        "else echo HOST_HIDDEN; fi"
    )
    result = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/sh", "-c", probe],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/bin/sh", "-c", probe])
    # A host-only random path is not visible inside the chroot: the host
    # filesystem is unreachable even though Landlock "/" covers the image
    # rootfs (/workspace is the sandbox's own mount, so it is not a probe).
    assert result.stdout.strip() == b"HOST_HIDDEN"
