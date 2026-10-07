"""Sandlock isolation tests (Linux + Landlock ABI >= 6 only)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    route_b_sandbox,
    run_sh,
    sandbox_tmpdir,
)


def _one_shot(sh: str) -> tuple[int, bytes, bytes]:
    """Run one shell command in a pure sandbox, in the deployment's shape.

    Every case in this file asserts a *refusal*, and a refusal assertion passes
    on any non-zero exit -- including the one a sandbox that never got created
    produces. Hand-building the executor (as these cases used to) stopped being
    a shape a deployment has when N15 made the pure shape mediated: on a root
    worker the fork now refuses in-process mediation (SL-1), so the assertions
    below would have been measuring the harness. Going through
    `route_b_sandbox` means they measure the product again.
    """
    executor, workspace = route_b_sandbox(None, None, workspace=sandbox_tmpdir())
    try:
        require_mediation_capable(executor)
        return asyncio.run(run_sh(executor, workspace, sh))
    finally:
        executor.close()


@pytest.mark.usefixtures("require_sandlock")
def test_read_etc_passwd_denied():
    code, out, err = _one_shot("cat /etc/passwd")
    assert code != 0, out
    assert b"root:" not in out


@pytest.mark.usefixtures("require_sandlock")
def test_write_outside_workspace_denied():
    code, out, err = _one_shot("echo x > /tmp/escaped")
    assert code != 0, out


@pytest.mark.usefixtures("require_sandlock")
def test_sys_and_proc_kcore_denied():
    for probe in ("cat /proc/kcore", "ls /sys"):
        code, out, err = _one_shot(probe)
        assert code != 0, out


@pytest.mark.usefixtures("require_sandlock")
def test_default_network_denied():
    ws = Path(sandbox_tmpdir())
    (ws / "net_probe.py").write_text(
        "import urllib.request, sys;"
        "sys.exit(0 if urllib.request.urlopen('http://example.com', timeout=3) else 1)"
    )
    code, out, err = _one_shot("/usr/local/bin/python3 /workspace/net_probe.py")
    assert code != 0, out


@pytest.mark.usefixtures("require_sandlock")
def test_install_to_system_path_denied():
    code, out, err = _one_shot("echo x > /usr/local/bin/pwned")
    assert code != 0, out


@pytest.mark.usefixtures("require_sandlock", "require_sandbox_file_ownership")
def test_user_cli_install_within_workspace_persists():
    """User-level installs into the sandbox dir survive across commands."""
    executor, workspace = route_b_sandbox(None, None, workspace=sandbox_tmpdir())
    try:
        require_mediation_capable(executor)

        async def _install_then_run():
            # One instance, two commands: the point is that the second one sees
            # the first one's file (a *fresh* sandbox cannot prove persistence).
            first = await run_sh(
                executor,
                workspace,
                "mkdir -p bin && printf '#!/bin/sh\\necho hi\\n' > bin/tool "
                "&& chmod +x bin/tool",
            )
            second = await run_sh(executor, workspace, "bin/tool")
            return first, second

        (code, out, err), (second_code, second_out, second_err) = asyncio.run(
            _install_then_run()
        )
        assert code == 0, err
        assert second_code == 0, second_err
        assert second_out.strip() == b"hi"
    finally:
        executor.close()


@pytest.mark.usefixtures("require_sandlock")
async def test_dev_shm_absent_but_dev_null_writable():
    """minimal_dev replaces the whole-tree /dev mount in the image-rootfs
    chroot: only the six single-node mounts exist, so /dev/shm is not present
    at all (no cross-sandbox tmpfs/queue surface) while /dev/null stays a
    writable host chardev.

    Built through the worker's own path (pooled host uid + ``E2B_OWN_IDENTITY=auto``
    slot): this is the mediated chroot shape, and mediation now runs as the
    sandbox's host uid, not as the worker -- see
    ``tests/security/conftest.py::route_b_sandbox``.
    """
    rootfs = resolve_test_rootfs("python:3.11-slim")
    executor, workspace = route_b_sandbox("python:3.11-slim", rootfs)
    probe = (
        "test -e /dev/shm && echo SHM_VISIBLE || echo SHM_ABSENT; "
        "if echo x > /dev/null 2>/dev/null; then echo DEV_NULL_WRITABLE; "
        "else echo DEV_NULL_BLOCKED; fi"
    )
    try:
        require_mediation_capable(executor)
        code, out, err = await run_sh(executor, workspace, probe)
        assert (code, out.strip(), err) == (0, b"SHM_ABSENT\nDEV_NULL_WRITABLE", b"")
    finally:
        executor.close()
