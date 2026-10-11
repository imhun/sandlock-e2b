"""Worker-side driver for ``test_c3_identity_grant_grant.py`` (C3 Task 3 / D11.2).

It is the *worker* of that lane: it starts the slot's child through the
**production** code and then waits, so the test can play the control plane and
the agent. Run inside a 65534 container with this repository mounted (the child
module is stdlib-only, so no worker image is needed):

    docker run --user 65534:65534 --cap-drop ALL --security-opt seccomp=unconfined \
        -e PYTHONPATH=/w -w /w --entrypoint python3 python:3.14-slim \
        /w/tests/contract/c3_slot_child_driver.py --uid X --slot-exec /w/.../slot-exec.py

Modes (the test picks one):

* ``production`` -- :func:`envd_service.own_identity._spawn_slot_child`, i.e. the
  worker's real starter: ``clone3(CLONE_NEWUSER)`` a child, hand it its the
  documents, and return the pid -- the child polls ``setresuid`` and then execs
  the program it was told to run. This is the arm that covers the starter end to
  end, including the ``stderr=PIPE`` contract the pool reads refusals from.
* ``plain`` -- a child that never unshares (a plain ``sleep``), for the
  counter-arm that pins face A's refusal against a pid still in the initial
  namespace. The production starter cannot produce one any more (see N80 below).

The pre-D11 ordering arms were ``delayed``/``no-wait``: they existed to order
the worker's report against the child's ``unshare`` byte. N80 (2026-10-06)
deleted that byte -- the child is created *inside* its namespace by ``clone3``,
so "the namespace exists" is true the moment the starter returns -- and the
driver refuses those modes by name rather than pretending to still test them.

It prints ``C3-DRIVER-READY container_pid=<pid> mode=<mode>`` once the pid the
control plane would be told about is known, and then stays up so the child's own
output (``C3-SLOT-EXEC-*``, printed by the program it ``execv``s) stays readable.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from envd_service.own_identity import _spawn_slot_child

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
    """The worker's real starter, unmodified.

    ``stderr=PIPE`` is the starter's own contract since N80 (the child's refusal
    is read from that pipe by the pool, and the starter refuses anything else by
    name); the old call passed ``None, None`` and would now die with
    ``NotImplementedError`` -- which the lane read as "the driver never reported
    ready" (measured 2026-10-10).

    ``stdout`` stays **inherited** (``None``): the program the child execs prints
    the ``C3-SLOT-EXEC-*`` line the lane reads out of this container's log, so
    sending it to ``DEVNULL`` (the pool's production choice) would make the arm
    unobservable -- measured: the driver reported ready, the grant landed, and
    the line the test waits for never appeared.
    """
    return _spawn_slot_child(
        Path(slot_exec),
        uid,
        Path("/tmp/c3-slot-policy.json"),
        Path("/tmp/c3-slot-program.json"),
        "rb-c3-slot-child-driver",
        "unused-channel-token",
        uid,
        None,
        subprocess.PIPE,
    )


def _relay_stderr(process) -> None:
    """Forward the child's stderr to ours, so a refusal lands in the log.

    ``spawn_child`` only knows ``stderr=PIPE`` and nothing in this driver would
    read it: a hidden refusal (or, past the 64 KiB pipe buffer, a blocked child)
    is exactly the shape the lane would report as "no C3-SLOT-EXEC-OK line".
    """

    def _pump() -> None:
        try:
            for chunk in iter(lambda: process.stderr.readline(), b""):
                sys.stderr.write(chunk.decode("utf-8", "replace"))
                sys.stderr.flush()
        except Exception:  # noqa: BLE001 - best effort, the lane reads stdout
            pass

    threading.Thread(target=_pump, daemon=True).start()


def _start_plain_child() -> subprocess.Popen:
    """A child that never unshares: the target of the counter-arm.

    Face A's refusal ("uid_map … is the initial namespace's full range") is
    about a pid that is *still* in the initial namespace, and the production
    starter can no longer produce one: N80's ``clone3`` creates the child
    directly inside its own user namespace, so every pid it returns has already
    unshared. A plain fork still has the old shape, and that is all the
    counter-arm needs -- it is testing face A, not the starter.
    """
    return subprocess.Popen(["/bin/sh", "-c", "sleep 600"], stdin=subprocess.DEVNULL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument(
        "--mode", choices=("production", "plain"), default="production"
    )
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--tmp-dir", default="/tmp")
    args = parser.parse_args(argv)
    slot_exec = _slot_exec(Path(args.tmp_dir))
    if args.mode == "production":
        process = _start_production(args.uid, slot_exec)
        _relay_stderr(process)
    elif args.mode == "plain":
        process = _start_plain_child()
    else:
        # ``--mode delayed`` / ``--mode no-wait`` were the pre-D11 ordering arms.
        # N80 removed the thing they ordered against: the child is created by
        # ``clone3(CLONE_NEWUSER)`` and never execs a module of its own, so there
        # is no handshake byte to be early or late for ("clone3 returning *is*
        # 'the namespace exists'"). Refused by name so a re-added arm cannot
        # silently test nothing.
        raise SystemExit(
            f"--mode {args.mode} was removed by N80: the handshake byte it "
            "ordered against no longer exists (clone3 creates the namespace "
            "before the starter returns)"
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
