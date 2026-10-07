#!/usr/bin/env python3
"""`clone3` vs `clone` inside a sandbox: which child the parent can reap (N86).

The `clone`-family reading in the N82 probe (`--op clone`) is `os.fork()` +
`waitpid()`, i.e. the **`clone`** syscall -- the half whose flags cBPF can read.
The half the notify table is actually kept for is `clone3` (`clone_args` sits
behind a user pointer cBPF cannot follow, so only `handle_fork` sees the
namespace bits). Measuring that half needs to know how a `clone3` child is
reaped, which is what this probe establishes.

Three arms, one sandbox, each printing the pid it got and how the reap went:

1. `clone3(flags=0, exit_signal=SIGCHLD)` -- the fork-shaped `clone3`.
2. the same via the legacy `clone(2)` (amd64 nr 56).
3. `wait4(..., __WCLONE)` on the `clone3` child.

**The reading (2026-10-07)**: the `clone3` child is a *clone child* -- plain
`wait4(flags=0)` answers `ECHILD` while `/proc/<pid>/status` shows the right
`PPid` and the child is alive; `wait4(..., __WCLONE=0x80000000)` reaps it. The
legacy `clone` child reaps with `flags=0`. Reproduced identically inside the
sandbox, inside the worker container, and inside a `--security-opt
seccomp=unconfined` container (struct bytes checked: offset 8 = 17), so it is a
property of the call shape, not of the sandbox, the notify filter or the worker
profile. `sandbox.rs::clone3_new_namespaces` carries the same warning for
`exit_signal = 0`; the product's leader path passes non-zero namespace flags and
is not affected.

Usage (any E2B endpoint):

    export E2B_API_URL=... E2B_SANDBOX_URL=... E2B_API_KEY=...
    python deploy/scripts/acceptance/probe_clone3_wait_shape.py
"""

from __future__ import annotations

import argparse
import os
import sys

INNER = r'''
import ctypes, os, sys, time


def out(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
LIBC.syscall.restype = ctypes.c_long
LIBC.wait4.restype = ctypes.c_long
LIBC.wait4.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

SYS_CLONE3 = 435          # x86_64 and aarch64
SYS_CLONE = 56            # x86_64 legacy clone; the lane this probe was read on
SIGCHLD = 17
WCLONE = -2147483648      # 0x80000000 as the signed int the kernel reads


class CloneArgs(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "flags", "exit_signal", "stack", "stack_size", "tls",
            "set_tid", "set_tid_size", "cgroup",
            "last_tid", "last_tid_size", "padding",
        )
    ]


def reap(pid):
    """How this child can be reaped: plain wait4, then __WCLONE."""
    ctypes.set_errno(0)
    plain = LIBC.wait4(pid, None, 0, None)
    if plain >= 0:
        return "wait4(flags=0) -> %d" % plain
    first = "wait4(flags=0) -> errno %d" % ctypes.get_errno()
    ctypes.set_errno(0)
    wclone = LIBC.wait4(pid, None, WCLONE, None)
    if wclone >= 0:
        return first + " | wait4(__WCLONE) -> %d" % wclone
    return first + " | wait4(__WCLONE) -> errno %d" % ctypes.get_errno()


out("parent pid=%d ppid=%d" % (os.getpid(), os.getppid()))

args = CloneArgs(flags=0, exit_signal=SIGCHLD)
ctypes.set_errno(0)
rc = LIBC.syscall(SYS_CLONE3, ctypes.byref(args), ctypes.c_size_t(64))
if rc == 0:
    out("  clone3 child pid=%d ppid=%d -> _exit(0)" % (os.getpid(), os.getppid()))
    os._exit(0)
out("clone3(flags=0, exit_signal=SIGCHLD) rc=%d errno=%d" % (rc, ctypes.get_errno()))
out("  " + reap(rc))

ctypes.set_errno(0)
rc2 = LIBC.syscall(
    SYS_CLONE, ctypes.c_ulonglong(SIGCHLD),
    ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_void_p(0),
)
if rc2 == 0:
    out("  clone child pid=%d ppid=%d -> _exit(0)" % (os.getpid(), os.getppid()))
    os._exit(0)
out("clone(SIGCHLD) rc=%d errno=%d" % (rc2, ctypes.get_errno()))
out("  " + reap(rc2))
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default="base")
    parser.add_argument("--timeout-s", default="180")
    args = parser.parse_args()

    from e2b import Sandbox

    api_url = os.environ["E2B_API_URL"]
    box = Sandbox.create(
        args.template,
        api_url=api_url,
        sandbox_url=os.environ.get("E2B_SANDBOX_URL", api_url),
        api_key=os.environ["E2B_API_KEY"],
        timeout=int(args.timeout_s),
        metadata={"probe": "clone3-wait-shape"},
    )
    try:
        out = box.commands.run(
            "cat > wait_shape.py <<'PYEOF'\n" + INNER + "PYEOF\npython3 wait_shape.py",
            timeout=120,
        )
        print(((out.stdout or "") + (out.stderr or "")).strip())
        return 0
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
