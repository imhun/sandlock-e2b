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
import select
import subprocess
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

#: How long the worker waits for the child's "I have unshared" byte. Bounds the
#: handshake so a child that wedges before ``unshare`` fails the create by name
#: instead of hanging it (D11).
DEFAULT_UNSHARED_TIMEOUT_S = 10.0


class UnshareHandshakeError(RuntimeError):
    """The child never reported its user namespace: fail closed, by name."""


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


def unshared_timeout_s() -> float:
    """``E2B_SLOT_IDENTITY_UNSHARED_TIMEOUT_S`` (seconds, default 10)."""
    raw = os.getenv("E2B_SLOT_IDENTITY_UNSHARED_TIMEOUT_S")
    if not raw:
        return DEFAULT_UNSHARED_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_UNSHARED_TIMEOUT_S
    return value if value > 0 else DEFAULT_UNSHARED_TIMEOUT_S


def child_argv(
    *,
    uid: int,
    supervise_argv: Sequence[str],
    unshared_fd: int | None = None,
) -> list[str]:
    """The argv the worker execs for the slot's child.

    Everything after ``--`` is ``sandlock-supervise``'s own argv, untouched: the
    control descriptor the pool passes (``--control-fd``) is inherited straight
    through, exactly as it is on the ``setpriv`` path.

    ``unshared_fd`` is the write end of the handshake pipe (D11). The child
    writes one byte into it **after** its ``unshare`` succeeds and closes it
    before it starts polling, so the worker can tell "the grant may go now"
    apart from "the interpreter is up". Without it (a hand-run child) there is
    no handshake and the worker must not report a pid.
    """
    argv = [
        sys.executable,
        "-m",
        "envd_service.slot_identity",
        "--uid",
        str(uid),
    ]
    if unshared_fd is not None:
        argv += ["--unshared-fd", str(unshared_fd)]
    return argv + ["--", *[str(arg) for arg in supervise_argv]]


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


def signal_unshared(fd: int) -> None:
    """The child's half of the handshake: one byte, then close.

    Called *after* ``unshare`` succeeded and *before* the poll begins, so the
    byte means "the namespace exists and its map is still empty" -- the state
    ``as_uid`` writes into. The descriptor is closed here (the child holds the
    write end without ``CLOEXEC``, so leaving it open would hand it to
    ``sandlock-supervise``).
    """
    os.write(fd, b"x")
    os.close(fd)


def await_unshared(
    read_fd: int, *, pid: int, timeout_s: float | None = None
) -> None:
    """Wait (bounded) for the child's handshake byte.

    An empty read means the child died between ``fork`` and ``unshare``; no
    readable byte within the deadline means it is wedged. Both are named
    refusals: a grant issued on either would race the child's ``unshare`` and
    have ``as_uid`` refuse a namespace that "has not unshared".
    """
    limit = unshared_timeout_s() if timeout_s is None else float(timeout_s)
    ready, _, _ = select.select([read_fd], [], [], limit)
    if not ready:
        raise UnshareHandshakeError(
            f"the slot child (pid {pid}) did not report its user namespace "
            f"within {limit}s: refusing (the identity grant would race the "
            "unshare)"
        )
    if os.read(read_fd, 1) == b"":
        raise UnshareHandshakeError(
            f"the slot child (pid {pid}) exited before reporting its user "
            "namespace: refusing"
        )


def spawn_child(
    *,
    uid: int,
    supervise_argv: Sequence[str],
    stdout,
    stderr,
    env: dict[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    timeout_s: float | None = None,
) -> subprocess.Popen:
    """Start the slot's child and return only once it has unshared (D11).

    The handshake is the point: ``Popen`` returns as soon as the child has
    *exec'd*, and its ``unshare`` happens later, inside this module. A worker
    that reported the child's pid on the strength of the spawn alone would race
    the grant -- when the grant arrived first, ``as_uid`` would read the initial
    namespace's full-range map and refuse, failing the create intermittently.
    So the worker owns the read end of a pipe and waits for one byte before it
    reports anything (``envd_service.route_b`` reports after this returns).

    Failure is fail-closed and named, and the child is killed on the way out: a
    process that never unshared would otherwise poll until its own deadline.
    """
    read_fd, write_fd = os.pipe()
    argv = child_argv(uid=uid, supervise_argv=supervise_argv, unshared_fd=write_fd)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            # The handshake descriptor is deliberately *not* CLOEXEC in the
            # child (pass_fds clears the flag), so it survives the interpreter
            # start and reaches this module's `main`; the slot's own descriptors
            # travel the same way.
            pass_fds=(*tuple(pass_fds), write_fd),
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    # The parent must let go of the write end, or a dead child would look like
    # a live one (nothing would ever reach EOF on the read end).
    os.close(write_fd)
    try:
        await_unshared(read_fd, pid=process.pid, timeout_s=timeout_s)
    except BaseException:
        try:
            process.kill()
        except OSError:  # pragma: no cover - already gone
            pass
        raise
    finally:
        os.close(read_fd)
    return process


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="envd_service.slot_identity")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--unshared-fd", type=int, default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("slot_identity: nothing to exec after --", file=sys.stderr)
        return 2
    _unshare_user_namespace()
    if args.unshared_fd is not None:
        # "The namespace exists and its map is still empty" -- the worker may
        # now report this pid to the control plane.
        try:
            signal_unshared(args.unshared_fd)
        except OSError as exc:
            # The worker is gone (it closed its end): nobody will write this
            # child's identity, so dying here is the honest ending -- named,
            # rather than a traceback.
            print(
                "slot_identity: cannot report the user namespace to the "
                f"worker: {exc}",
                file=sys.stderr,
            )
            return 3
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
