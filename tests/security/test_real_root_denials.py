"""What a policy denial looks like *inside* the sandbox, under the real root.

`docs/chroot-workspace-exec.md` §9.7.8 recorded the measurement without pinning
it: `fs_denied` (`/proc/kcore`, `/sys`) and a read-only image rootfs must answer
the same way with the root built for real (`real_root`: mount namespace +
`pivot_root`) as they did with the mediator emulating it. The real root moves
those paths into the image tree, which could plausibly turn "denied by the
mediator" into "absent" or even "writable" -- so the four probes below are
measured, not asserted from the doc. (Both shapes were measured on 2026-09-24;
the emulated one is retired since N14 S5, and this file pins the surviving
answer.)

N95 re-measured it on 2026-10-11, because that change replaced the mediator's
verdict with **the kernel's own errno** on the resolve path ("the error is the
kernel's errno, not a verdict", `chroot/resolve.rs`): a path whose components do
not resolve in the tree now answers `ENOENT` instead of being reported as
`EACCES`, and a denial that lands on the *open* rather than on the lookup spells
itself differently. The four probes still answer refusals -- nothing below
became readable or writable -- but two of the spellings moved, so the doc's
table was corrected with them (see §9.7.8 in `docs/chroot-workspace-exec.md`).
That is the point of keeping them pinned here: the *claim* is "the real root
does not loosen a denial", but each answer is a measurement, so it has to move
when the layer that produces it does.
"""

from __future__ import annotations

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    own_identity_sandbox,
    run_sh,
)

IMAGE = "python:3.11-slim"


@pytest.mark.usefixtures("require_sandlock")
async def test_denied_paths_are_denied_under_the_real_root():
    """One sandbox, four probes: the kernel-side denials a policy promises."""
    rootfs = resolve_test_rootfs(IMAGE)
    executor, workspace = own_identity_sandbox(IMAGE, rootfs)
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

        # Measured identical in both shapes on 2026-09-24 (E2B_REAL_ROOT=0 and
        # =1), which is the whole claim: the real root neither loosens nor
        # rewrites a denial. Re-measured 2026-10-11 after N95 (the emulated
        # shape is gone, so the surviving answer is what is pinned).
        #   * kcore: the read is refused by the policy's `fs_denied` entry.
        #   * sys: `/sys` *is* a directory in the image rootfs, so the lookup
        #     succeeds and `ls` fails at the open -- "cannot open directory",
        #     i.e. EACCES from the `fs_denied` rule, still a refusal.
        #   * sys_kernel: absent from the image tree (the rootfs's `/sys` is
        #     empty), so the answer is the kernel's ENOENT. This is the entry
        #     N95 moved: the old mediator answered EACCES for the whole prefix
        #     before any lookup happened, which read as "denied by a rule"; now
        #     the answer says what the tree contains. Refusal either way --
        #     there was never anything to read.
        #   * proc_listing: `/proc` is the mediator's synthesized view, so it
        #     succeeds and lists nothing (no host pids, no host tree).
        #   * rootfs_write: the shared image rootfs stays read-only.
        assert measured == {
            "kcore": (1, b"", b"cat: /proc/kcore: Permission denied\n"),
            "sys": (2, b"", b"ls: cannot open directory '/sys': Permission denied\n"),
            "sys_kernel": (
                2,
                b"",
                b"ls: cannot access '/sys/kernel': No such file or directory\n",
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
