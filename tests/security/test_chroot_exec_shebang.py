"""N35②: what the image-rootfs (chroot) shape can exec out of its own tree.

Two measurements of the same workflow ("a file lands in the sandbox, then the
sandbox runs it"), both on the production path -- pooled per-sandbox host uid
plus an ``E2B_ROUTE_B=auto`` slot, which is the only identity that mediations
run as (see ``tests/security/conftest.py::route_b_sandbox``):

* a **dynamic ELF binary** copied into the workspace runs. It only runs because
  the mediator handles the ``execve`` by opening the target itself, copying it
  into an *anonymous memfd* (patching PT_INTERP) and rewriting the caller's
  path to that fd -- anonymous inodes are the one exec target no Landlock rule
  has an opinion about. A *static* ELF in the workspace does NOT run (measured:
  EACCES, while the same binary inside the image rootfs runs), so this test is
  deliberately the dynamic case;
* a **shebang script** does not, with ``EACCES``. The kernel resolves the
  ``#!`` interpreter on its own, inside the same syscall and without a second
  seccomp notification, and that lookup is refused in this shape.

The second case is the *gap*, not the contract: it is pinned as a strict
``xfail`` so the suite records today's behavior, and flips to ``XPASS`` (a
failure, on purpose) the day it is fixed by N14's real root or by a shebang
branch in the mediator. Reasoning, evidence, the static-ELF half of the gap and
the A/B account are in ``docs/chroot-workspace-exec.md``; the probe that
measured it is ``tmp/k0s/probe_n35_exec_gate.py``.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    route_b_sandbox,
    run_sh,
)

IMAGE = "python:3.11-slim"

# Static ELFs available in the lane image (tini, shipped as docker-init). A
# static binary is the shape that proves the *workspace's own inode* is
# exec-allowed: a dynamic one goes through the mediator's memfd copy instead.
STATIC_CANDIDATES = ("/usr/sbin/docker-init", "/sbin/tini", "/bin/busybox")


def _elf_interpreter(path: Path) -> str | None:
    """PT_INTERP path, "" for a static ELF, None for a non-ELF."""
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    if len(blob) < 64 or not blob.startswith(b"\x7fELF") or blob[4] != 2:
        return None
    (e_phoff,) = struct.unpack_from("<Q", blob, 0x20)
    (e_phentsize,) = struct.unpack_from("<H", blob, 0x36)
    (e_phnum,) = struct.unpack_from("<H", blob, 0x38)
    for index in range(e_phnum):
        off = e_phoff + index * e_phentsize
        (p_type,) = struct.unpack_from("<I", blob, off)
        if p_type != 3:  # PT_INTERP
            continue
        (p_offset,) = struct.unpack_from("<Q", blob, off + 8)
        (p_filesz,) = struct.unpack_from("<Q", blob, off + 32)
        return blob[p_offset : p_offset + p_filesz].rstrip(b"\0").decode(errors="replace")
    return ""


def _static_elf() -> Path | None:
    for candidate in STATIC_CANDIDATES:
        path = Path(candidate)
        if path.exists() and _elf_interpreter(path) == "":
            return path
    return None


def _chroot_sandbox():
    rootfs = resolve_test_rootfs(IMAGE)
    executor, workspace = route_b_sandbox(IMAGE, rootfs)
    require_mediation_capable(executor)
    return executor, workspace


@pytest.mark.usefixtures("require_sandlock")
async def test_elf_binary_copied_into_the_workspace_runs():
    """The control half: a *dynamic* binary the sandbox wrote into its own tree.

    This is the shape that works, and it works through the mediator's memfd
    copy -- not because the workspace's own inode is exec-allowed. The static
    binary and the script are the halves that do not work; see
    ``docs/chroot-workspace-exec.md`` §1 for both (and for why this control has
    to stay dynamic to keep its meaning).
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
        "rewrite, and in the image-rootfs shape that lookup is refused -- "
        "EACCES once the file's own inode is exec-allowed and its write "
        "descriptor has been released, ETXTBSY while the mediator still holds "
        "that descriptor (same tick) -- docs/chroot-workspace-exec.md"
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


@pytest.mark.usefixtures("require_sandlock")
async def test_static_binary_in_the_workspace_runs():
    """The other half of the gap, and the one a fix could close first.

    A static ELF has no PT_INTERP, so the mediator cannot hand the kernel an
    anonymous copy: the exec reaches the workspace's own inode. That inode had
    no path rule until 2026-09-23 (`fs_writable` came in host-spelled and the
    chroot translation dropped it, so the mount source was never granted), and
    the refusal was EACCES. It is granted now, which is what this test pins.
    """
    source = _static_elf()
    if source is None:
        pytest.skip("no static ELF in the lane image to use as the fixture")
    executor, workspace = _chroot_sandbox()
    try:
        (Path(workspace) / "static_bin").write_bytes(source.read_bytes())
        (Path(workspace) / "static_bin").chmod(0o755)
        code, out, err = await run_sh(
            executor, workspace, "/workspace/static_bin --version"
        )
        assert code == 0, f"exit={code} stderr={err!r}"
        assert out.startswith(b"tini version"), out
        assert err == b""
    finally:
        executor.close()
