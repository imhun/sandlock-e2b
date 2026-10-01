"""SEC-K0S-006: `statfs(2)` inside a sandbox reports the host's accounting.

`df` and `shutil.disk_usage` go through `statfs`, which is not namespaced: the
sandbox used to be shown the node's whole volume (99.7 GiB on `/`, and a 10 PiB
NAS aggregate for `/workspace`). The platform knows the sandbox's quota (what
it was sold) and its current usage (it owns the tree and measures it), so the
worker publishes both and the fork reports them on each call.

This is the executor-side end of that: the path the platform hands the
executor must reach the fork, and the numbers must come back through `df`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SEC-K0S-006 residual -- ROOT CAUSE PROVEN: a route-B payload receives "
        "NO seccomp notifications at all, so no notif-based mediation can apply "
        "to it. Evidence (instrumented runs, diagnostics since removed): a "
        "per-(pid,nr) notification matrix from the supervisor's dispatch point "
        "shows notifications arriving for pids 60/62/65/68/80 (brk, openat, "
        "close, newfstatat, prlimit64 ...) while the payload -- which reports "
        "its own pid and runs CPython, so it must issue openat/close -- "
        "produced none, and `nr=137` never appeared anywhere. Corroborating: "
        "the confined child's plan does contain SYS_statfs "
        "(`CTX notif branch ... statfs=true`), the handler IS registered "
        "(`DISPATCH statfs handler registered`), the handler-entry log never "
        "fires, and a raw syscall(137) from the payload returns the host's "
        "numbers. So the payload is not running under that filter. Fix "
        "direction: spawn/re-confine route-B exec children under the "
        "generation's filter (the update needs its own acceptance, and it also "
        "decides whether uname/affinity/proc synthesis are inert in this shape "
        "for the same reason)."
    ),
)
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
