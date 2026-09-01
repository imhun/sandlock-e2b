"""E3.2: per-sandbox host uid isolation at the executor layer.

Different sandboxes get different host uids from the worker pool; sandlock
``RunAs`` maps each sandbox to its uid (inside the namespace it is uid 0 /
fake root, on the host the allocated uid). The worker chowns every sandbox
workspace to that uid with ``0700``, so the same relative path
(``workspace/secret.txt``) in two sandboxes is mutually invisible: the
unmediated metadata/exec syscalls (``chdir``/``stat``) are stopped by the
kernel DAC check on the 0700 directory, and the mediated ``open`` path is
stopped by sandlock's grant check (the other sandbox is never granted the
workspace). This is the root form of the S1.2 kernel-isolation contract at
the E2B layer.

Under a non-root supervisor sandlock refuses arbitrary ``RunAs`` uids
(S1.2 fail-closed contract, verified in the sandlock fork's own suite);
these tests are the root form and skip otherwise.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor

UID_A = 10000
UID_B = 10001


def _executor(workspace: str, uid: int) -> SandlockExecutor:
    return SandlockExecutor(
        workspace_dir=workspace,
        base_image=None,
        image_rootfs=None,
        host_uid=uid,
        per_sandbox_uid=True,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )


def _run(workspace: str, uid: int, cmd: list[str]):
    proc = _executor(workspace, uid)._build_sandbox(
        ExecConfig(
            cmd=cmd,
            env={},
            cwd=workspace,
            stdin_enabled=False,
        )
    ).run(cmd)
    return proc


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root (Docker runner)")
@pytest.mark.usefixtures("require_sandlock")
def test_distinct_host_uids_isolate_same_path_files(workspace):
    # The workspace root (0755) is traversable by every sandbox uid; each
    # sandbox workspace under it is chowned + 0700.
    ws_a = workspace / "sbx_a"
    ws_a.mkdir()
    (ws_a / "workspace").mkdir()
    ws_b = workspace / "sbx_b"
    ws_b.mkdir()
    (ws_b / "workspace").mkdir()

    from envd_service.uid_pool import apply_sandbox_ownership

    apply_sandbox_ownership(ws_a, UID_A)
    apply_sandbox_ownership(ws_b, UID_B)
    assert ws_a.stat().st_uid == UID_A
    assert ws_b.stat().st_uid == UID_B
    assert stat.S_IMODE(ws_a.stat().st_mode) == 0o700
    assert stat.S_IMODE(ws_b.stat().st_mode) == 0o700

    # Both sandboxes write the same relative path; each sees only its own.
    write_a = _run(
        str(ws_a),
        UID_A,
        ["/bin/sh", "-c", "printf A-secret > workspace/secret.txt"],
    )
    assert write_a.exit_code == 0
    assert write_a.stderr == b""
    write_b = _run(
        str(ws_b),
        UID_B,
        ["/bin/sh", "-c", "printf B-secret > workspace/secret.txt"],
    )
    assert write_b.exit_code == 0
    assert write_b.stderr == b""
    secret_a = ws_a / "workspace" / "secret.txt"
    secret_b = ws_b / "workspace" / "secret.txt"
    assert secret_a.read_text(encoding="utf-8") == "A-secret"
    assert secret_b.read_text(encoding="utf-8") == "B-secret"

    # Kernel DAC backstop (unmediated syscalls): B cannot chdir or stat into
    # A's 0700 workspace even though both uids are different sandboxes.
    chdir_b = _run(
        str(ws_b), UID_B, ["/bin/sh", "-c", f"cd {ws_a / 'workspace'}"]
    )
    assert chdir_b.exit_code != 0
    assert (
        chdir_b.stderr
        == f"/bin/sh: 1: cd: can't cd to {ws_a / 'workspace'}\n".encode()
    )
    stat_b = _run(str(ws_b), UID_B, ["/bin/stat", str(ws_a / "workspace")])
    assert stat_b.exit_code != 0
    assert (
        stat_b.stderr
        == f"stat: cannot statx '{ws_a / 'workspace'}': Permission denied\n".encode()
    )

    # Open path: sandlock's grant check (B is never granted A's workspace)
    # blocks the read of A's same-path file.
    cat_b = _run(
        str(ws_b), UID_B, ["/bin/cat", str(secret_a)]
    )
    assert cat_b.exit_code != 0
    assert (
        cat_b.stderr == f"cat: {secret_a}: Permission denied\n".encode()
    )

    # A's control read succeeds.
    cat_a = _run(str(ws_a), UID_A, ["/bin/cat", str(secret_a)])
    assert cat_a.exit_code == 0
    assert cat_a.stdout == b"A-secret"


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_workspace_host_ownership_applied(workspace):
    """The workspace (including nested snapshot-style files) is chowned to
    the allocated uid and tightened to 0700."""
    from envd_service.uid_pool import apply_sandbox_ownership

    ws = workspace / "sbx_a"
    ws.mkdir()
    (ws / "workspace").mkdir()
    (ws / "workspace" / "from-snapshot.txt").write_text("x", encoding="utf-8")
    apply_sandbox_ownership(ws, UID_A)
    assert ws.stat().st_uid == UID_A
    assert stat.S_IMODE(ws.stat().st_mode) == 0o700
    assert (ws / "workspace").stat().st_uid == UID_A
    assert (ws / "workspace" / "from-snapshot.txt").stat().st_uid == UID_A
