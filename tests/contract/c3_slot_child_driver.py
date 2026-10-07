"""Worker-side driver for ``test_c3_slot_identity_grant.py`` (C3 Task 3 / D11.2).

It is the *worker* of that lane: it starts the slot's child through the
**production** code and then waits, so the test can play the control plane and
the agent. Run inside a 65534 container with this repository mounted (the child
module is stdlib-only, so no worker image is needed):

    docker run --user 65534:65534 --cap-drop ALL --security-opt seccomp=unconfined \
        -e PYTHONPATH=/w -w /w --entrypoint python3 python:3.14-slim \
        /w/tests/contract/c3_slot_child_driver.py --uid X --slot-exec /w/.../slot-exec.py

Modes (the test picks one):

* ``production`` -- :func:`envd_service.own_identity._spawn_slot_identity`, i.e. the
  worker's real starter: it builds ``child_argv`` for
  ``python -m envd_service.slot_identity``, passes the handshake descriptor and
  **returns only after the child's ``unshare`` byte**. This is the arm that
  covers the child module end to end (argv parsing, handshake, poll loop, and
  the descriptor surviving the interpreter start).
* ``delayed`` -- the same child module behind a wrapper that sleeps first, with
  the handshake driven by the *production* ``await_unshared``. It makes the
  ordering measurable: a worker that reported on the spawn alone would be
  granting a namespace that does not exist yet.
* ``no-wait`` -- the *old* ordering, kept as the lane's counter-arm: it reports
  the pid immediately after ``Popen`` and is what the delayed arm would look
  like without the handshake (the grant is then refused, by name, by face A).

It prints ``C3-DRIVER-READY container_pid=<pid> mode=<mode>`` once the pid the
control plane would be told about is known, and then stays up so the child's own
output (``C3-SLOT-EXEC-*``, printed by the program it ``execv``s) stays readable.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from envd_service import slot_identity as si
from envd_service.own_identity import _spawn_slot_identity

#: A slot's line for the "it really got there" fact: the program the child
#: ``execv``s after its identity lands. Nothing else about it is production --
#: the child's half is over by the time this runs.
EXEC_PRELUDE = (
    "import os, sys, time\n"
    "print('C3-SLOT-EXEC-OK uid=%d pidns=%s' % "
    "(os.geteuid(), os.readlink('/proc/self/ns/pid')), flush=True)\n"
    "time.sleep(600)\n"
)


def _slot_exec(tmp_dir: Path) -> str:
    """The program the child execs instead of ``sandlock-supervise``."""
    path = tmp_dir / "c3-slot-exec.py"
    path.write_text("#!/usr/bin/env python3\n" + EXEC_PRELUDE, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _start_production(uid: int, slot_exec: str) -> subprocess.Popen:
    """The worker's real starter, unmodified."""
    return _spawn_slot_identity(
        Path(slot_exec),
        uid,
        Path("/tmp/c3-slot-policy.json"),
        Path("/tmp/c3-slot-program.json"),
        "rb-c3-slot-child-driver",
        "unused-channel-token",
        uid,
        None,
        None,
    )


def _start_timed(uid: int, slot_exec: str, *, delay: float, wait: bool):
    """The same child module, with the timing of its ``unshare`` controlled.

    ``delay`` is applied by a wrapper that sleeps *before* exec'ing the child
    module, so the handshake still comes from the production child; the
    handshake descriptor is inherited across both execs (that is what
    ``pass_fds`` is for). ``wait=False`` reproduces the pre-D11 ordering: report
    on the spawn, without waiting for the byte.
    """
    read_fd, write_fd = os.pipe()
    supervise = [slot_exec, "--policy", "/tmp/delayed", "--uid", str(uid)]
    argv = si.child_argv(uid=uid, supervise_argv=supervise, unshared_fd=write_fd)
    if delay > 0:
        argv = [
            "/bin/sh",
            "-c",
            f"sleep {delay}; exec " + " ".join(shlex.quote(part) for part in argv),
        ]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    if wait:
        try:
            si.await_unshared(read_fd, pid=process.pid)
        finally:
            os.close(read_fd)
    else:
        os.close(read_fd)
    return process


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument(
        "--mode", choices=("production", "delayed", "no-wait"), default="production"
    )
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--tmp-dir", default="/tmp")
    args = parser.parse_args(argv)
    slot_exec = _slot_exec(Path(args.tmp_dir))
    if args.mode == "production":
        process = _start_production(args.uid, slot_exec)
    else:
        process = _start_timed(
            args.uid,
            slot_exec,
            delay=args.delay,
            wait=args.mode == "delayed",
        )
    print(
        f"C3-DRIVER-READY container_pid={process.pid} mode={args.mode}",
        flush=True,
    )
    # Stay up: the child prints to this container's stdout, and the test reads
    # that output to see it get past setresuid.
    time.sleep(600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
