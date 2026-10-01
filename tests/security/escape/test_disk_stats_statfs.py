"""SEC-K0S-006: `statfs(2)` inside a sandbox reports the platform's accounting.

`df` and `shutil.disk_usage` go through `statfs`, which is not namespaced: the
sandbox used to be shown the node's whole volume (99.7 GiB on `/`, and a 10 PiB
NAS aggregate for `/workspace`). The platform knows the sandbox's quota (what
it was sold) and its current usage (it owns the tree and measures it), so the
worker publishes both and the fork reports them on each call.

This is the executor-side end of that: the path the platform hands the
executor must reach the fork, and the numbers must come back through `df`.

At first this case was `xfail(strict=True)` with the reading "a route-B payload
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
route-B end-to-end acceptance the audit named.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.usefixtures("require_sandlock")
def test_statfs_reports_the_published_accounting():
    from tests.security.conftest import route_b_sandbox, run_sh, sandbox_tmpdir

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

    executor, workspace = route_b_sandbox(
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
