"""The real root's mounts stay inside the sandbox's own mount namespace.

Acceptance item ② of the N14/real-root work (`docs/chroot-workspace-exec.md`
§9.5): the sandbox builds its root with `mount` + `pivot_root`, and all of that
has to be invisible to the host -- no leaked mount, no leaked staging directory,
nothing that a second sandbox (or the worker itself) could see.

The check is the host's own mount table, so it fails loudly if the mounts ever
land in the wrong namespace (a missing `unshare(CLONE_NEWNS)`, a propagation
that is not private, a mount performed outside the sandbox). It is meaningful in
both shapes: the emulated root performs no mounts at all, and this pins that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    own_identity_sandbox,
    run_sh,
)

IMAGE = "python:3.11-slim"


def _mount_lines() -> set[str]:
    """Every mount the *worker* can see, as the kernel describes it."""
    return set(Path("/proc/self/mountinfo").read_text().splitlines())


@pytest.mark.usefixtures("require_sandlock")
async def test_creating_and_destroying_sandboxes_leaves_no_mounts_behind():
    before = _mount_lines()
    rootfs = resolve_test_rootfs(IMAGE)
    for _ in range(3):
        executor, workspace = own_identity_sandbox(IMAGE, rootfs)
        try:
            require_mediation_capable(executor)
            code, out, err = await run_sh(executor, workspace, "echo alive")
            assert (code, out.strip(), err) == (0, b"alive", b"")
        finally:
            executor.close()
    after = _mount_lines()
    leaked = sorted(after - before)
    assert not leaked, "the sandbox leaked mounts into the worker's namespace:\n" + "\n".join(
        leaked
    )
    assert after == before, "the worker's mount table changed while sandboxes ran"
