"""The unprivileged half of C3 Task 3's identity hand-off (ruling D9.1).

The retired slot starter needed a privileged step ("start this process as uid
X"), which is why a non-root worker carried ``e2b-slot-spawn``. On the C3 path
the worker performs **none** of it:

```
worker    clone3(CLONE_NEWUSER)                  unprivileged
worker    reports {sandbox_id, pid} to the CP    (this module's caller)
CP        validates, then instructs the agent with the uid from its records
agent     writes the child's uid_map/gid_map     the only privileged step
child     polls setresuid(X) until it sticks, then execs supervise
```

N80 (2026-10-06) turned this from "exec a helper that unshares" into
"``clone3``, then wait": the namespace exists before any of this module's code
runs, the worker knows that the moment ``clone3`` returns (so the handshake
byte is gone), and the child must still not exec until its identity lands --
``execve`` clears the fresh namespace's capabilities, and ``setresuid(X)``
needs them. All three arms are measured in
``deploy/scripts/acceptance/probe_slot_clone3_shape.py``.

The child holds no privilege, reads no policy and knows no identity: ``X``
arrives as an argument only because ``sandlock-supervise`` self-checks
``geteuid() == X`` -- the *grant* is the agent's write, and a child that asks
for a uid nobody granted simply keeps polling until it gives up.

Linux-only by nature (``clone3``/``setresuid``); the module is importable
everywhere so unit tests can drive :func:`spawn_child`'s parent half.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
import traceback
from typing import Sequence

from envd_service import env_alias

#: ``clone3`` on both x86_64 and aarch64 (the deployment's two architectures).
CLONE3_SYSCALL = 435


def _clone3_new_user_namespace() -> int:
    """``clone3(CLONE_NEWUSER, SIGCHLD)``: the child starts inside a fresh userns.

    Returns the child's pid to the caller and 0 *in* the child -- fork
    semantics, because no ``CLONE_VM`` is set, so a zero ``stack`` means "a
    copy of the caller's stack" and the child continues at the call site.

    Why not ``unshare``: it refuses a threaded caller, and this runs on the
    worker's asyncio threads. ``clone3`` puts the namespace on the *child*, so
    the calling thread is untouched. The child must not exec before its
    identity lands: ``execve`` clears the fresh namespace's capabilities (the
    uid is still unmapped, so the process has no valid identity there), and
    those capabilities are what ``setresuid(X)`` needs. Measured both ways in
    ``deploy/scripts/acceptance/probe_slot_clone3_shape.py``.
    """
    class _CloneArgs(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "flags",
                "pidfd",
                "child_tid",
                "parent_tid",
                "exit_signal",
                "stack",
                "stack_size",
                "tls",
                "set_tid",
                "set_tid_size",
                "cgroup",
            )
        ]

    args = _CloneArgs()
    args.flags = CLONE_NEWUSER
    args.exit_signal = signal.SIGCHLD
    # ``PyDLL``, not ``CDLL``: a plain ``CDLL`` call **releases the GIL** around
    # the syscall, and this call is a *fork*. If another thread holds the GIL at
    # the instant the clone lands, the child inherits that state and can never
    # take it back: it parks in ``pthread_cond_timedwait`` inside ctypes'
    # ``PyEval_RestoreThread`` before a single line of the child branch runs.
    #
    # Measured 2026-10-11 on this box, and it is the whole "the first sandbox in
    # a process takes 30/90/180 s (or hangs forever)" story: the child's own
    # faulthandler dump showed `identity_grant.py:86 in _clone3_new_user_namespace`
    # -> `_ctypes` -> `PyEval_RestoreThread` -> `pthread_cond_timedwait`, while
    # the parent's `SlotProcess.poll()` correctly said "alive" and the pool's
    # `stats` never got an answer. The other thread holding the GIL is whoever
    # happens to be running -- the image-cache pruner, an import, the event loop
    # -- which is why it hits *some* launches and not others, and why it is worst
    # right after startup (most threads busy).
    #
    # Holding the GIL across the clone makes the child start with the GIL owned
    # by its own (only) thread, which is the state ``os.fork()`` guarantees. The
    # runtime's *other* locks cannot be re-initialized here (``PyOS_AfterFork_Child``
    # aborts when the fork did not go through CPython), so the child keeps its
    # pre-exec work free of allocation and locks until it execs -- see
    # ``_fds_to_close`` and the child branch below.
    libc = ctypes.PyDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = [
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    ctypes.set_errno(0)
    pid = libc.syscall(CLONE3_SYSCALL, ctypes.byref(args), ctypes.sizeof(args))
    if pid < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return pid


def _child_says(message: str) -> None:
    """Write one line to the slot's stderr (fd 2, the pool's drained pipe).

    The child runs before ``exec`` with no logging of its own, and its exit
    status is all the worker sees otherwise -- the pool turns "the child died"
    into a named error that quotes this text (``_slot_stderr``), so a failure
    here is diagnosable from the worker log alone.
    """
    try:
        os.write(2, (message.rstrip() + "\n").encode("utf-8", "replace"))
    except OSError:  # pragma: no cover - stderr is a pipe we own
        pass


def _fds_to_close(keep: set[int]) -> list[int]:
    """The descriptors above stderr a child must not inherit.

    ``subprocess.Popen(close_fds=True)`` used to do this. A clone3 child
    inherits the worker's whole fd table -- asyncio wakeup pipes, the control
    plane's sockets, every other slot -- so skipping this hands the supervisor
    a table full of descriptors it must never hold.

    The list is computed **in the parent**, before the clone: enumerating
    ``/proc/self/fd`` allocates (a list of path names, one ``int`` per entry),
    and the child is a hand-rolled fork in a multi-threaded process -- it must
    not take a lock another thread held at clone time. See
    ``_clone3_new_user_namespace``'s note on the GIL.
    """
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:  # pragma: no cover - no /proc
        return []
    close: list[int] = []
    for name in names:
        if not name.isdigit():
            continue
        fd = int(name)
        if fd <= 2 or fd in keep:
            continue
        close.append(fd)
    return close


def _close_fds(fds: Sequence[int]) -> None:
    """Close ``fds`` (the list the parent computed, see :func:`_fds_to_close`)."""
    for fd in fds:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - already closed
            pass


def _keep_inheritable(fds: Sequence[int]) -> None:
    """Clear close-on-exec on the descriptors the slot is handed.

    ``subprocess.Popen(pass_fds=...)`` did this for the old starter, and the
    clone3 one replaced ``Popen`` without it. Python builds the slot's control
    and events channels with ``socket.socketpair()``, which is ``O_CLOEXEC``:
    a descriptor ``_fds_to_close`` deliberately kept would still be closed
    by the child's ``execve``, and ``sandlock-supervise`` would find
    ``--control-fd`` gone ("control fd N is not open: Bad file descriptor").
    clone3 copies the descriptor table but changes no flags, so the starter
    clears them itself -- in the child, whose table is its own copy.
    """
    for fd in fds:
        try:
            os.set_inheritable(fd, True)
        except OSError:
            # Already closed on the worker's side: the slot's own failure names
            # the descriptor it wanted, which beats a silent substitution.
            pass


class SlotProcess:
    """The slot's child, in place of the ``Popen`` this used to return.

    Only the surface ``own_identity`` uses: ``pid``, ``stderr``, ``poll()``,
    ``wait(timeout)`` and ``kill()``. ``wait`` raises
    :class:`subprocess.TimeoutExpired` exactly as ``Popen.wait`` does, so the
    shutdown path keeps its shape.
    """

    def __init__(self, pid: int, stderr) -> None:
        self.pid = pid
        self.stderr = stderr
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        try:
            reaped, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # Reaped by somebody else; a slot that is gone is a slot that ended.
            self._returncode = 0
            return self._returncode
        if reaped == 0:
            return None
        if os.WIFEXITED(status):
            self._returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self._returncode = -os.WTERMSIG(status)
        else:  # pragma: no cover - waitpid without WUNTRACED
            self._returncode = 1
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd=f"slot {self.pid}", timeout=timeout)
            time.sleep(0.05)

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

#: ``CLONE_NEWUSER`` -- the flag ``clone3`` carries so the child starts inside a
#: fresh user namespace (whose uid map is empty until the agent writes it).
CLONE_NEWUSER = 0x10000000

#: How long the child may wait for its identity before giving up. The pool's own
#: readiness wait is the outer bound; this one exists so a child whose grant
#: never arrives exits instead of polling forever.
DEFAULT_TIMEOUT_S = 30.0

#: How long between ``setresuid`` attempts. A grant is one write into
#: ``/proc/<pid>/uid_map``; the poll only has to outlast the round trip
#: (worker → CP → agent → kernel).
POLL_INTERVAL_S = 0.05

def identity_wait_timeout_s() -> float:
    """``E2B_IDENTITY_GRANT_WAIT_TIMEOUT_S`` (seconds, default 30).

    Deliberately not the same knob as the worker→CP report deadline
    (``E2B_IDENTITY_GRANT_REPORT_TIMEOUT_S``): the child is waiting for the whole
    round trip (report → CP → agent → kernel), so its bound has to be the
    outer one. Two names keep a tightened report deadline from silently cutting
    the child's wait short.

    Named apart from ``spawn_child``'s ``timeout_s`` argument on purpose: the
    argument shadows this function inside that body, and calling the argument
    (``None`` by default, and own identity never passes one) as if it were the helper
    killed every child with ``os._exit(4)`` before its first ``setresuid``.
    """
    raw = env_alias.read(
        "E2B_IDENTITY_GRANT_WAIT_TIMEOUT_S",
        legacy="E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S",
    )
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def _await_identity(
    uid: int, *, deadline_s: float, interval_s: float = POLL_INTERVAL_S
) -> bool:
    """Poll the identity until the agent's mapping lands (or the deadline).

    **Both halves of the identity, and the gid half is not decoration.** A
    slot's documents are ``owner=<worker>, group=X, mode 0440`` (``maint.c``'s
    ``--worker`` form: the owner stays the worker so it can rewrite them, the
    group moves to the slot), and ``sandlock-supervise`` reads ``policy.json``
    before it does anything else -- so a child that took only the uid half
    cannot start a slot at all: the read is ``EACCES``, the slot exits, and the
    create fails with "policy read failed" (measured on the compose multinode
    stack, 2026-09-29; the identity is set by ``as_uid``'s ``X X 1`` in
    *both* maps, so the gid is mapped and settable).

    The order is the one ``e2b-slot-spawn.c`` used (``setgid(X)`` then
    ``setuid(X)``), with one deliberate difference: **no** ``setgroups``. The
    agent's ``as_uid`` must write ``deny`` into ``/proc/<pid>/setgroups`` to be
    allowed to write the gid map at all, and the kernel refuses ``setgroups``
    for the rest of that namespace's life afterwards -- so the call would be an
    ``EPERM`` the child could not recover from. The supplementary groups it
    keeps are the worker's, which is what the old path started from too.

    ``setresgid`` and ``setresuid`` are attempted together so a mapping that
    lands one map at a time (``as_uid`` writes ``uid_map`` first) is retried as
    a unit rather than half-applied.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            os.setresgid(uid, uid, uid)
            os.setresuid(uid, uid, uid)
        except OSError:
            # EINVAL while the identity is unmapped, EPERM while the namespace
            # has no mapping at all; both mean "not granted yet".
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval_s)
            continue
        return True


def spawn_child(
    *,
    uid: int,
    supervise_argv: Sequence[str],
    stdout,
    stderr,
    env: dict[str, str] | None = None,
    pass_fds: Sequence[int] = (),
    timeout_s: float | None = None,
) -> SlotProcess:
    """Start the slot's child inside a user namespace of its own.

    N80 (2026-10-06): the child is created by ``clone3`` and does **not** exec
    until its identity lands. That replaces the old shape -- exec
    ``python -m envd_service.identity_grant``, which then unshared -- and, with
    it, the whole handshake byte: ``clone3`` returning *is* "the namespace
    exists and its map is still empty", so the worker reports the pid straight
    away instead of waiting for the child to say so (D11's race is gone
    because there is no window between the spawn and the namespace).

    The child's half, in order: wire the stdio it was handed, drop every
    descriptor ``Popen`` used to drop, poll for the granted identity, then exec
    ``sandlock-supervise``. The poll has to happen *before* the exec
    (``execve`` clears the namespace's capabilities, and ``setresuid(X)`` needs
    them) and *after* the clone (the namespace does not exist before it).
    """
    argv = [str(arg) for arg in supervise_argv]
    if not argv:
        raise ValueError("supervise_argv must not be empty")
    if stdout is not subprocess.DEVNULL and stdout is not None:
        raise NotImplementedError("the slot starter only knows stdout=DEVNULL")
    if stderr is not subprocess.PIPE:
        raise NotImplementedError("the slot starter only knows stderr=PIPE")

    stderr_reader, stderr_writer = os.pipe()
    # Computed here, not in the child: see _fds_to_close.
    close_fds = _fds_to_close({0, 1, 2, *pass_fds})
    try:
        pid = _clone3_new_user_namespace()
    except BaseException:
        os.close(stderr_reader)
        os.close(stderr_writer)
        raise

    if pid == 0:
        # === the child: inside the new namespace, capabilities intact ===
        try:
            # The child is a *hand-rolled* fork inside a multi-threaded
            # process: CPython's runtime knows nothing about it (see the GIL
            # note in ``_clone3_new_user_namespace``), so it must not allocate,
            # import or take any lock another thread might have held at clone
            # time. Everything below is either a plain syscall or a loop over
            # values the parent already computed.
            #
            # (``PyOS_AfterFork_Child`` would be the runtime's own remedy, but
            # it *aborts* when the fork was not taken through CPython -- SIGABRT
            # in a thread-spawned child, measured here -- so the path instead
            # holds the GIL across the clone, above, and keeps the child's work
            # allocation- and lock-free until exec.)
            if stdout is subprocess.DEVNULL:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.close(devnull)
            os.dup2(stderr_writer, 2)
            os.close(stderr_reader)
            os.close(stderr_writer)
            # A plain loop over ints, through a helper so a test can drive
            # this half without closing the *runner's* descriptors (see
            # tests/unit/test_identity_grant.py): the child does no allocation
            # and takes no lock between here and exec (see _fds_to_close).
            _close_fds(close_fds)
            _keep_inheritable(pass_fds)
            limit = (
                identity_wait_timeout_s()
                if timeout_s is None
                else float(timeout_s)
            )
            if not _await_identity(uid, deadline_s=limit):
                _child_says(
                    f"identity for uid {uid} never landed within {limit:.0f}s"
                )
                os._exit(3)
            os.execvpe(argv[0], argv, env if env is not None else os.environ)
        except BaseException:
            # Nothing of this is visible from the worker otherwise: the child's
            # exit says only "the slot is gone" (measured 2026-10-11, an empty
            # ``<empty slot stderr>`` on a launch that failed in 4 s). The
            # reason goes to the slot's stderr, which the pool keeps and prints
            # when the child dies.
            _child_says(traceback.format_exc())
            os._exit(4)
        os._exit(9)  # pragma: no cover - execvpe does not return

    os.close(stderr_writer)
    return SlotProcess(pid, os.fdopen(stderr_reader, "rb", buffering=0))
