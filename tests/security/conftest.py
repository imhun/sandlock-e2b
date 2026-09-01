"""Sandlock availability helpers for security tests."""

from __future__ import annotations

import os
import sys

import pytest


def sandlock_ready() -> bool:
    """True only on Linux with a working sandlock and Landlock ABI >= 6."""
    if sys.platform != "linux":
        return False
    try:
        import sandlock

        return sandlock.landlock_abi_version() >= 6
    except Exception:
        return False


@pytest.fixture()
def require_sandlock():
    if not sandlock_ready():
        pytest.skip(
            "Sandlock security tests require Linux with Landlock ABI >= 6 "
            "(run inside the Docker test runner / Linux CI)"
        )
    # The environment claims sandlock support; prove it can actually create a
    # confined process before any isolation assertion runs. A create failure
    # here must fail loudly instead of letting ``exit_code != 0`` assertions
    # below pass vacuously.
    import tempfile

    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    ws = tempfile.mkdtemp()
    # mkdtemp creates 0700; the sandbox host uid (root workers: 1000, or an
    # allocated pool uid) needs traverse permission on the workspace and its
    # parents, mirroring the 0755 workspace_base of real deployments.
    os.chmod(ws, 0o755)
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
    smoke = executor._build_sandbox(
        ExecConfig(
            cmd=["/bin/echo", "ok"],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/bin/echo", "ok"])
    if smoke.exit_code != 0:
        pytest.fail(
            f"sandlock cannot execute commands in this environment "
            f"(smoke test exit_code={smoke.exit_code}, error={smoke.error!r}); "
            "run the test runner with seccomp unconfined / privileged"
        )
