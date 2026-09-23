"""N35②: what the image-rootfs (chroot) shape can exec out of its own tree.

Two measurements of the same workflow ("a file lands in the sandbox, then the
sandbox runs it"), both on the production path -- pooled per-sandbox host uid
plus an ``E2B_ROUTE_B=auto`` slot, which is the only identity that mediations
run as (see ``tests/security/conftest.py::route_b_sandbox``):

* an **ELF binary** copied into the workspace runs. The mediator handles the
  ``execve``, opens the target itself and rewrites the caller's path to an
  injected ``/proc/self/fd/N``, so no host path ever has to resolve;
* a **shebang script** does not, with ``EACCES``. The kernel resolves the
  ``#!`` interpreter on its own, inside the same syscall and without a second
  seccomp notification, and that lookup is refused in this shape.

The second case is the *gap*, not the contract: it is pinned as a strict
``xfail`` so the suite records today's behavior, and flips to ``XPASS`` (a
failure, on purpose) the day it is fixed by N14's real root or by a shebang
branch in the mediator. Reasoning, evidence and the three options are in
``docs/shebang-exec-in-chroot.md``; the probe that measured it is
``tmp/k0s/probe_n35_exec_gate.py``.
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


def _chroot_sandbox():
    rootfs = resolve_test_rootfs(IMAGE)
    executor, workspace = route_b_sandbox(IMAGE, rootfs)
    require_mediation_capable(executor)
    return executor, workspace


@pytest.mark.usefixtures("require_sandlock")
async def test_elf_binary_copied_into_the_workspace_runs():
    """The control half: a binary the sandbox just wrote into its own tree runs.

    It goes through the mediator's fd injection, so this also pins that the
    workspace is *not* missing an exec grant -- the refusal in the test below
    is about the interpreter lookup, not about the file's location.
    """
    executor, workspace = _chroot_sandbox()
    try:
        code, out, err = await run_sh(
            executor,
            workspace,
            "cp /bin/echo ./n35_bin && chmod +x ./n35_bin && ./n35_bin elf-hi",
        )
        assert (code, out.strip(), err) == (0, b"elf-hi", b"")
    finally:
        executor.close()


@pytest.mark.usefixtures("require_sandlock")
@pytest.mark.xfail(
    strict=True,
    reason=(
        "N35: the kernel resolves a #! interpreter outside the mediator's "
        "rewrite, and in the image-rootfs shape that lookup is refused with "
        "EACCES -- docs/shebang-exec-in-chroot.md"
    ),
)
async def test_shebang_script_written_into_the_workspace_runs():
    """The gap: `pip install --user`-shaped, and refused today."""
    executor, workspace = _chroot_sandbox()
    try:
        code, out, err = await run_sh(
            executor,
            workspace,
            "printf '#!/bin/sh\\necho script-hi\\n' > ./n35_script "
            "&& chmod +x ./n35_script && ./n35_script",
        )
        assert (code, out.strip(), err) == (0, b"script-hi", b"")
    finally:
        executor.close()
