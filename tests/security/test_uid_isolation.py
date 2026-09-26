"""E3.2: per-sandbox host uid isolation at the executor layer.

Different sandboxes get different host uids from the worker pool; sandlock
``RunAs`` maps each sandbox to its uid (inside the namespace it is uid 0 /
fake root, on the host the allocated uid). The worker chowns every sandbox
workspace to ``0770 <sandbox uid>:<worker gid>`` (fix round 1 / c1: the worker
is the data-plane owner and reaches the tree through its group), so the same relative path
(``workspace/secret.txt``) in two sandboxes is mutually invisible: the
unmediated metadata/exec syscalls (``chdir``/``stat``) are stopped by the
kernel DAC check on the workspace directory (0770, but the sandbox is not in
its group), and the mediated ``open`` path is
stopped by sandlock's grant check (the other sandbox is never granted the
workspace). This is the root form of the S1.2 kernel-isolation contract at
the E2B layer.

Under a non-root supervisor sandlock refuses arbitrary ``RunAs`` uids
(S1.2 fail-closed contract, verified in the sandlock fork's own suite);
these tests are the root form and skip otherwise.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.security.conftest import route_b_sandbox, run_sh

UID_A = 10000
UID_B = 10001


def _run(workspace: str, uid: int, cmd: list[str]):
    """Run one command as ``uid``, in the shape a worker would build.

    This used to hand-build an in-process sandbox carrying an explicit host
    uid. N15 made the pure shape mediated, and a *root* mediator running as a
    different sandbox uid is exactly what the fork refuses (SL-1), so the
    command has to run on a slot -- which is how a real worker gives one
    sandbox its own uid in the first place. What the case is about (kernel DAC
    between two ``0770 <uid>:<worker gid>`` trees) is untouched by that; only
    the route is.
    """
    executor, _ = route_b_sandbox(None, None, host_uid=uid, workspace=workspace)
    try:
        shell = cmd[2] if cmd[:2] == ["/bin/sh", "-c"] else " ".join(cmd)
        code, out, err = asyncio.run(run_sh(executor, workspace, shell))
    finally:
        executor.close()
    return SimpleNamespace(exit_code=code, stdout=out, stderr=err)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root (Docker runner)")
@pytest.mark.usefixtures("require_sandlock")
def test_distinct_host_uids_isolate_same_path_files(workspace):
    # The workspace root (0755) is traversable by every sandbox uid; each
    # sandbox workspace under it is chowned to `0770 <uid>:<worker gid>`.
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
    assert stat.S_IMODE(ws_a.stat().st_mode) == 0o770
    assert stat.S_IMODE(ws_b.stat().st_mode) == 0o770
    assert (ws_a.stat().st_gid, ws_b.stat().st_gid) == (os.getegid(), os.getegid())

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
    # A's `0770 A:worker-gid` workspace even though both uids are different
    # sandboxes -- B runs with `setgroups([])` and gid=B, so it is not in the
    # worker's group and the other bits are 0.
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
    assert stat_b.stderr in _diagnostic(
        "stat", f"cannot statx '{ws_a / 'workspace'}': Permission denied"
    )

    # Open path: sandlock's grant check (B is never granted A's workspace)
    # blocks the read of A's same-path file.
    cat_b = _run(
        str(ws_b), UID_B, ["/bin/cat", str(secret_a)]
    )
    assert cat_b.exit_code != 0
    assert cat_b.stderr in _diagnostic("cat", f"{secret_a}: Permission denied")

    # A's control read succeeds.
    cat_a = _run(str(ws_a), UID_A, ["/bin/cat", str(secret_a)])
    assert cat_a.exit_code == 0
    assert cat_a.stdout == b"A-secret"

    # Sandbox B writing/deleting inside A's tree is refused too.
    write_b = _run(
        str(ws_b),
        UID_B,
        ["/bin/sh", "-c", f"printf x > {ws_a / 'workspace' / 'b-wrote.txt'}"],
    )
    assert write_b.exit_code != 0
    assert write_b.stderr == (
        f"/bin/sh: 1: cannot create {ws_a / 'workspace' / 'b-wrote.txt'}: "
        "Permission denied\n"
    ).encode()
    rm_b = _run(str(ws_b), UID_B, ["/bin/rm", str(secret_a)])
    assert rm_b.exit_code != 0
    assert rm_b.stderr in _diagnostic(
        "rm", f"cannot remove '{secret_a}': Permission denied"
    )
    assert secret_a.read_text(encoding="utf-8") == "A-secret"


def _diagnostic(program: str, message: str) -> tuple[bytes, bytes]:
    """The exact stderr spellings a coreutils diagnostic can take.

    coreutils' ``error()`` prints whatever gnulib's ``set_program_name`` left in
    ``program_name``: <= 9.4 keeps the path as the caller invoked it
    (``/bin/ls: cannot access ...``), >= 9.5 strips it to the basename
    (``ls: cannot access ...``). Which one a lane gets is build trivia -- the
    container image ships 9.7, Ubuntu 24.04 ships 9.4 -- so pin *both* spellings
    exactly rather than one build's wording (the same treatment
    ``test_chroot_hardlink_into_a_branch_is_refused`` got for glibc's EXDEV
    text). Everything from the ``": "`` on is the contract.
    """
    return (
        f"{program}: {message}\n".encode(),
        f"/bin/{program}: {message}\n".encode(),
    )


def _as_uid(uid: int, *cmd: str, groups: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    """Run ``cmd`` as ``uidd:gid`` with either no groups or ``groups``.

    No sandlock involved: this is the bare-kernel form of the c1 isolation
    claim -- a sandbox is ``uid X, gid X, setgroups([])``, so the ``0770
    owner=<sandbox uid> group=<worker gid>`` tree is unreachable for it while
    the worker (same gid as the tree's group) reaches it.
    """
    argv = ["setpriv", "--reuid", str(uid), "--regid", str(uid)]
    if groups:
        argv += ["--groups", ",".join(str(g) for g in groups)]
    else:
        argv += ["--clear-groups"]
    return subprocess.run([*argv, *cmd], capture_output=True)


@pytest.mark.skipif(os.geteuid() != 0, reason="chown/setpriv require root")
def test_0770_group_bit_grants_the_worker_and_not_another_sandbox(workspace):
    """Fix round 1 (c1): the workspace is ``0770`` for the *worker's* group.

    Direct kernel check, no sandlock: ``uid B, gid B, setgroups([])`` -- the
    exact sandbox shape -- cannot list, write or delete inside A's tree, while
    the same uid B *with the worker's gid* can, which is exactly the access the
    worker itself uses for the data plane (files API, watcher, logs,
    snapshots).
    """
    from envd_service.uid_pool import apply_sandbox_ownership

    ws_a = workspace / "sbx_a"
    (ws_a / "workspace").mkdir(parents=True)
    (ws_a / "workspace" / "a.txt").write_text("A-secret", encoding="utf-8")
    apply_sandbox_ownership(ws_a, UID_A)
    assert ws_a.stat().st_gid == os.getegid()
    assert stat.S_IMODE(ws_a.stat().st_mode) == 0o770

    listings = _as_uid(UID_B, "/bin/ls", str(ws_a / "workspace"))
    assert listings.returncode != 0
    assert listings.stderr in _diagnostic(
        "ls", f"cannot access '{ws_a / 'workspace'}': Permission denied"
    )
    wrote = _as_uid(UID_B, "/bin/touch", str(ws_a / "workspace" / "b-wrote.txt"))
    assert wrote.returncode != 0
    assert wrote.stderr in _diagnostic(
        "touch",
        f"cannot touch '{ws_a / 'workspace' / 'b-wrote.txt'}': Permission denied",
    )
    removed = _as_uid(UID_B, "/bin/rm", str(ws_a / "workspace" / "a.txt"))
    assert removed.returncode != 0
    assert removed.stderr in _diagnostic(
        "rm", f"cannot remove '{ws_a / 'workspace' / 'a.txt'}': Permission denied"
    )
    assert (ws_a / "workspace" / "a.txt").read_text(encoding="utf-8") == "A-secret"

    # Positive control: the same uid B, but carrying the worker's gid (what a
    # worker process has), is inside the group and therefore reaches the tree.
    granted = _as_uid(
        UID_B, "/bin/cat", str(ws_a / "workspace" / "a.txt"), groups=(os.getegid(),)
    )
    assert granted.returncode == 0, granted.stderr
    assert granted.stdout == b"A-secret"


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_workspace_host_ownership_applied(workspace):
    """The workspace (including nested snapshot files) is chowned to
    `<allocated uid>:<worker gid>` at 0770 (fix round 1 / c1)."""
    from envd_service.uid_pool import apply_sandbox_ownership

    ws = workspace / "sbx_a"
    ws.mkdir()
    (ws / "workspace").mkdir()
    (ws / "workspace" / "from-snapshot.txt").write_text("x", encoding="utf-8")
    apply_sandbox_ownership(ws, UID_A)
    assert ws.stat().st_uid == UID_A
    assert ws.stat().st_gid == os.getegid()
    assert stat.S_IMODE(ws.stat().st_mode) == 0o770
    assert (ws / "workspace").stat().st_gid == os.getegid()
    assert (ws / "workspace").stat().st_uid == UID_A
    assert (ws / "workspace" / "from-snapshot.txt").stat().st_uid == UID_A
