"""N35②: what the image-rootfs (chroot) shape can exec out of its own tree.

Two measurements of the same workflow ("a file lands in the sandbox, then the
sandbox runs it"), both on the production path -- pooled per-sandbox host uid
plus an ``E2B_OWN_IDENTITY=auto`` slot, which is the only identity that mediations
run as (see ``tests/security/conftest.py::route_b_sandbox``):

* a **dynamic ELF binary** copied into the workspace runs. It only runs because
  the mediator handles the ``execve`` by opening the target itself, copying it
  into an *anonymous memfd* (patching PT_INTERP) and rewriting the caller's
  path to that fd -- anonymous inodes are the one exec target no Landlock rule
  has an opinion about. A *static* ELF in the workspace does NOT run (measured:
  EACCES, while the same binary inside the image rootfs runs), so this test is
  deliberately the dynamic case;
* a **shebang script** runs, including one whose ``#!`` names an interpreter
  that was written in the same command. The kernel resolves the ``#!`` line
  itself, inside the same syscall and without a second seccomp notification --
  which is exactly what a real root makes work: the lookup happens in the
  sandbox's own tree, not in the host's path space.

The second bullet used to be the *gap*: under the emulated root it was refused
with ``EACCES``/``ETXTBSY``, and the cases were pinned as strict ``xfail``s. N14
S5 retired that root, so they assert the running behaviour unconditionally now.
Reasoning, evidence, the static-ELF half of the story and the A/B account are
in ``docs/chroot-workspace-exec.md``; the probe that measured it is
``deploy/scripts/acceptance/probe_n35_exec_gate.py``.
"""

from __future__ import annotations

import os
import struct
import subprocess
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
#
# Each entry is ``(path, workspace name, argv, banner)``. The extra fields are
# not decoration: "a static ELF" is one shape, but "what a static ELF prints"
# is not. The production image ships tini (whose version banner is a
# compile-time string, so it ignores argv[0]) while a plain guest may only have
# busybox -- and busybox dispatches on argv[0]: copied in as ``static_bin`` and
# asked for ``--version`` it answers ``static_bin: applet not found`` (exit
# 127), because it falls back to treating argv[1] as the applet name
# (measured on the aarch64 lane 2026-09-24). It has no ``--version`` either;
# ``--help`` is where its banner lives, on stdout. Giving each candidate the
# invocation it actually answers keeps this test about the workspace inode.
STATIC_CANDIDATES = (
    ("/usr/sbin/docker-init", "docker-init", ("--version",), b"tini version"),
    ("/sbin/tini", "tini", ("--version",), b"tini version"),
    ("/bin/busybox", "busybox", ("--help",), b"BusyBox v"),
)


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


def _static_elf() -> tuple[Path, str, tuple[str, ...], bytes] | None:
    """The first candidate that is a static ELF, with the way to run it."""
    for candidate, name, argv, banner in STATIC_CANDIDATES:
        path = Path(candidate)
        if path.exists() and _elf_interpreter(path) == "":
            return path, name, argv, banner
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
async def test_shebang_script_written_into_the_workspace_runs():
    """The `pip install --user` shape: write a script, then run it.

    Without a real root this is the gap N35 is about -- the kernel resolves the
    `#!` interpreter outside the mediator's rewrite, and the lookup is refused
    (EACCES once the file's own inode is exec-allowed and its write descriptor
    has been released, ETXTBSY while the mediator still holds that descriptor).
    With the real root (the fork's mount namespace + pivot_root, the only shape
    since N14 S5) the interpreter resolves inside the sandbox's own tree and the
    script simply runs -- which is what this test then asserts.
    """
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
async def test_script_whose_interpreter_was_written_in_the_same_command_runs():
    """The venv shape: a tool arrives, and a script is pointed at it.

    `uv venv`/`python -m venv` then install a console script whose `#!` names an
    interpreter *inside the workspace*, so both the exec target and its
    interpreter were written in this tick. The kernel resolves that interpreter
    itself, which means the mediator never sees the open and cannot release the
    write watch it holds on it -- unless it reads the `#!` line while handling
    the exec, which is what makes this work.
    """
    executor, workspace = _chroot_sandbox()
    try:
        # The interpreter has to be an absolute path: the kernel takes the `#!`
        # line verbatim, so a relative one is a non-starter.
        code, out, err = await run_sh(
            executor,
            workspace,
            "cp /bin/echo ./n35_interp && chmod +x ./n35_interp "
            "&& printf '#!%s/n35_interp\\nignored\\n' \"$(pwd)\" > ./n35_uses_interp "
            "&& chmod +x ./n35_uses_interp "
            "&& ./n35_uses_interp interp-marker; echo rc=$?",
        )
        assert code == 0, f"exit={code} stderr={err!r}"
        assert b"interp-marker" in out, out
    finally:
        executor.close()


#: A magic binfmt_misc registration of this test's own, so the case does not
#: depend on which handlers the host happens to advertise. The magic is a shell
#: comment on purpose: the interpreter (`/bin/sh`) reads the same file, and the
#: test wants its output to be the payload only.
BINFMT_NAME = "n35probe"
BINFMT_MAGIC = "#N35BINFMT"


def _register_binfmt_handler() -> bool:
    if os.geteuid() != 0:
        return False
    register = Path("/proc/sys/fs/binfmt_misc/register")
    if not register.exists():
        # The registry is a mount, not a permanent part of /proc: bring it up
        # when this process may, and skip when it may not.
        subprocess.run(
            ["mount", "-t", "binfmt_misc", "binfmt_misc", "/proc/sys/fs/binfmt_misc"],
            check=False,
            capture_output=True,
        )
    if not register.exists():
        return False
    entry = Path("/proc/sys/fs/binfmt_misc") / BINFMT_NAME
    if entry.exists():
        return True
    # `M` = magic at offset 0; the interpreter is an in-image path.
    register.write_text(f":{BINFMT_NAME}:M::{BINFMT_MAGIC}::/bin/sh:\n")
    return entry.exists()


@pytest.mark.usefixtures("require_sandlock")
async def test_a_format_handler_resolves_its_interpreter_inside_the_sandbox():
    """binfmt_misc is the other thing the kernel execs by itself.

    The registration belongs to the worker (it is the host's kernel registry),
    but the interpreter path it names is resolved *by the kernel*, in whatever
    root the exec'ing process has. With the real root (the only shape since N14
    S5) that is the image's own `/bin/sh`, so the payload runs. Under the
    emulated root the lookup landed in the host's path space, where the
    chroot-translated ruleset had no rule, and the exec was refused (measured:
    EACCES, rc 126 -- the same mechanism as the shebang case).
    """
    if not _register_binfmt_handler():
        pytest.skip("binfmt_misc needs root and a mounted binfmt_misc registry")
    executor, workspace = _chroot_sandbox()
    try:
        payload = Path(workspace) / "n35_binfmt_payload"
        payload.write_text(f"{BINFMT_MAGIC}\n/bin/echo binfmt-ran\n")
        payload.chmod(0o755)
        code, out, err = await run_sh(
            executor, workspace, "/workspace/n35_binfmt_payload"
        )
        assert code == 0, f"exit={code} stderr={err!r}"
        assert b"binfmt-ran" in out, out
        assert err == b"", err
    finally:
        executor.close()


@pytest.mark.usefixtures("require_sandlock")
async def test_the_workload_cannot_mount():
    """The seal that makes a real root safe to hand to the sandbox.

    The sandbox builds its own root (the only shape since N14 S5) with
    CAP_SYS_ADMIN *inside its own user namespace*, and gives that capability up
    before the workload starts. Whatever the container's own profile admits, the
    workload must not be able to mount, unshare a namespace or chroot -- three
    independent mechanisms say so: the capability is gone, the sandbox's own
    seccomp filter refuses those syscalls, and (for the kernel-resolved cases)
    the mount namespace is the sandbox's own.
    """
    executor, workspace = _chroot_sandbox()
    try:
        code, out, err = await run_sh(
            executor,
            workspace,
            "mkdir -p ./mnt; "
            "mount -t tmpfs none ./mnt 2>&1; echo mount_rc=$?; "
            "unshare -m true 2>&1; echo unshare_rc=$?; "
            "chroot / true 2>&1; echo chroot_rc=$?",
        )
        assert code == 0, f"exit={code} stderr={err!r}"
        text = out.decode(errors="replace")
        assert "mount_rc=0" not in text, text
        assert "unshare_rc=0" not in text, text
        assert "chroot_rc=0" not in text, text
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
    fixture = _static_elf()
    if fixture is None:
        pytest.skip("no static ELF in the lane image to use as the fixture")
    source, name, argv, banner = fixture
    executor, workspace = _chroot_sandbox()
    try:
        target = Path(workspace) / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)
        code, out, err = await run_sh(
            executor, workspace, f"/workspace/{name} {' '.join(argv)}"
        )
        assert code == 0, f"exit={code} stderr={err!r}"
        assert out.startswith(banner), out
        assert err == b""
    finally:
        executor.close()
