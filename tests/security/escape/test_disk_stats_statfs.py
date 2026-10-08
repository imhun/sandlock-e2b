"""SEC-K0S-006: `statfs(2)` inside a sandbox reports the platform's accounting.

`df` and `shutil.disk_usage` go through `statfs`, which is not namespaced: the
sandbox used to be shown the node's whole volume (99.7 GiB on `/`, and a 10 PiB
NAS aggregate for `/workspace`). The platform knows the sandbox's quota (what
it was sold) and its current usage (it owns the tree and measures it), so the
worker publishes both and the fork reports them on each call.

This is the executor-side end of that: the path the platform hands the
executor must reach the fork, and the numbers must come back through `df`.

At first this case was `xfail(strict=True)` with the reading "an own-identity payload
receives no seccomp notifications at all, so no notif-based mediation can apply
to it" (SEC-K0S-007, 2026-10-01). That reading was wrong, and the 2026-10-01
re-measurement says what actually happened: the payload *is* notified in this
shape (`uname` hostname virtualization, `/proc` synthesis and
`inotify_add_watch` mediation all answer for it, and the fork's trace shows
`notif nr=137 pid=<payload>`), but the *handler chain* answered the `statfs`
with the node's numbers: `register_chroot_handlers` registered `SYS_statfs`
before the accounting handler, and a chain stops at the first non-`Continue`
result. Every deployment shape has a chroot root (pure/synth, image rootfs,
real root), so the accounting was unreachable in production while it worked in
the bare (no-chroot) fork test.

Fixed in `third_party/sandlock` by registering the accounting handler before
the chroot path handlers. Pinned by the fork's
`test_procfs::test_statfs_accounting_wins_over_the_chroot_handler` (red before
the fix with the node's numbers, green after) and by this case, which is the
own-identity end-to-end acceptance the audit named.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.usefixtures("require_sandlock")
def test_statfs_reports_the_published_accounting():
    from tests.security.conftest import own_identity_sandbox, run_sh, sandbox_tmpdir

    ws = Path(sandbox_tmpdir())
    # Beside the sandbox's tree, not in it -- the sandbox must not be able to
    # write the numbers it is shown.
    stats = ws / "disk-stats"
    # 10 GiB sold, 4 GiB used -> 6 GiB free, in 4 KiB blocks.
    stats.write_text("10737418240 4294967296\n")

    probe = (
        "import os\n"
        "s = os.statvfs('/')\n"
        "print(s.f_frsize, s.f_blocks, s.f_bfree, s.f_bavail)\n"
    )
    (ws / "statfs_probe.py").write_text(probe)

    executor, workspace = own_identity_sandbox(
        None, None, workspace=ws, disk_stats_path=str(stats)
    )
    code, out, err = asyncio.run(
        run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/statfs_probe.py")
    )
    assert code == 0, err
    assert out.decode().strip() == "4096 2621440 1572864 1572864"

    # The worker refreshes the file on its scan cadence; the next call must see
    # the new value without the sandbox being restarted.
    stats.write_text("10737418240 9663676416\n")
    code, out, err = asyncio.run(
        run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/statfs_probe.py")
    )
    assert code == 0, err
    assert out.decode().strip() == "4096 2621440 262144 262144"


@pytest.mark.usefixtures("require_sandlock")
def test_the_numbers_the_worker_publishes_are_what_the_sandbox_sees():
    """The production chain: worker publishes, *another uid* reads it.

    The path is only half of the wiring. The worker writes as its own uid and
    the own-identity slot -- the process that answers `statfs` -- runs at the
    sandbox's host uid, so the directory chain has to be traversable by name
    and the file readable. Measured 2026-10-01: with the runtime directory at
    its historical ``0700`` the slot's read failed with EACCES and every
    `statfs` silently answered with the node's volume again, even though the
    file existed and the handler was registered.
    """
    from envd_service.agent import _write_disk_stats
    from envd_service.config import Settings
    from gateway_common.paths import sandbox_disk_stats_path
    from tests.security.conftest import (
        make_sandbox_visible,
        own_identity_sandbox,
        run_sh,
        sandbox_tmpdir,
    )

    state = Path(sandbox_tmpdir(suffix="-state"))
    # pytest's own tmp trees are 0700 root-owned; the slot would be stopped by
    # those ancestors before it ever reached the modes under test, and the
    # production state base is a 0755 volume.
    make_sandbox_visible(state)
    settings = Settings(workspace_base=str(state))
    sandbox_id = "sbx_worker_published"
    # The production order matters: the registry creates `_runtime/<id>` first
    # (that is where its 0700 used to come from), and the scan round publishes
    # the accounting into it afterwards.
    from envd_service.runtime.registry import RuntimeRegistry

    registry = RuntimeRegistry(settings.workspace_base, state_base=settings.state_base)
    registry.register(
        sandbox_id=sandbox_id,
        access_token="token",
        workspace_dir=str(state / sandbox_id),
        disk_mb=10240,
    )
    _write_disk_stats(settings, sandbox_id, total_bytes=10 * 1024**3, used_bytes=4 * 1024**3)
    stats = sandbox_disk_stats_path(
        settings.workspace_base, sandbox_id, state_base=settings.state_base
    )
    # 10 GiB sold, 4 GiB used -> 6 GiB free, in 4 KiB blocks.
    assert stats.read_text() == f"{10 * 1024**3} {4 * 1024**3}\n"

    ws = Path(sandbox_tmpdir())
    probe = (
        "import os\n"
        "s = os.statvfs('/')\n"
        # Any handle the sandbox already holds will do -- the accounting is
        # path-independent, and /etc is not in a pure sandbox's read set.
        "fd = os.open('/workspace/statfs_probe.py', os.O_RDONLY)\n"
        "f = os.fstatvfs(fd)\n"
        "print(s.f_frsize, s.f_blocks, s.f_bfree, s.f_bavail)\n"
        "print(f.f_frsize, f.f_blocks, f.f_bfree, f.f_bavail)\n"
    )
    (ws / "statfs_probe.py").write_text(probe)
    executor, workspace = own_identity_sandbox(None, None, workspace=ws, disk_stats_path=str(stats))
    try:
        code, out, err = asyncio.run(
            run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/statfs_probe.py")
        )
        assert code == 0, err
        # Both spellings: `statvfs(path)` takes statfs(2), `fstatvfs(fd)` takes
        # the fd-based sibling, which is a different syscall (measured
        # 2026-10-01: without its own trap it answered the node's volume while
        # the path call reported the ledger).
        assert out.decode().strip() == (
            "4096 2621440 1572864 1572864\n4096 2621440 1572864 1572864"
        )

        # The next scan round publishes fresh numbers to the same path; the
        # sandbox must see them without being restarted.
        _write_disk_stats(settings, sandbox_id, total_bytes=10 * 1024**3, used_bytes=9 * 1024**3)
        code, out, err = asyncio.run(
            run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/statfs_probe.py")
        )
        assert code == 0, err
        assert out.decode().strip() == (
            "4096 2621440 262144 262144\n4096 2621440 262144 262144"
        )
    finally:
        executor.close()
