"""The unprivileged half of C3 Task 3's identity hand-off (ruling D9.1).

The old route-B starter needed a privileged step ("start this process as uid
X"), which is why a non-root worker carried ``e2b-slot-spawn``. On the C3 path
the worker performs **none** of it:

```
worker    forks the child C                       unprivileged
C         unshare(CLONE_NEWUSER)                  unprivileged
worker    reports {sandbox_id, pid(C)} to the CP  (this module's caller)
CP        validates, then instructs the agent with the uid from its records
agent     writes C's uid_map/gid_map              the only privileged step
C         polls setresuid(X) until it sticks, then execs supervise
```

This module is the child's own program: it is what ``W1SlotPool`` execs in place
of the old ``setpriv`` wrapper, and it is deliberately the smallest thing that
can do the child's half. It holds no privilege, reads no policy and knows no
identity: ``X`` arrives through ``--uid`` only because ``sandlock-supervise``
self-checks ``geteuid() == X`` -- the *grant* is the agent's write, and a child
that asks for a uid nobody granted simply keeps polling until it gives up.

Linux-only by nature (``unshare``/``setresuid``); the module is importable
everywhere so the host lane can drive :func:`child_argv`, and the container lane
runs the real thing.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Sequence

#: ``unshare(CLONE_NEWUSER)`` -- a new user namespace with an empty uid map.
CLONE_NEWUSER = 0x10000000

#: How long the child may wait for its identity before giving up. The pool's own
#: readiness wait is the outer bound; this one exists so a child whose grant
#: never arrives exits instead of polling forever.
DEFAULT_TIMEOUT_S = 30.0

#: How long between ``setresuid`` attempts. A grant is one write into
#: ``/proc/<pid>/uid_map``; the poll only has to outlast the round trip
#: (worker → CP → agent → kernel).
POLL_INTERVAL_S = 0.05


def timeout_s() -> float:
    """``E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S`` (seconds, default 30).

    Deliberately not the same knob as the worker→CP report deadline
    (``E2B_SLOT_IDENTITY_REPORT_TIMEOUT_S``): the child is waiting for the whole
    round trip (report → CP → agent → kernel), so its bound has to be the
    outer one. Two names keep a tightened report deadline from silently cutting
    the child's wait short.
    """
    raw = os.getenv("E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def child_argv(*, uid: int, supervise_argv: Sequence[str]) -> list[str]:
    """The argv the worker execs for the slot's child.

    Everything after ``--`` is ``sandlock-supervise``'s own argv, untouched: the
    control descriptor the pool passes (``--control-fd``) is inherited straight
    through, exactly as it is on the ``setpriv`` path.
    """
    return [
        sys.executable,
        "-m",
        "envd_service.slot_identity",
        "--uid",
        str(uid),
        "--",
        *[str(arg) for arg in supervise_argv],
    ]


def _unshare_user_namespace() -> None:
    unshare = getattr(os, "unshare", None)
    if unshare is None:  # pragma: no cover - non-Linux
        raise RuntimeError(
            "this platform has no os.unshare: the identity-grant slot path "
            "needs Linux user namespaces"
        )
    unshare(CLONE_NEWUSER)


def _await_identity(
    uid: int, *, deadline_s: float, interval_s: float = POLL_INTERVAL_S
) -> bool:
    """Poll ``setresuid(X)`` until the agent's mapping lands (or the deadline)."""
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            os.setresuid(uid, uid, uid)
        except OSError:
            # EINVAL while the identity is unmapped, EPERM while the namespace
            # has no mapping at all; both mean "not granted yet".
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval_s)
            continue
        return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="envd_service.slot_identity")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("slot_identity: nothing to exec after --", file=sys.stderr)
        return 2
    _unshare_user_namespace()
    deadline = timeout_s()
    if not _await_identity(args.uid, deadline_s=deadline):
        print(
            f"slot_identity: no identity for uid {args.uid} within {deadline}s "
            "(the agent never wrote the map)",
            file=sys.stderr,
        )
        return 1
    os.execv(command[0], command)
    return 1  # pragma: no cover - execv never returns


if __name__ == "__main__":  # pragma: no cover - exercised in the container lane
    sys.exit(main())
