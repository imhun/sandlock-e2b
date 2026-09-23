"""What a policy denial looks like *inside* the sandbox, in both root shapes.

`docs/chroot-workspace-exec.md` §9.7.8 recorded the measurement without pinning
it: `fs_denied` (`/proc/kcore`, `/sys`) and a read-only image rootfs must answer
the same way whether the sandbox's root is emulated by the mediator or built for
real (`real_root`: mount namespace + `pivot_root`). The real root moves those
paths into the image tree, which could plausibly turn "denied by the mediator"
into "absent" or even "writable" -- so the shapes are compared here, not
asserted from the doc.

Run as-is for the default shape; `E2B_REAL_ROOT=1` (tests/security/conftest.py)
runs the same file with the real root on.
"""

from __future__ import annotations

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    route_b_sandbox,
    run_sh,
)

IMAGE = "python:3.11-slim"


@pytest.mark.usefixtures("require_sandlock")
async def test_denied_paths_answer_the_same_in_both_root_shapes():
    """One sandbox, four probes: the kernel-side denials a policy promises."""
    rootfs = resolve_test_rootfs(IMAGE)
    executor, workspace = route_b_sandbox(IMAGE, rootfs)
    try:
        require_mediation_capable(executor)
        measured = {}
        for label, script in (
            ("kcore", "cat /proc/kcore"),
            ("sys", "ls /sys"),
            ("sys_kernel", "ls /sys/kernel"),
            ("proc_listing", "ls /proc"),
            ("rootfs_write", "echo x > /usr/bin/n35-probe"),
        ):
            measured[label] = await run_sh(executor, workspace, script)

        # Measured identical in both shapes (E2B_REAL_ROOT=0 and =1), which is
        # the whole claim: the real root neither loosens nor rewrites a denial.
        #   * kcore: the read is refused by the policy's `fs_denied` entry.
        #   * sys / sys_kernel: EACCES on the lookup in *both* shapes. The real
        #     root does not turn this into "an empty directory in the image
        #     tree" -- the denial is a rule, not an artifact of what the tree
        #     happens to contain (measured; an earlier note in the doc said
        #     otherwise and is corrected there).
        #   * proc_listing: `/proc` is the mediator's synthesized view, so it
        #     succeeds and lists nothing (no host pids, no host tree).
        #   * rootfs_write: the shared image rootfs stays read-only.
        assert measured == {
            "kcore": (1, b"", b"cat: /proc/kcore: Permission denied\n"),
            "sys": (2, b"", b"ls: cannot access '/sys': Permission denied\n"),
            "sys_kernel": (
                2,
                b"",
                b"ls: cannot access '/sys/kernel': Permission denied\n",
            ),
            "proc_listing": (0, b"", b""),
            "rootfs_write": (
                2,
                b"",
                b"/bin/sh: 1: cannot create /usr/bin/n35-probe: Permission denied\n",
            ),
        }
        assert not (rootfs / "usr/bin/n35-probe").exists()
    finally:
        executor.close()
