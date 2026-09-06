"""Sandlock isolation tests (Linux + Landlock ABI >= 6 only)."""

from __future__ import annotations

import pytest

from tests.security.conftest import sandbox_tmpdir


@pytest.mark.usefixtures("require_sandlock")
def test_read_etc_passwd_denied():
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    import tempfile

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
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
    sb = executor._build_sandbox(
        ExecConfig(cmd=["/bin/cat", "/etc/passwd"], env={}, cwd=ws, stdin_enabled=False)
    )
    result = sb.run(["/bin/cat", "/etc/passwd"])
    assert result.exit_code != 0
    assert b"root:" not in result.stdout


@pytest.mark.usefixtures("require_sandlock")
def test_write_outside_workspace_denied():
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    import tempfile

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
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
    result = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/sh", "-c", "echo x > /tmp/escaped"],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/bin/sh", "-c", "echo x > /tmp/escaped"])
    assert result.exit_code != 0


@pytest.mark.usefixtures("require_sandlock")
def test_sys_and_proc_kcore_denied():
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    import tempfile

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
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
    for probe in ("cat /proc/kcore", "ls /sys"):
        result = executor._build_sandbox(
            ExecConfig(
                cmd=["/bin/sh", "-c", probe],
                env={},
                cwd=ws,
                stdin_enabled=False,
            )
        ).run(["/bin/sh", "-c", probe])
        assert result.exit_code != 0


@pytest.mark.usefixtures("require_sandlock")
def test_default_network_denied():
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    import tempfile

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
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
    script = (
        "import urllib.request, sys;"
        "sys.exit(0 if urllib.request.urlopen('http://example.com', timeout=3) else 1)"
    )
    result = executor._build_sandbox(
        ExecConfig(
            cmd=["/usr/local/bin/python3", "-c", script],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/usr/local/bin/python3", "-c", script])
    assert result.exit_code != 0


@pytest.mark.usefixtures("require_sandlock")
def test_install_to_system_path_denied():
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    import tempfile

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
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
    result = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/sh", "-c", "echo x > /usr/local/bin/pwned"],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/bin/sh", "-c", "echo x > /usr/local/bin/pwned"])
    assert result.exit_code != 0


@pytest.mark.usefixtures("require_sandlock", "require_sandbox_file_ownership")
def test_user_cli_install_within_workspace_persists():
    """User-level installs into the sandbox dir survive across commands."""
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    import tempfile

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
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
    cfg = ExecConfig(
        cmd=["/bin/sh", "-c", "mkdir -p bin && printf '#!/bin/sh\\necho hi\\n' > bin/tool && chmod +x bin/tool"],
        env={"PATH": f"{ws}/bin:/usr/bin:/bin"},
        cwd=ws,
        stdin_enabled=False,
    )
    result = executor._build_sandbox(cfg).run(
        ["/bin/sh", "-c", "mkdir -p bin && printf '#!/bin/sh\\necho hi\\n' > bin/tool && chmod +x bin/tool"]
    )
    assert result.exit_code == 0
    # Second command in a fresh Sandbox instance sees the persisted file.
    second = executor._build_sandbox(cfg).run([f"{ws}/bin/tool"])
    assert second.exit_code == 0
    assert second.stdout.strip() == b"hi"


@pytest.mark.usefixtures("require_sandlock")
def test_dev_shm_absent_but_dev_null_writable(tmp_path):
    """minimal_dev replaces the whole-tree /dev mount in the image-rootfs
    chroot: only the six single-node mounts exist, so /dev/shm is not present
    at all (no cross-sandbox tmpfs/queue surface) while /dev/null stays a
    writable host chardev."""
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    rootfs = resolve_image_rootfs("python:3.11-slim", tmp_path / "cache")
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
    probe = (
        "test -e /dev/shm && echo SHM_VISIBLE || echo SHM_ABSENT; "
        "if echo x > /dev/null 2>/dev/null; then echo DEV_NULL_WRITABLE; "
        "else echo DEV_NULL_BLOCKED; fi"
    )
    result = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/sh", "-c", probe],
            env={},
            cwd=str(tmp_path / "ws"),
            stdin_enabled=False,
        )
    ).run(["/bin/sh", "-c", probe])
    assert result.exit_code == 0
    assert result.stderr == b""
    assert result.stdout.strip() == b"SHM_ABSENT\nDEV_NULL_WRITABLE"
