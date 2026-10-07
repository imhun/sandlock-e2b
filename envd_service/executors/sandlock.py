"""Sandlock executor: Landlock + seccomp-bpf + seccomp user notification.

Requires Linux with Landlock ABI >= 6 and ``sandlock==0.9.0-beta``. Each
executor holds one lazily-created exec instance; every command execs onto it
(``start()`` -> ``instance.exec``) with per-exec cwd/env/clean_env and
optional pty stdio, while the policy ceiling (fs/chroot/network/limits) is
fixed at instance creation (M4 D1-D3). Non-Linux hosts import nothing and
``start()`` raises unimplemented.

The instance is either in-process (``sandlock.SandboxInstance``) or a
route-B ``sandlock-supervise`` slot running as the sandbox's own host uid,
where path mediation and DAC ownership are correct by construction
(``envd_service/own_identity.py``, backlog #5 / T5).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from gateway_common.errors import ConnectError, unimplemented
from gateway_common.network import NetworkUpdateConflictError
from gateway_common.paths import own_identity_instance_name
from envd_service.executors.base import ExecConfig, Executor, RunningProcess
from envd_service.process.stream_budget import (
    STREAM_LIMIT_DEFAULT,
    ByteBudgetQueue,
    ThreadHandoff,
    TRUNCATED_MARK,
)
from envd_service.process.stream_budget import item_bytes
from envd_service.uid_pool import (
    CAP_SETGID,
    CAP_SETUID,
    LEGACY_SHARED_UID,
    has_effective_cap,
)
from envd_service.own_identity import (
    OwnIdentityConfig,
    OwnIdentityInstance,
    SandboxError,
    SlotDeadError,
    supervise_policy_document,
    slot_pool_for,
)

logger = logging.getLogger(__name__)

# "The session is gone, rebuild once" is decided by **type**, never by
# substring-matching the message: since fork SL-12 a create/launch failure
# carries the core's own prose (a refusal's remedy, a confinement errno), so
# any text could contain "closed"/"dead" by accident (B1 review, minor-3).
#
# The map is module data so tests can substitute their own stand-in types.
# It holds only *typed* session-gone failures -- a served route-B refusal is
# never in here: it arrives as a coded exception
# (:func:`_refusal_reason`), because the same exception class also covers
# refusals that are emphatically *not* a gone session (a policy ceiling
# conflict).
_INSTANCE_GONE_REASONS: dict[type[BaseException], str] = {
    # Route B's slot-gone error is typed and needs no native package, so it is
    # registered regardless of whether `sandlock` imports (idle/restart of a
    # leased slot surfaces at exec time exactly like a dead instance).
    SlotDeadError: "dead",
}

try:  # sandlock is Linux-only; keep the import optional for macOS dev.
    import sandlock
    from sandlock import (
        ExecStdio,
        Sandbox as SandlockSandbox,
        SandboxInstance,
    )
    from sandlock.exceptions import InstanceClosedError, InstanceDeadError
except ModuleNotFoundError:  # not installed: the documented dev fallback
    sandlock = None  # type: ignore[assignment]
    ExecStdio = None  # type: ignore[assignment]
    SandboxInstance = None  # type: ignore[assignment]
    SandlockSandbox = None  # type: ignore[assignment]
except Exception as exc:  # installed but broken (B1 review): never silent
    raise RuntimeError(
        "the sandlock package is installed but unusable "
        f"({type(exc).__name__}: {exc}); refusing to run the sandlock executor "
        "without it. Reinstall the matching sandlock wheel (or rebuild "
        "libsandlock_ffi.so) and restage the worker image."
    ) from exc
else:
    # Typed session-gone failures from the native SDK (SL-12 fix round 1).
    _INSTANCE_GONE_REASONS[InstanceClosedError] = "closed"
    _INSTANCE_GONE_REASONS[InstanceDeadError] = "dead"


def _instance_gone_reason(exc: BaseException) -> str | None:
    """``"closed"`` / ``"dead"`` when ``exc`` is a typed session-gone error.

    ``None`` for every other failure, so the caller can log/propagate it
    unchanged instead of guessing from the message text.
    """
    for exc_type, reason in _INSTANCE_GONE_REASONS.items():
        if isinstance(exc, exc_type):
            return reason
    return None


#: The fork's stable refusal codes (``sandlock-core/src/error.rs``,
#: ``RefusalCode``; carried as ``code`` on a served ``ok:false`` answer and
#: surfaced by the wheel as ``sandlock.exceptions.SlotRefusal.code``) that
#: mean the generation can no longer take work, mapped to the same
#: ``"closed"``/``"dead"`` vocabulary the typed in-process errors use.
#:
#: A generation is a *container*: when its M0 main child exits, `sandlock-init`
#: collapses every group, closes the control channel and every later verb,
#: `exec` included, is refused with the unified closed-instance code
#: (``generation_closed``). ``generation_dead`` is a machinery failure
#: (listener/reaper/channel). The other two codes -- ``policy_denied`` and
#: ``verb_refused`` -- are a *Live* session refusing the verb for its own
#: reason (a ceiling conflict, an unknown child, a malformed payload): that
#: refusal is the caller's business and is never rebuilt.
#:
#: The missing-code shape (a wheel older than the field) is deliberately
#: **not** in here: an uncoded answer is treated exactly like an unknown code
#: -- propagate, no rebuild -- because guessing it from the prose is the
#: unsound reverse-inference this table replaced. In practice envd and
#: ``sandlock-supervise`` ship in the same wheel and cannot drift; the
#: upgrade rule is written down in ``third_party/sandlock/docs/CHANGELOG.md``.
_REFUSAL_GONE_REASONS: dict[str, str] = {
    "generation_closed": "closed",
    "generation_dead": "dead",
}


def _refusal_reason(exc: BaseException) -> str | None:
    """``"closed"`` / ``"dead"`` when ``exc`` is a *coded* route-B refusal.

    Route B's slot is a separate process, so its refusal cannot be a typed
    native error: the channel carries the anchor's prose plus the fork's
    stable ``code``, and the wheel exposes the pair as
    ``sandlock.exceptions.SlotRefusal``. This reads that code -- and **only**
    that code. ``None`` for a Live refusal (``policy_denied`` /
    ``verb_refused``), for an uncoded answer from an older wheel, and for
    every exception that carries no code at all; all of those propagate
    unchanged.
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        return None
    return _REFUSAL_GONE_REASONS.get(code)


# The six single-node /dev mounts of the fork ``sandlock.minimal_dev()``
# (third_party/sandlock/python/src/sandlock/sandbox.py, importable only on
# Linux). The mirror keeps the chroot policy shape unit-testable off-Linux,
# where the native module is absent and nothing is ever mounted.
_MINIMAL_DEV_MOUNTS = {
    "/dev/ptmx": "/dev/ptmx",
    "/dev/pts": "/dev/pts",
    "/dev/null": "/dev/null",
    "/dev/urandom": "/dev/urandom",
    "/dev/zero": "/dev/zero",
    "/dev/tty": "/dev/tty",
}

#: Exit codes ``sandlock-init`` reserves for a *setup* failure, i.e. the
#: workload never ran (``sandlock-core/src/init/mod.rs``): 125 is a failed
#: ``chdir`` or a refused stdio wiring, 126 a failed ``setpgid``, 127 a failed
#: ``execvp`` -- which is also what a mediated lookup the supervisor performs
#: turns into (a path it could not open, including the documented *retryable*
#: ``EAGAIN`` that ``openat2(RESOLVE_IN_ROOT)`` may return for a ``..``
#: symlink, e.g. the image's ``/lib64/ld-linux-x86-64.so.2``).
#:
#: A command that exits with one of these produced no output of its own, so
#: without a record on the failure path the only thing an operator sees is
#: "exit 127, empty stderr" -- the shape that made this family unreadable.
_EXEC_SETUP_FAILURE_CODES = (125, 126, 127)


#: `pivot_root(2)` by architecture. x86_64 kept its own syscall table (155);
#: every other 64-bit Linux arch this fork targets -- aarch64, riscv64,
#: loongarch64 -- uses the generic one (41).
#:
#: This is not trivia. The probe used to hardcode x86_64's 155, and on aarch64
#: 155 is `sched_getattr`: the call came back ESRCH ("No such process") and
#: every aarch64 node was told its seccomp profile did not admit `pivot_root`,
#: when nothing had ever asked the kernel for it -- so `E2B_REAL_ROOT` could
#: never be turned on for the architecture production runs. Measured
#: 2026-09-24 on the aarch64 lane: `syscall(155)` -> ESRCH,
#: `syscall(41)` -> EINVAL, which is what a real `pivot_root` answers when its
#: paths are not mount points. It travels into the child as a literal because
#: the probe is a *string* run by `python -c` (a name that only exists in this
#: module would be a NameError there, and a dead probe reports as a reason
#: rather than as a wrong number -- the same silent shape as the bug).
_PIVOT_ROOT_NR = {"x86_64": 155, "aarch64": 41, "riscv64": 41, "loongarch64": 41}

#: The capability probe's source, run by a child *process* (never ``os.fork()``:
#: the worker is multi-threaded -- ``asyncio.to_thread`` -- and a fork in a
#: threaded process can leave the child holding a lock another thread took, so
#: the check meant to prevent a wedged worker could itself wedge it; the child
#: process also gets a deadline for free).
#:
#: It walks exactly the steps the fork's real-root phase walks -- user
#: namespace, mount namespace, private, a recursive bind, then ``pivot_root``
#: and ``umount2(MNT_DETACH)`` -- so it fails where a sandbox would fail. The
#: ids are read *before* the unshare: inside a fresh user namespace with no map
#: yet the process reports the overflow id (65534), and mapping that is EPERM.
_REAL_ROOT_PROBE = r'''
import ctypes
import os
import platform
import struct
from pathlib import Path


class _Failed(Exception):
    pass


libc = ctypes.CDLL("libc.so.6", use_errno=True)
real_uid, real_gid = os.getuid(), os.getgid()
CLONE_NEWUSER, CLONE_NEWNS = 0x10000000, 0x00020000
SYS_CLONE3, SIGCHLD = 435, 17
MS_BIND, MS_REC, MS_PRIVATE = 4096, 16384, 1 << 18

PIVOT_ROOT_NR = __PIVOT_ROOT_NR__


def check(label, call):
    ctypes.set_errno(0)
    if call() != 0:
        raise _Failed(f"{label}: {os.strerror(ctypes.get_errno())}")


def in_the_new_namespaces():
    """Every step the fork's real-root phase walks, inside the namespaces."""
    # The maps have to exist before the namespace has capabilities the kernel
    # will honour on a mount.
    for path, text in (
        ("/proc/self/setgroups", "deny"),
        ("/proc/self/uid_map", f"0 {real_uid} 1\n"),
        ("/proc/self/gid_map", f"0 {real_gid} 1\n"),
    ):
        try:
            Path(path).write_text(text)
        except OSError as exc:
            if "uid_map" in path or "gid_map" in path:
                raise _Failed(f"write {path}: {exc.strerror}") from exc
            break
    check(
        "mount(NULL, /, MS_REC|MS_PRIVATE)",
        lambda: libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None),
    )
    scratch = Path("/tmp/.e2b-real-root-probe")
    scratch.mkdir(exist_ok=True)
    check(
        "mount --bind (the sandbox's own mounts need this)",
        lambda: libc.mount(
            str(scratch).encode(), str(scratch).encode(), None, MS_BIND | MS_REC, None
        ),
    )
    # The last two steps the fork takes: pivot into the root it just built,
    # then drop the old one. A profile that admits mount but not pivot_root
    # (the pre-N35 worker profile is exactly that) fails here, nowhere earlier.
    check("chdir", lambda: libc.chdir(str(scratch).encode()))
    arch = platform.machine()
    nr = PIVOT_ROOT_NR.get(arch)
    if nr is None:
        # Fail closed: a wrong number is how this went unnoticed for a whole
        # architecture, and it reports as a seccomp problem.
        raise _Failed(f"pivot_root: no syscall number known for {arch}")
    check(
        "pivot_root (the profile must admit it)",
        lambda: libc.syscall(nr, b".", b"."),
    )
    check("umount2(MNT_DETACH)", lambda: libc.umount2(b".", 2))


def fail(reason):
    print(reason, flush=True)
    os._exit(0)


# N80 (2026-10-06): the sandbox gets its user and mount namespaces from one
# clone3 call, so that is what this probes. Asking with unshare would answer a
# question the deployment no longer asks -- the engine does not call it, and
# the shipped profile no longer admits it.
args = struct.pack(
    "<11Q", CLONE_NEWUSER | CLONE_NEWNS, 0, 0, 0, SIGCHLD, 0, 0, 0, 0, 0, 0
)
buf = ctypes.create_string_buffer(args, len(args))
ctypes.set_errno(0)
pid = libc.syscall(SYS_CLONE3, ctypes.byref(buf), len(args))
if pid < 0:
    fail(f"clone3(CLONE_NEWUSER|CLONE_NEWNS): {os.strerror(ctypes.get_errno())}")
if pid == 0:
    try:
        in_the_new_namespaces()
    except _Failed as exc:
        fail(str(exc))
    except Exception as exc:  # a probe that raises is a probe that failed
        fail(f"{type(exc).__name__}: {exc}")
    print("ok", flush=True)
    os._exit(0)
_, status = os.waitpid(pid, 0)
if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
    print("clone3 child died before it could finish the root steps", flush=True)
'''.replace("__PIVOT_ROOT_NR__", repr(_PIVOT_ROOT_NR))

#: How long the child probe may take. Generous on purpose: this runs once per
#: process (``lru_cache``), and a cold node can be slow to fork+exec.
_REAL_ROOT_PROBE_TIMEOUT_S = 30


@functools.lru_cache(maxsize=1)
def _real_root_capability() -> str:
    """Can this worker build a sandbox root? Cached: it is a deployment property.

    Returns ``""`` when it can, otherwise the reason -- the errno text plus which
    step failed (:data:`_REAL_ROOT_PROBE`), so the caller can name the fix
    instead of failing every create with "instance is closed".

    Why this exists: the shape and the worker's seccomp profile have to travel
    together (the profile has to admit the mount family before a sandbox can
    pivot into a root of its own). Since N14 S5 the real root is the shape, so
    this is asked for every sandbox that has one. Without this check a node that
    has not been updated fails every sandbox create with "instance is closed"
    and no reason -- measured cost of finding that out the hard way is a whole
    debugging session.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _REAL_ROOT_PROBE],
            capture_output=True,
            text=True,
            timeout=_REAL_ROOT_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"the probe did not finish within {_REAL_ROOT_PROBE_TIMEOUT_S}s"
    except OSError as exc:
        return f"could not start the probe ({sys.executable}): {exc}"
    reason = (completed.stdout or "").strip()
    if reason == "ok":
        return ""
    if reason:
        return reason[:400]
    detail = [line for line in (completed.stderr or "").strip().splitlines() if line]
    if detail:
        return detail[-1][:400]
    return f"the probe exited {completed.returncode} without a reason"


def _mkdir_traversable(path: Path, mode: int = 0o755) -> None:
    """Create ``path`` -- and every missing parent -- at an explicit ``mode``.

    ``mkdir(mode=...)`` (with or without ``parents``) is masked by the ambient
    umask: under ``umask 077`` a request for ``0o755`` lands as ``0o700``. Every
    directory created here is a mount target or a root layer the sandbox's own
    uid traverses while owning none of them, so a ``0700`` one fails the bind
    (and the ``chdir``) with ``EACCES`` before the sandbox ever starts. Same
    call as ``own_identity._write_slot_documents``: create, then ``chmod`` each level
    explicitly, because neither the ambient umask nor a private scratch root
    may decide the modes. Directories that already exist are left untouched --
    this must never widen the modes of something an image shipped. A tree that
    is wholly the worker's own (the synthesized root) heals its leftovers
    instead: see ``_heal_traversable``.
    """
    if path.exists() or path.is_symlink():
        # `Path.mkdir(exist_ok=True)`'s contract: an existing directory is the
        # only acceptable occupant. A file (or a dangling symlink) in the way
        # has to fail here, loudly, instead of leaving the bind to explain it.
        if not path.is_dir():
            path.mkdir(mode=mode)
        return
    pending: list[Path] = []
    probe = path
    while not probe.exists():
        pending.append(probe)
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    for directory in reversed(pending):
        directory.mkdir(mode=mode)
        os.chmod(directory, mode)


def _heal_traversable(path: Path, stop: Path, mode: int = 0o755) -> None:
    """``_mkdir_traversable``, plus a ``chmod`` of every level it *owns*.

    ``_mkdir_traversable`` deliberately leaves an existing directory's mode
    alone: an **image** may have shipped it ``0700`` on purpose and widening a
    shipped mode is not this code's call (that is the early return
    ``_ensure_chroot_mount_points`` relies on). A **synthesized** root has no
    image behind it -- every level between ``stop`` and ``path`` was created by
    this worker, this run or an older one, so a ``0700`` there is a leftover
    under a hostile umask to heal, not a shipped decision. The split is
    categorical, not a heuristic, which is why the two behaviours live in two
    functions instead of one flag.

    ``stop`` is the boundary that keeps the heal inside the sandbox's own tree;
    it has to be ``path`` or one of its ancestors (fail closed otherwise) and is
    itself re-asserted, which is how the root and ``<pure_rootfs_dir>`` get
    their ``0755`` back. Nothing above ``stop`` is ever touched.
    """
    _mkdir_traversable(path, mode)
    if stop != path and stop not in path.parents:
        raise ValueError(
            f"refusing to heal {path}: {stop} is not an ancestor of it, so the "
            "chmod would leave the tree this worker owns"
        )
    current = path
    while True:
        os.chmod(current, mode)
        if current == stop:
            return
        current = current.parent


def _ensure_chroot_mount_points(
    rootfs: Path, mounts: dict[str, str], *, heal: bool = False
) -> None:
    """Create every mount *target* inside the rootfs.

    The emulating shape never needed them: the mediator resolved the virtual
    path to the host source and opened that. A real root (fork ``real_root``)
    binds each source *onto* its target, so the target has to exist -- including
    the single-node files of ``minimal_dev``, which slim base images extract
    without. Directories are created, missing file targets are touched (the bind
    replaces them with the device node or file the source is, so their own mode
    never reaches the sandbox); the fork refuses a mount whose target is missing
    rather than silently leaving a hole. Directories go through
    ``_mkdir_traversable`` -- the sandbox uid does not own them, and a umask
    decided ``0700`` fails the bind.

    ``heal=True`` is for the synthesized root, whose whole tree is the
    worker's own: targets that *already exist* get their mode re-asserted too
    (``_heal_traversable``), so a ``0700`` left by an older run heals. It stays
    False for an image rootfs, where an existing directory may have been shipped
    with a mode this code has no business widening.
    """
    for virtual, host in mounts.items():
        target = rootfs / str(virtual).removeprefix("/")
        if Path(str(host)).is_dir():
            if heal:
                _heal_traversable(target, rootfs)
            else:
                _mkdir_traversable(target)
        else:
            if heal:
                _heal_traversable(target.parent, rootfs)
            else:
                _mkdir_traversable(target.parent)
            if not target.exists():
                target.touch()


def _minimal_dev_mounts() -> dict[str, str]:
    """The chroot shape's ``fs_mount`` /dev set (minimal_dev, six nodes).

    With the native library present this delegates to the fork helper -- the
    host sources the mounts are taken from -- after a fail-closed pre-check
    that the host exposes a readable ``/dev/pts`` directory (the ``pts``
    mount binds the host devpts directory). There is deliberately no silent
    fallback to a whole-tree host ``/dev`` mount when devpts is missing.
    Off-Linux (policy-shape unit tests only) the module mirror is returned.
    """
    if sandlock is not None:
        pts = Path("/dev/pts")
        if not pts.is_dir() or not os.access(pts, os.R_OK):
            raise RuntimeError(
                "sandlock chroot shape requires a readable host /dev/pts "
                "directory (devpts) for the minimal_dev /dev/pts bind "
                "mount; refusing to fall back to a whole-tree /dev mount"
            )
        return sandlock.minimal_dev()
    return dict(_MINIMAL_DEV_MOUNTS)


#: The system directories a synthesized pure root (N16) binds from the host.
#: Filtered by host existence at build time: the fork *skips* a mount whose
#: source is gone but *fails* on a missing target, so a directory the host does
#: not have must never reach the map (it would leave an empty stub behind and
#: make `/opt` exist or not depending on the node). Measured table:
#: tmp/k0s/pure-synth-root-symlinks.log.
_SYNTHETIC_ROOTFS_SYSTEM_DIRS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
)

#: Everything the sandbox must see as a directory in its own root, whether or
#: not anything is bound onto it. The list is the shape, not a convenience:
#: `/proc` is only ever *listed* through it (its content is the mediator's
#: synthesis) and the rest is where today's pure shape answers `EACCES` for a
#: path outside the allow-list -- an empty directory answers the same way, a
#: missing one answers `ENOENT` and changes the contract.
_SYNTHETIC_ROOTFS_SKELETON_DIRS: tuple[str, ...] = (
    "proc",
    "dev",
    "etc",
    "tmp",
    "root",
    "run",
    "var",
    "srv",
    "media",
    "mnt",
    "home",
    "workspace",
)


def _synthetic_rootfs_mounts() -> dict[str, str]:
    """The bind mounts that fill a synthesized pure root.

    `/dev` is bound as the whole container tree, not as ``minimal_dev``'s six
    nodes: the pure shape's `/dev` has always *been* that tree, and the six
    nodes would silently drop `/dev/shm` plus seven more entries -- a tightening
    of the tenant's view this route must not do by accident. What the whole bind
    buys is **node-level** equivalence: the same 14 entries, with the four
    symlinks into ``/proc/self/fd`` (``fd``/``stdin``/``stdout``/``stderr``)
    preserved in *shape*. Whether those four resolve is the `/proc` synthesis /
    mediator's line of business, not this mount's -- and it is **not** a promise
    that bash process substitution works in a synthesized root (in this lane the
    four dangle; today's pure shape resolves them only because its `/proc` is a
    real procfs, while the skeleton's is an empty directory).
    Measured: tmp/k0s/pure-synth-root-dev-hosttree-guarded.log vs
    tmp/k0s/pure-synth-root-dev-minimal-guarded.log (this round's rerun: full
    listing, no `[:12]` truncation, `skeleton has /lib64: True` ahead of the exec
    lines). The "sets are equal" call is re-runnable as the probe's ``devdiff``
    part: tmp/k0s/pure-synth-root-devdiff-hosttree.log (rc 0 = equal, 14 = 14).
    """
    mounts = {
        directory: directory
        for directory in _SYNTHETIC_ROOTFS_SYSTEM_DIRS
        if Path(directory).is_dir()
    }
    mounts["/dev"] = "/dev"
    return mounts


def _materialize_synthetic_rootfs(root: Path, mounts: dict[str, str]) -> None:
    """Create the skeleton and every mount target a synthesized root needs.

    ``0755`` on purpose: the binds and the ``chdir`` into the root happen in the
    sandbox's own user namespace as the sandbox's own host uid, which owns none
    of these directories (a ``0700`` one fails the bind with EACCES before the
    sandbox ever starts). The mode is set by ``_mkdir_traversable``'s explicit
    ``chmod``, never by ``mkdir(mode=0o755)`` -- that argument is masked by the
    umask and lands as ``0700`` under ``umask 077``, which is the same failure.
    ``_ensure_chroot_mount_points`` then creates the targets the fork refuses to
    run without, by the same rule.

    Everything here is healed, not merely created: the synthesized root has no
    image behind it, so a ``0700`` left by an older run (or by an operator's own
    ``mkdir`` under a hostile umask) is a leftover of ours, and leaving it in
    place would fail every bind from then on -- what the root's old
    ``os.chmod`` promised while only ever fixing the root itself. The heal is
    bounded at ``<pure_rootfs_dir>`` and never walks above it.
    """
    # ``_mkdir_traversable`` walks the parents, so ``<pure_rootfs_dir>`` itself
    # is created 0755 too: the switch hands that layer out in Task 5 and the
    # sandbox uid has to be able to traverse into the sandbox's own directory.
    _mkdir_traversable(root)
    base = root.parent
    if base != Path(base.anchor):
        # ``<pure_rootfs_dir>``: created 0755 above when it was missing, and
        # *healed* when an older run -- or a ``mkdir`` the operator ran under
        # ``umask 077`` -- left it 0700, because the sandbox uid traverses this
        # layer into its own directory. Guarded against a filesystem root
        # (``E2B_PURE_ROOTFS=/``): a misconfigured switch must never make this
        # code chmod ``/``.
        _heal_traversable(base, base)
    _heal_traversable(root, root)
    for name in _SYNTHETIC_ROOTFS_SKELETON_DIRS:
        _heal_traversable(root / name, root)
    _heal_traversable(root / "home" / "user", root)
    _ensure_chroot_mount_points(root, mounts, heal=True)


class SandlockRunningProcess(RunningProcess):
    """Wraps a fork ``ExecProcess`` returned by ``SandboxInstance.exec``.

    PIPED mode streams ``proc.stdout``/``proc.stderr``; PTY mode streams the
    host-side pty master (``proc.pty``) and drives resizes straight through
    ``ExecProcess.resize`` -- the in-sandbox bridge and its in-band resize
    frames are gone (M4 D3). The child pid is cached at creation because
    ``ExecProcess.pid`` becomes ``None`` after ``wait()``.
    """

    def __init__(
        self,
        *,
        proc,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        stdin_queue: asyncio.Queue,
        pty_mode: bool = False,
        on_exit=None,
        on_setup_failure=None,
        signal_pause_supported: bool | None = None,
        handoff: ThreadHandoff | None = None,
    ) -> None:
        self._proc = proc
        self._queue = queue
        self._loop = loop
        self._stdin_queue = stdin_queue
        self._pty_mode = pty_mode
        self._pid = proc.pid if proc.pid is not None else -1
        self._on_exit = on_exit
        self._on_setup_failure = on_setup_failure
        self._reaped = False
        self._writer_thread: threading.Thread | None = None
        self._closed = False
        self._stdin_closed = False
        self._eof_count = 0
        # SEC-K0S-003: the reader thread's byte gate; closing it unblocks a
        # pump that is waiting for a consumer that will never come back.
        self._handoff = handoff
        if signal_pause_supported is not None:
            # Instance-level override of the class flag: a route-B child can
            # be signalled by number, an in-process one cannot.
            self.supports_signal_pause = signal_pause_supported

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def _input_stream(self):
        """Where stdin bytes go: PIPED -> proc.stdin, PTY -> proc.pty."""
        return self._proc.pty if self._pty_mode else self._proc.stdin

    def output(self) -> AsyncIterator[tuple[str, bytes]]:
        return self._consume()

    async def _consume(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            # Internal stream-termination markers are not output; the local
            # executor filters them the same way before yielding.
            if item[0] == "__eof__":
                continue
            yield item

    def _start_stdin_writer(self) -> None:
        stream = self._input_stream
        if self._writer_thread is not None or stream is None:
            return

        def _write_loop() -> None:
            try:
                while True:
                    data = asyncio.run_coroutine_threadsafe(
                        self._stdin_queue.get(), self._loop
                    ).result()
                    if data is None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                        return
                    try:
                        stream.write(data)
                        stream.flush()
                        logger.debug(
                            "sandlock stdin wrote %d bytes (fd=%s)",
                            len(data),
                            getattr(stream, "fileno", lambda: None)(),
                        )
                    except Exception as e:  # noqa: BLE001 - keep the loop alive
                        logger.warning("sandlock stdin write failed: %r", e)
                        return
            except Exception:  # pragma: no cover - defensive
                logger.exception("sandlock stdin writer failed")

        self._writer_thread = threading.Thread(target=_write_loop, daemon=True)
        self._writer_thread.start()

    def send_stdin(self, data: bytes) -> None:
        if self._closed or self._stdin_closed:
            return
        self._start_stdin_writer()
        try:
            self._stdin_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("sandlock stdin queue full; dropping %d bytes", len(data))

    async def feed_stdin(self, data: bytes) -> None:
        """``send_stdin`` that waits for queue room instead of dropping (N28).

        The interactive path above trades bytes for latency on purpose
        (a keystroke that cannot be queued is not worth stalling the event
        loop for). A streamed upload has the opposite requirement: every byte
        must arrive, and the producer can afford to wait. ``await put`` on the
        same bounded queue gives exactly that backpressure -- the queue holds
        at most 256 chunks, so a fast reader of a 512 MiB body never has more
        than that in flight.
        """
        if self._closed or self._stdin_closed:
            raise RuntimeError("the child's stdin is already closed")
        if self._input_stream is None:
            raise RuntimeError("the child has no stdin stream to write to")
        self._start_stdin_writer()
        await self._stdin_queue.put(data)

    def close_stdin(self) -> None:
        if self._pty_mode:
            # A pty has no independent EOF: closing the master write side
            # would hang up the terminal. Just stop accepting input; the
            # master is closed by wait() once the process exits.
            if self._stdin_closed:
                return
            self._stdin_closed = True
            return
        if self._closed:
            return
        self._closed = True
        # Flush any queued writes before closing the pipe (the writer thread
        # consumes the None sentinel and closes proc.stdin).
        self._start_stdin_writer()
        try:
            self._stdin_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def resize(self, rows: int, cols: int) -> None:
        if not self._pty_mode:
            return
        try:
            self._proc.resize(rows, cols)
        except (OSError, RuntimeError) as e:
            logger.warning("sandlock pty resize failed: %r", e)

    # M4 D5 / FUP #8: ``kill(sig)`` always SIGKILLs (see below); a
    # pause/resume fallback must never use it to deliver SIGSTOP/SIGCONT.
    supports_signal_pause = False

    def kill(self, sig: int) -> None:
        if self._handoff is not None:
            # A pump thread blocked on the byte gate must not outlive the
            # command it was reading (SEC-K0S-003).
            self._handoff.close()
        # In-process: the fork registry delivers SIGKILL to the child's whole
        # command subtree regardless of the requested signal, so
        # ``supports_signal_pause`` stays False and ProcessManager's
        # pause/resume fallback skips such a child with a WARNING instead of
        # turning a pause into a kill (FUP #8).
        #
        # Route B: ``kill_child`` carries the signal number through the slot's
        # registered pidfd, so the requested signal really arrives and
        # pause/resume may use it.
        try:
            if self.supports_signal_pause:
                self._proc.kill(sig)
            else:
                self._proc.kill()
        except Exception:
            pass

    def _mark_eof(self) -> None:
        """Signal the end of the output stream.

        PIPED mode needs both stdout and stderr EOF; PTY mode has a single
        stream (the master), so its EOF ends the output. By then the process
        has exited, so ProcessManager's ``exit_code()`` -> ``wait()`` can
        reap it without closing a still-open stdin first.
        """
        self._eof_count += 1
        target = 1 if self._pty_mode else 2
        if self._eof_count >= target:
            try:
                # SEC-K0S-003: the end-of-stream sentinel is a control item --
                # it must arrive even when the queue is at its budget, or the
                # consumer waits forever on a command that already ended.
                self._queue.put_control(None)
            except asyncio.QueueFull:  # pragma: no cover - item bound only
                pass

    async def exit_code(self) -> int:
        result = await asyncio.to_thread(self._proc.wait)
        if self._on_exit is not None and not self._reaped:
            self._reaped = True
            self._on_exit(self._proc.child_id, self._pid)
        # A reserved setup/exec code means the workload never ran, and the
        # child's own output is empty by construction: record the sandbox view
        # here, or the command is indistinguishable from a silent crash.
        if (
            self._on_setup_failure is not None
            and result.exit_code in _EXEC_SETUP_FAILURE_CODES
        ):
            self._on_setup_failure(result.exit_code)
        return result.exit_code


class SandlockExecutor(Executor):
    """Holds one lazily-created exec instance per sandbox.

    M4 D1-D3: the instance is created on first ``_ensure_instance()`` with a
    stable ``sandbox_id``-derived name and the command-independent policy
    ceiling from ``_policy_ceiling()`` (rebuilt exactly once after a
    closed/dead launch), and released by ``close()``. Every ``start()``
    execs onto that instance with per-exec cwd/env/clean_env/bind_ports and
    PIPED/PTY stdio. On non-Linux hosts sandlock is unavailable and the
    instance stays ``None`` (D11).

    The instance has two interchangeable backends, chosen once per sandbox by
    :meth:`_own_identity_decline_reason`: the **in-process** ``sandlock.SandboxInstance``
    (the mediator is the worker process), or a **route-B** ``sandlock-supervise``
    slot leased from :mod:`envd_service.own_identity` (the mediator is a process
    whose euid *is* this sandbox's host uid, so mediated path operations land
    with the sandbox's own ownership). Route B speaks the same
    ``exec``/``wait_child``/``kill_child``/``update_network``/``shutdown`` verb
    surface through :class:`~envd_service.own_identity.OwnIdentityInstance`, which
    mirrors ``SandboxInstance`` -- everything below is one code path.
    """

    _non_root_fallback_warned = False

    def __init__(
        self,
        *,
        workspace_dir: str,
        base_image: str | None,
        image_rootfs: Path | None,
        host_uid: int | None = None,
        per_sandbox_uid: bool = False,
        memory_mb: int,
        cpu_percent: int,
        disk_mb: int,
        disk_stats_path: str | None = None,
        max_file_size_mb: int | None = None,
        max_processes: int,
        max_open_files: int,
        allow_internet_access: bool,
        enable_network: bool,
        enable_netns: bool = False,
        enable_net_isolation: bool = False,
        fd_inject_connect: bool = False,
        bind_inject: bool = False,
        pid_ns: bool = False,
        port_mappings: dict | None = None,
        network: dict | None = None,
        network_deny_cidrs: tuple[str, ...] = (),
        notify_rate_limit: int = 0,
        iam_tokens: dict[str, dict[str, str]] | None = None,
        stream_limit_bytes: int | None = STREAM_LIMIT_DEFAULT,
        iam_signing_key: str | None = None,
        secrets_dir: str | Path | None = None,
        extra_fs_writable: list[str] | None = None,
        fs_mounts: dict[str, str] | None = None,
        sandbox_id: str | None = None,
        pure_rootfs_dir: Path | str | None = None,
        own_identity: OwnIdentityConfig | None = None,
    ) -> None:
        self._workspace_dir = workspace_dir
        self._base_image = base_image
        self._image_rootfs = image_rootfs
        self._host_uid = host_uid
        self._per_sandbox_uid = per_sandbox_uid
        self._memory_mb = memory_mb
        self._cpu_percent = cpu_percent
        self._disk_mb = disk_mb
        #: SEC-K0S-006 -- where the worker publishes this sandbox's disk
        #: accounting for `statfs(2)`. Unset leaves `statfs` reporting the host.
        self._disk_stats_path = disk_stats_path
        self._max_file_size_mb = max_file_size_mb
        self._max_processes = max_processes
        # SEC-K0S-003: the byte budget of the per-command output queue below
        # (``None`` or non-positive = unlimited, repo convention).
        self._stream_limit_bytes = (
            None
            if stream_limit_bytes is None or int(stream_limit_bytes) <= 0
            else int(stream_limit_bytes)
        )
        self._max_open_files = max_open_files
        self._allow_internet_access = allow_internet_access
        self._enable_network = enable_network
        self._enable_net_isolation = enable_net_isolation
        self._fd_inject_connect = fd_inject_connect
        self._bind_inject = bool(bind_inject)
        self._pid_ns = bool(pid_ns)
        self._port_mappings = {
            int(host): int(sandbox) for host, sandbox in (port_mappings or {}).items()
        }
        if self._port_mappings and not enable_net_isolation:
            raise ValueError(
                "port_mappings require net isolation "
                "(E2B_ENABLE_NET_ISOLATION=true): host ports in the 50005+ "
                "range map onto the sandbox's own netns listeners"
            )
        self._network_deny_cidrs = tuple(network_deny_cidrs)
        self._notify_rate_limit = notify_rate_limit
        # Accepted for config compatibility (E2B_ENABLE_NETNS), but the fork
        # dropped per-sandbox netns/veth in favor of the unprivileged
        # loopback-netns mode: the real switch is `enable_net_isolation`
        # (E2B_ENABLE_NET_ISOLATION -> sandlock `net_isolation`), so this
        # legacy flag is a no-op.
        self._enable_netns = enable_netns
        self._network = dict(network) if network else None
        self._iam_tokens = dict(iam_tokens or {})
        self._iam_signing_key = iam_signing_key or "e2b-sandlock-local-iam-key"
        self._secrets_dir = Path(secrets_dir) if secrets_dir else None
        self._extra_fs_writable = list(extra_fs_writable or [])
        self._fs_mounts = dict(fs_mounts or {})
        self._sandbox_id = sandbox_id
        self._pure_rootfs_dir = Path(pure_rootfs_dir) if pure_rootfs_dir else None
        if self._has_sandbox_root:
            # N14 S5: the real root is the shape, so this probe is no longer
            # behind a flag -- but it still has to travel with the worker's
            # seccomp profile. Probe once per process and fail with the
            # operator's next action instead of failing every create with
            # "instance is closed".
            reason = _real_root_capability()
            if reason:
                raise RuntimeError(
                    "this worker cannot build a sandbox root: "
                    f"{reason}. Apply deploy/seccomp/sandlock-worker.json "
                    "(it admits the mount-family syscalls the sandbox's own user "
                    "namespace needs) to every node."
                )
        # Route B (one ``sandlock-supervise`` per sandbox, euid == the
        # sandbox's host uid). ``None`` / ``off`` keeps the in-process
        # instance; the decision itself is made once here because every input
        # (shape, uid, platform, starter privilege) is fixed at construction.
        self._own_identity = own_identity
        self._own_identity_decline = self._own_identity_decline_reason()
        self._own_identity_active = self._own_identity_decline is None
        if not self._own_identity_active:
            self._refuse_in_process_without_a_quota(self._own_identity_decline)
        # N25: consumer of the slot's pushed append events, installed by the
        # runtime context (`SandboxRuntimeContext`) right after construction.
        # ``None`` means the numbers keep coming from the filesystem alone.
        self._append_sink = None
        self._disclose_mediation_shape()
        self._instance = None
        self._instance_name: str | None = None
        # Set by ``close()`` (the single shutdown point). Guards the
        # closed/dead rebuild-once paths: after an explicit close/shutdown a
        # fresh instance must never be leaked, so ``start()`` fails loudly
        # instead of rebuilding.
        self._closed = False
        # Serializes the instance lifecycle (creation/rebuild in
        # ``_ensure_instance``, teardown in ``close``) against
        # ``update_network``'s validate+apply section: a command exec racing
        # a network update must never interleave preflight and application
        # with instance creation (M4 D4 review Important-1).
        self._lifecycle_lock = threading.Lock()
        # F4.3/S2 staleness mapping: fork child id -> (host pid, resolved
        # argv). Registered in ``start()`` before the process is returned.
        self._child_registry: dict[int, tuple[int, list[str]]] = {}
        self._mcp_bind_port: int | None = None
        # SSL_CERT_FILE/CURL_CA_BUNDLE overrides merged into every per-exec
        # env when the chroot HTTPS-MITM CA branch is active (the policy
        # ceiling itself no longer carries env).
        self._http_inject_env: dict[str, str] = {}

    def _merged_state(self, network: dict | None) -> dict:
        """Canonical D4=A state for ``network`` folded with this executor's
        record mirrors (``allowInternetAccess``; ``allowPublicTraffic`` is
        carried by the network dict itself)."""
        from gateway_common.network import merged_network_state

        allow_internet = (network or {}).get("allowInternetAccess")
        if allow_internet is None:
            allow_internet = self._allow_internet_access
        return merged_network_state(
            network, allow_internet_access=bool(allow_internet)
        )

    def _applied_state(self) -> dict:
        """Canonical state currently applied to the instance (or, before the
        first launch, the static policy the future instance would build)."""
        from gateway_common.network import merged_network_state

        return merged_network_state(
            self._network, allow_internet_access=self._allow_internet_access
        )

    def validate_update(self, network: dict | None) -> None:
        """Raise :class:`NetworkUpdateConflictError` when ``network`` cannot
        be applied to an already-launched instance.

        D4=A: with no instance yet every normalized update is applicable (it
        becomes the static policy the future instance is built with). Once
        launched, only monotone narrowings the fork verb can represent are
        accepted; everything else must be rejected with HTTP 409 before any
        record is persisted. This method is pure -- it never mutates the
        executor or calls the instance.
        """
        if self._instance is None:
            return
        from gateway_common.network import network_update_conflict_reason

        reason = network_update_conflict_reason(
            self._applied_state(), self._merged_state(network)
        )
        if reason is not None:
            raise NetworkUpdateConflictError(reason)

    def update_network(self, network: dict | None) -> None:
        """Replace the network policy for new execs (M4 D4, S2 semantics).

        With no live instance the update simply becomes the static policy of
        the future instance. On a launched instance the update is validated
        first (raising :class:`NetworkUpdateConflictError` without mutating
        anything when it is not expressible); expressible narrowings call
        ``instance.update_network(ip_set)`` with the IP-literal allow set of
        the proposed ``allowOut`` and log the fork's stale-children report.
        The executor's own policy copy is only replaced after a successful
        apply, so a rejection never leaves the record and runtime diverging.
        """
        merged = dict(network) if network else None
        with self._lifecycle_lock:
            if self._instance is not None:
                self.validate_update(merged)
                if self._merged_state(merged) != self._applied_state():
                    self._apply_instance_update(merged)
            self._network = merged
            allow_internet = (merged or {}).get("allowInternetAccess")
            if allow_internet is not None:
                self._allow_internet_access = bool(allow_internet)

    def _apply_instance_update(self, network: dict | None) -> None:
        """Bind an accepted narrowing to the live instance's new execs.

        A closed/dead ``RuntimeError`` from ``instance.update_network``
        (idle/24h expiry surfaced at apply time) rebuilds the instance
        exactly once under the already-held lifecycle lock and retries the
        apply; after an explicit ``close()``/shutdown the failure propagates
        instead of leaking a fresh instance.
        """
        allow_out = (network or {}).get("allowOut")
        ip_set = list(allow_out) if allow_out is not None else []
        try:
            stale_child_ids = self._instance.update_network(ip_set)
        except RuntimeError as exc:
            # Typed classification only: the message now carries arbitrary core
            # text (SL-12), so it must never decide the rebuild (B1 minor-3).
            reason = _instance_gone_reason(exc)
            if reason is None:
                raise
            if self._closed:
                logger.warning(
                    "not rebuilding %s instance after executor shutdown "
                    "during network update sandbox_id=%s instance_name=%s",
                    reason,
                    self._sandbox_id or "-",
                    self.instance_name,
                )
                raise
            if self._own_identity_active:
                # Rebuilding here would spawn a supervise process on the event
                # loop (this method is synchronous). Refuse instead: the
                # executor's own policy copy stays untouched, so the record and
                # the runtime never diverge, and the next exec rebuilds the
                # slot off the loop with whatever network state is current.
                logger.warning(
                    "route-B instance %s during network update; refusing the "
                    "update instead of restarting the slot from the request "
                    "path sandbox_id=%s instance_name=%s",
                    reason,
                    self._sandbox_id or "-",
                    self.instance_name,
                )
                raise
            logger.info(
                "sandlock instance %s during network update; rebuilding once "
                "sandbox_id=%s instance_name=%s",
                reason,
                self._sandbox_id or "-",
                self.instance_name,
            )
            inst = self._instance
            try:
                inst.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            if self._instance is inst:
                self._instance = None
            inst = self._ensure_instance_locked()
            if inst is None:
                raise RuntimeError(
                    "no sandlock instance available after closed/dead rebuild"
                )
            try:
                # Exactly one retry; a second closed/dead failure propagates.
                stale_child_ids = inst.update_network(ip_set)
            except PermissionError as exc2:
                raise NetworkUpdateConflictError(
                    str(exc2) or "instance refused the network update (EPERM)"
                ) from exc2
        except PermissionError as exc:
            # Defense in depth: a fork EPERM (widening past the static
            # ceiling) maps to the same 409 without persisting.
            raise NetworkUpdateConflictError(
                str(exc) or "instance refused the network update (EPERM)"
            ) from exc
        self._log_stale_children(stale_child_ids)

    def _log_stale_children(self, stale_child_ids: list[int]) -> None:
        """INFO log one line per stale fork child plus a count summary."""
        sandbox_id = self._sandbox_id or "-"
        for child_id in stale_child_ids:
            entry = self._child_registry.get(child_id)
            if entry is None:
                logger.info(
                    "sandbox_id=%s instance_name=%s stale_child_id=%s "
                    "pid=%s cmd=%s",
                    sandbox_id,
                    self.instance_name,
                    child_id,
                    "-",
                    "-",
                )
                continue
            pid, cmd = entry
            logger.info(
                "sandbox_id=%s instance_name=%s stale_child_id=%s pid=%s cmd=%s",
                sandbox_id,
                self.instance_name,
                child_id,
                pid,
                " ".join(cmd),
            )
        logger.info(
            "sandbox_id=%s instance_name=%s network_update stale_child_count=%d",
            sandbox_id,
            self.instance_name,
            len(stale_child_ids),
        )

    def set_mcp_bind_port(self, port: int | None) -> None:
        """Set the per-sandbox MCP gateway host port for the bind ceiling.

        The runtime context pre-allocates the port before the first exec when
        the sandbox has MCP enabled (``record.mcp``), so the instance policy
        can carry ``net_allow_bind=[port]`` from creation. The ceiling is
        fixed at instance creation; ``None`` clears the allowance.
        """
        self._mcp_bind_port = int(port) if port is not None else None

    def _log_exec_failure_context(
        self,
        config: ExecConfig,
        resolved: list[str],
        *,
        exit_code: int | None = None,
    ) -> None:
        """Record what the sandbox's view should look like when an exec fails.

        A failing first command has two very different shapes, and telling
        them apart needs different evidence: a *gone session* (logged by the
        caller) versus a command that ran against a broken image view -- e.g.
        ``cat: not found`` in a sandbox whose rootfs is intact on the host,
        which is only diagnosable together with the chroot path, the per-exec
        env and whether the image's own tools are still on disk. Three stats,
        on the failure path only.

        ``exit_code`` is set when the *child* exited with one of the fork's
        reserved setup/exec codes (125/126/127): then no exception was raised
        and nothing was logged anywhere else, and the exit code is the whole
        diagnosis -- 127 means ``execvp`` failed (a mediated lookup the
        supervisor performs could not be opened, e.g. the ``EAGAIN`` a
        ``..``-symlink lookup under ``RESOLVE_IN_ROOT`` may return), 125 a
        failed ``chdir`` (usually a missing mount point), 126 a failed
        ``setpgid``. The dynamic-linker row is there because that symlink's
        relative target is what such a lookup most often trips on.
        """
        root = self._image_rootfs
        try:
            env = self._exec_params(
                config, bind_ports=self._bind_ports_for(config)
            ).get("env")
            interpreter = None
            if root is not None:
                linker = root / "lib64" / "ld-linux-x86-64.so.2"
                with suppress(OSError):
                    interpreter = {
                        "path": str(linker),
                        "exists": linker.exists(),
                        "target": os.readlink(linker),
                    }
            logger.warning(
                "sandlock exec failure context sandbox_id=%s exit_code=%s "
                "argv=%s cwd=%s env=%s chroot=%s rootfs_tools=%s "
                "dynamic_linker=%s ca_bundle=%s",
                self._sandbox_id or "-",
                exit_code,
                resolved,
                self._view_cwd(config),
                env,
                root,
                (
                    {
                        name: (root / relative).exists()
                        for name, relative in (
                            ("sh", "bin/sh"),
                            ("cat", "usr/bin/cat"),
                            ("run-parts", "usr/bin/run-parts"),
                            ("profile", "etc/profile"),
                        )
                    }
                    if root is not None
                    else None
                ),
                interpreter,
                (Path(self._workspace_dir) / ".e2b-ca/ca-certificates.crt").exists(),
            )
        except Exception as exc:  # noqa: BLE001 - forensics never mask the cause
            logger.warning(
                "sandlock exec failure context unavailable sandbox_id=%s "
                "error=%r",
                self._sandbox_id or "-",
                exc,
            )

    @property
    def instance_name(self) -> str:
        """Stable instance identity: ``sandbox_id`` (or its hash) when given,
        otherwise the workspace directory basename."""
        return self._instance_name or self._instance_name_for()

    @property
    def instance_handle(self):
        """Live ``SandboxInstance``, or ``None`` until ``_ensure_instance()``."""
        return self._instance

    def drain_dirty_dirs(self) -> tuple[list[str], bool] | None:
        """Take the session's written-directory ledger (N25/L2c).

        ``None`` means this backend cannot answer -- no instance yet (nothing
        has been launched, so nothing has been written), an older fork wheel
        without the drain symbol, or the pure shape, where there are no path
        notifications to mark from. The caller falls back to a whole-tree walk,
        which is exactly what it did before this existed.
        """
        holder = self._instance
        drain = getattr(holder, "drain_dirty_dirs", None)
        if drain is None:
            return None
        try:
            return drain()
        except Exception:  # noqa: BLE001 - a capability answer, never a crash
            logger.debug(
                "drain_dirty_dirs unavailable for sandbox %s",
                self._sandbox_id,
                exc_info=True,
            )
            return None

    def set_append_sink(self, sink) -> None:
        """Install the consumer of pushed append events (N25).

        ``sink(bytes_)`` is called from the slot's event-pump thread whenever
        the mediator reports that the sandbox appended bytes. The sink is the
        registry's `note_appended`, so nothing here interprets the number.
        """
        self._append_sink = sink

    def set_file_size_limit(
        self, bytes_: int, stamps: tuple[int, int] | None = None
    ) -> dict | None:
        """Ask the live slot to tighten the running processes' file limit (N25).

        ``None`` means no live instance or a slot that does not know the verb;
        the caller treats that exactly like "no tightening available".
        """
        instance = self._instance
        setter = getattr(instance, "set_file_size_limit", None)
        if setter is None:
            return None
        try:
            if stamps is None:
                return setter(int(bytes_))
            try:
                return setter(int(bytes_), stamps)
            except TypeError:
                # An instance that does not take stamps (not route B): the
                # number still lands, just anchored at arrival.
                return setter(int(bytes_))
        except Exception:  # noqa: BLE001 - a capability answer, never a crash
            logger.warning(
                "update_file_size_limit refused for sandbox %s",
                self._sandbox_id or "-",
                exc_info=True,
            )
            return None

    def read_write_counters(self) -> tuple[int, int] | None:
        """The live slot's write counters, for dating a walk (N25)."""
        instance = self._instance
        reader = getattr(instance, "read_write_counters", None)
        if reader is None:
            return None
        try:
            return reader()
        except Exception:  # noqa: BLE001 - a capability answer, never a crash
            logger.debug("read_write_counters unavailable", exc_info=True)
            return None

    def set_entry_limit(
        self, entries: int, limit: int, stamps: tuple[int, int] | None = None
    ) -> dict | None:
        """Ask the live slot to cap how many names the tree may hold (N31).

        ``None`` means no live instance or a slot that does not know the verb,
        which the caller reads as "this deployment has no entry gate".
        """
        instance = self._instance
        setter = getattr(instance, "set_entry_limit", None)
        if setter is None:
            return None
        try:
            if stamps is None:
                return setter(int(entries), int(limit))
            try:
                return setter(int(entries), int(limit), stamps)
            except TypeError:
                return setter(int(entries), int(limit))
        except Exception:  # noqa: BLE001 - a capability answer, never a crash
            logger.warning(
                "update_entry_limit refused for sandbox %s",
                self._sandbox_id or "-",
                exc_info=True,
            )
            return None

    def capture_checkpoint(self, dir: str, name: str | None = None) -> dict:
        """Write a checkpoint image of this sandbox's live session into ``dir``.

        Returns the outcome shape the worker's checkpoint endpoint reports:
        ``{"captured": bool, "reason": str, ...}``. A ``False`` always carries
        *why*, because a checkpoint is an **offer**: every caller (a pause, an
        operator) keeps doing exactly what it did before this feature existed,
        so "not captured" has to be sayable instead of inferred from silence.

        The three ways to get ``False`` are all capability answers rather than
        failures:

        * the in-process mediator -- no slot owns the process tree, so there is
          nothing a verb could reach (the shape T5 exists for);
        * no live session on this worker: nothing was launched, or the
          generation went away with the worker it ran on;
        * the slot refused -- an older ``sandlock-supervise`` with no
          ``checkpoint`` arm, or a session whose live-child count is not exactly
          one. The engine's own sentence is carried through verbatim.
        """
        if not self._own_identity_active:
            return {
                "captured": False,
                "reason": (
                    "this worker runs the in-process mediator; the sandbox's "
                    "process tree is not in a slot, so no checkpoint verb can "
                    "reach it"
                ),
            }
        instance = self._instance
        if instance is None:
            return {
                "captured": False,
                "reason": "no live session on this worker to capture",
            }
        try:
            reply = instance.capture_checkpoint(dir, name)
        except (SandboxError, SlotDeadError) as exc:
            logger.info(
                "checkpoint refused for sandbox %s: %s",
                self._sandbox_id or "-",
                exc,
            )
            return {"captured": False, "reason": str(exc)}
        outcome: dict = {"captured": True, "reason": ""}
        for key in ("dir", "name", "pid", "fds", "exe", "argv"):
            if key in reply:
                outcome[key] = reply[key]
        return outcome

    def restore_checkpoint(self, dir: str) -> dict:
        """Resume the image in ``dir`` into this sandbox's session on this worker.

        The session is **created** here when there is none: the pooled shape is
        "lease a slot, then tell it what to bring back", and the fork's restore
        arm attaches the resumed process to a launched session -- which is
        exactly what keeps ``exec`` working afterwards (D9/(b) in
        ``docs/checkpoint-restore-e2b-half.md`` §(g)).

        Returns ``{"restored": bool, "reason": str, ...}``; on success the
        engine's ``restore_skipped`` fd list rides along, because a restored
        process has **no** sockets, pipes or memfds left and a caller that
        cannot say so would report a sandbox that looks fine until its first
        read (D6).
        """
        if not self._own_identity_active:
            return {
                "restored": False,
                "reason": (
                    "this worker runs the in-process mediator; there is no slot "
                    "whose session a checkpoint could be resumed into"
                ),
            }
        try:
            instance = self._ensure_instance()
        except Exception as exc:  # noqa: BLE001 - a slot that will not come up
            logger.warning(
                "restore: could not launch a session for sandbox %s",
                self._sandbox_id or "-",
                exc_info=True,
            )
            return {"restored": False, "reason": f"{type(exc).__name__}: {exc}"}
        try:
            reply = instance.restore_checkpoint(dir)
        except (SandboxError, SlotDeadError) as exc:
            logger.warning(
                "restore refused for sandbox %s: %s",
                self._sandbox_id or "-",
                exc,
            )
            self._log_slot_stderr("restore refused")
            return {"restored": False, "reason": str(exc)}
        # The slot's stderr is where the engine's restore breadcrumbs go with
        # `SANLOCK_RESTORE_TRACE=1` (`checkpoint::resume::note`): a session points
        # the *child's* stdio at /dev/null, so without this the only report of a
        # restore that got as far as announcing a child and nothing further is the
        # absence of the process. One line here, on the rare path.
        self._log_slot_stderr("restore")
        outcome: dict = {"restored": True, "reason": ""}
        for key in ("dir", "child_id", "pid", "restore_skipped"):
            if key in reply:
                outcome[key] = reply[key]
        return outcome

    def _log_slot_stderr(self, what: str) -> None:
        """Log the slot's stderr tail, if it has anything to say (never raises)."""
        instance = self._instance
        reader = getattr(instance, "slot_stderr", None)
        if reader is None:
            return
        try:
            text = reader()
        except Exception:  # noqa: BLE001 - diagnostics must not change an outcome
            return
        if text:
            logger.info(
                "%s: slot stderr for sandbox %s:\n%s",
                what,
                self._sandbox_id or "-",
                text,
            )

    def _on_slot_event(self, event: dict) -> None:
        """Handle one pushed slot event (N25)."""
        if event.get("event") == "hello":
            # The slot's proof that the channel is wired: without it, "no
            # events" and "a dead channel" look identical from here.
            logger.info(
                "sandbox_id=%s pushed-append channel is live (slot fd %s)",
                self._sandbox_id or "-",
                event.get("fd"),
            )
            return
        if not getattr(self, "_append_event_seen", False):
            self._append_event_seen = True
            logger.info(
                "sandbox_id=%s received its first pushed event: %r",
                self._sandbox_id or "-",
                event,
            )
        if event.get("event") != "append":
            return
        sink = getattr(self, "_append_sink", None)
        if sink is None:
            return
        try:
            bytes_ = int(event.get("bytes") or 0)
        except (TypeError, ValueError):
            return
        if bytes_ > 0:
            sink(bytes_)
        try:
            return drain()
        except Exception:  # noqa: BLE001 - a capability answer, never a crash
            logger.debug(
                "drain_dirty_dirs unavailable for sandbox %s",
                self._sandbox_id,
                exc_info=True,
            )
            return None

    def _instance_name_for(self) -> str:
        # The rule itself lives in ``gateway_common`` (D20): the control plane
        # derives the slot documents' directory from the same function, so a
        # slot the worker created is one the CP can address.
        sid = self._sandbox_id or Path(self._workspace_dir).name
        return own_identity_instance_name(sid)

    # Warn-once switches for the shapes that decline a slot. The reason itself
    # comes from `_own_identity_decline_reason` -- one decision, quoted verbatim by
    # the disclosure below, so the message can never disagree with the rule.
    _own_identity_no_starter_warned = False
    _own_identity_no_fd_client_warned = False
    _mediation_shape_disclosed = False

    def _own_identity_decline_reason(self) -> str | None:
        """Why this sandbox does not run on a supervise slot, or None if it does.

        Route B needs a *per-sandbox host uid*: the slot process **is** that uid
        (``docs/supervise-identity-handoff.md`` §5), and W1 forbids two live
        generations on one uid, so a shared-uid sandbox cannot have a slot. It
        also needs the native library, the ``sandlock-supervise`` binary the
        wheel ships, and a starter that can drop privileges.

        ``auto`` engages where it matters: the chroot (image-rootfs) shape is
        the only one where ``fs_denied``/chroot path mediation runs, and
        mediating as the sandbox's own uid is what makes mediated writes belong
        to the sandbox (T5). ``E2B_MAX_SLOTS>0`` or ``E2B_OWN_IDENTITY=on`` asks
        for a slot in every shape instead. An operator who explicitly asked for
        route B and cannot get it fails loudly -- route A vs route B is a
        deployment decision, never a silent downgrade (§8).
        """
        cfg = self._own_identity
        if cfg is None:
            return "the worker passed no route-B config (E2B_OWN_IDENTITY_* unset)"
        if cfg.mode == "off":
            return "E2B_OWN_IDENTITY=off"
        if sandlock is None:
            return "the native sandlock module is unavailable"
        forced = cfg.mode == "on" or cfg.slots > 0
        # N15: every shape is mediated now -- the pure (no-rootfs) one included,
        # with the host root as the mediator's root -- so `auto` engages a slot
        # in every shape. That is not route B for its own sake: mediated path
        # operations run as the mediator, and they may only touch the sandbox's
        # files as the sandbox's *own* uid (T5), which a root worker can only do
        # through a slot. Where no slot is possible the reasons below say so and
        # the in-process path fails closed (SL-1) rather than attributing the
        # sandbox's writes to the mediator.
        #
        # (Until N15 this read `auto keeps the pure (no-chroot) shape
        # in-process: it mediates nothing` -- true then, false now.)
        if not self._per_sandbox_uid or self._host_uid is None:
            reason = (
                "no per-sandbox host uid (E2B_PER_SANDBOX_UID off, or the uid "
                "pool allocated nothing): a slot runs as the sandbox's own uid"
            )
            if forced:
                raise RuntimeError(
                    "route B was requested (E2B_OWN_IDENTITY=on / E2B_MAX_SLOTS>0) "
                    "but " + reason
                )
            return reason
        if not cfg.privileged_starter:
            # C3 Task 3 (the only shape left since N52): nobody here changes an
            # identity -- the child unshares and the agent writes the map -- so
            # what is missing is the control-plane reporter, not a privileged
            # starter.
            reason = (
                "E2B_IDENTITY_GRANT=agent-grant needs the control-plane "
                "reporter, and this worker does not know where its control "
                "plane is (E2B_CONTROL_PLANE_URL and E2B_NODE_ID)"
            )
            if forced:
                raise RuntimeError("route B was requested but " + reason)
            if not type(self)._own_identity_no_starter_warned:
                type(self)._own_identity_no_starter_warned = True
                logger.warning(
                    "route B unavailable for sandbox_id=%s: %s; mediation stays "
                    "in-process, which for the chroot shape now fails closed "
                    "(T5 is not traded back)",
                    self._sandbox_id or "-",
                    reason,
                )
            return reason
        from envd_service.own_identity import default_supervise_bin, fd_client_available

        if cfg.transport == "fd" and not fd_client_available():
            # The wheel predates fork F17: it can serve an fd handoff but the
            # worker cannot drive one. Falling back to `path` would put a
            # channel token into the slot's argv, so that is an operator
            # decision, not something to do quietly.
            reason = (
                "the installed sandlock wheel has no sandlock_supervise_connect_fd "
                "(needs fork F17 or newer)"
            )
            if forced:
                raise RuntimeError(
                    "route B was requested with transport=fd, but " + reason
                    + ": rebuild wheels/fork/ or set E2B_SLOT_TRANSPORT=path"
                )
            if not type(self)._own_identity_no_fd_client_warned:
                type(self)._own_identity_no_fd_client_warned = True
                logger.warning(
                    "route B unavailable for sandbox_id=%s: %s; not falling back to "
                    "the registered transport, whose token would sit in the slot's "
                    "world-readable argv (rebuild wheels/fork/ or set "
                    "E2B_SLOT_TRANSPORT=path deliberately)",
                    self._sandbox_id or "-",
                    reason,
                )
            return reason
        if not default_supervise_bin().exists():
            reason = (
                f"{default_supervise_bin()} is missing (the sandlock wheel ships "
                "the supervise binary)"
            )
            if forced:
                raise RuntimeError("route B was requested but " + reason)
            logger.warning(
                "route B unavailable for sandbox_id=%s: %s; running the in-process "
                "instance",
                self._sandbox_id or "-",
                reason,
            )
            return reason
        return None

    def _disclose_mediation_shape(self) -> None:
        """Say up front what an in-process chroot sandbox now means.

        E2B no longer asks for a mediation tier, and fork B3 (2026-09-11)
        deleted that field outright, so the combination it used to paper over
        -- privileged in-process mediator + path mediation + a non-zero sandbox
        host uid -- is refused by the fork (route B is the only remedy it
        names) instead of silently producing supervisor-owned files (SL-1/T5).
        None of that reaches the operator through the library, though: the FFI
        create/launch entry points return a null handle and the SDK turns it into
        ``sandlock_instance_launch failed`` (SL-12), so this is the only place
        that says which rule fired and *why no slot was available* -- with the
        reason quoted from the very function that decided it.
        """
        if self._own_identity_active or type(self)._mediation_shape_disclosed:
            return
        if sandlock is None or not self._in_process_mediation_is_refused():
            return
        type(self)._mediation_shape_disclosed = True
        logger.error(
            "mediated sandbox_id=%s runs in-process, not on a "
            "supervise slot (%s): path mediation would then execute as the "
            "mediator, so the fork refuses the create instead of leaving "
            "supervisor-owned files behind (T5, no downgrade tier is set any "
            "more). Fix: keep E2B_PER_SANDBOX_UID on and let route B lease a "
            "slot (E2B_OWN_IDENTITY=auto/on), or run a privileged launcher or an "
            "external slot fleet.",
            self._sandbox_id or "-",
            self._own_identity_decline or "reason unavailable",
        )

    def _in_process_mediation_is_refused(self) -> bool:
        """Would the fork refuse *this* sandbox's in-process path mediation?

        Mirrors the fork's C-tier check (``mediation_remap_is_refused``: F6.1
        fail-closed, F14 privilege rule). Mediated path operations run in the
        mediator, so a mediator that can remap the sandbox to a *different*
        non-zero host uid attributes the sandbox's own files to itself (T5) and
        the create is refused -- the fork's only remedy is route B, since the
        downgrade tier that used to accept this shape no longer exists (B3).

        The distinction matters because this predicate gates a loud ERROR: a
        non-root worker (E5.1) mediates as its own euid, which *is* the
        sandbox's host uid, so nothing is refused there and disclosing it on
        every unprivileged run would be a false alarm.
        """
        if self._per_sandbox_uid:
            if self._host_uid is None:
                # A root worker fails the create on the missing allocation
                # itself (`_run_as_identity` raises); this is not that error.
                return False
            host_uid = self._host_uid
        else:
            host_uid = LEGACY_SHARED_UID if os.geteuid() == 0 else os.geteuid()
        mediator_euid = os.geteuid()
        if mediator_euid == 0:
            return host_uid != 0
        return (
            host_uid != 0
            and host_uid != mediator_euid
            and has_effective_cap(CAP_SETUID)
            and has_effective_cap(CAP_SETGID)
        )

    def _slot_key(self) -> str:
        """The pool key for this sandbox (slots are leased per sandbox)."""
        return self._sandbox_id or self.instance_name

    def _refuse_in_process_without_a_quota(self, reason: str) -> None:
        """N83 phase 1, plan Review Focus 4: no slot ⇒ no create when required.

        A sandbox that does not get a route-B slot runs under the **in-process**
        mediator, and that mediator has no per-sandbox cgroup at all -- there is
        no ``sbx_<id>`` to attach its process tree to. A deployment that asked
        for per-sandbox cgroups (``E2B_SANDBOX_CGROUP=required``) therefore
        cannot serve it: letting it run is exactly the silent "no quota" the
        switch forbids, and it is the shape an operator hits by turning
        ``E2B_OWN_IDENTITY=off`` (or dropping the per-sandbox host uid, or the
        control-plane reporter) while the cgroup switch still says required.

        Raised at construction, so the create fails with the decline reason on
        record instead of starting a sandbox nobody capped. ``off`` (the
        default) returns immediately: the fallback path is exactly as it was.
        """
        if not self._kernel_enforced_limits():
            return
        raise RuntimeError(
            "E2B_SANDBOX_CGROUP=required refuses an in-process sandbox: this "
            f"sandbox would run without a per-sandbox cgroup ({reason}). Give "
            "the sandbox a route-B slot (per-sandbox host uid + the "
            "control-plane reporter), or set E2B_SANDBOX_CGROUP=off to accept "
            "uncapped sandboxes."
        )

    def _kernel_enforced_limits(self) -> bool:
        """Does this deployment enforce the sandbox's budgets in the kernel?

        ``E2B_SANDBOX_CGROUP=required`` is that question, and the worker
        already reads it from route B's config for the in-process refusal
        above. N83 phase 2 (Task 4, D7) asks the same one twice more -- the
        slot policy has to be told, so the fork can retire the mediator's own
        accounting notifications (``kernel_enforced_limits`` on the wire) --
        and one reader is what keeps the three answers from drifting apart.

        ``off`` (the default) answers ``False``, and a settings double that
        predates the field reads the same way route B's own default does.
        """
        if self._own_identity is None:
            return False
        mode = str(getattr(self._own_identity, "sandbox_cgroup", "off") or "off")
        return mode.strip().lower() == "required"

    def _notify_rate_limit_for_the_lane(self) -> int | None:
        """The notification cap for this lane, or ``None`` for "no cap".

        N82 (2026-10-06) measured what this cap actually is: a stand-in for the
        supervisor's *accounting*. The supervisor's CPU used to land on the
        worker pod and on nobody's quota, so capping the notification rate was
        the only thing keeping one sandbox from spending a neighbor's core --
        and the cap was also the 0.86 s of every second that ordinary
        ``npm install``-class work ran into (N79/N82).

        N83 (2026-10-06/07) removed that premise. On the cgroup lane the whole
        sandbox tree -- ``sandlock-superv`` included -- sits in ``sbx_<id>``,
        whose ``cpu.max`` is the sandbox's own declared share, so a flood now
        spends the flooder's budget and the kernel throttles it
        (``deploy/scripts/acceptance/cgroup_acceptance.py`` check 3, measured
        with this cap off). Keeping the cap on *that* lane would buy nothing
        and pay the stall back.

        So the cap travels only where no cgroup bounds the box: ``off`` (the
        rollback lever) and the in-process mediator, which has no ``sbx_<id>``
        to hang a quota on. There the value is what it always was, byte for
        byte.
        """
        if self._kernel_enforced_limits():
            return None
        return self._notify_rate_limit or None

    def _open_own_identity_instance(self):
        """Lease this sandbox's slot and wrap it in the instance shim.

        The ceiling travels as a full-field ``--policy`` document (the same
        field set the in-process builder gets, in wire spellings), and the
        generation's main program is the parking shell: an envd instance has
        no main process, but launch-first is what brings the slot's session up
        and the generation ends when that process ends.
        """
        cfg = self._own_identity
        document = supervise_policy_document(self._policy_ceiling())
        pool = slot_pool_for(cfg)
        uid = self._host_uid

        def _start():
            return pool.acquire_sync(
                self._slot_key(),
                document,
                uid=uid,
                name=self.instance_name,
                # N83 phase 1: the sandbox's **declared** share. The policy
                # ceiling above clamps to ``min(100, ...)`` for the fork's own
                # throttle; the cgroup is what actually enforces the declared
                # number, so it must travel unclamped.
                cpu_percent=self._cpu_percent,
                # N83 phase 2 (Task 3): the same two numbers the record
                # carries become the box's ``memory.high``/``memory.max`` and
                # ``pids.max``. The slot policy above still declares the
                # mediator's own soft ceiling; the cgroup is what makes the
                # budget a kernel fact.
                memory_mb=self._memory_mb,
                max_processes=self._max_processes,
            )

        try:
            handle = _start()
        except SlotDeadError as exc:
            if self._closed:
                raise
            # W1 recycles a uid by restarting its process, so a slot that died
            # on the way up is restarted exactly once here; a second failure
            # surfaces unchanged.
            logger.info(
                "route-B slot for sandbox_id=%s failed to start (%s); "
                "restarting once",
                self._sandbox_id or "-",
                exc,
            )
            handle = _start()
        logger.info(
            "route-B instance ready sandbox_id=%s instance_name=%s uid=%s "
            "slot=%s channel=%s guest-uid=%s",
            self._sandbox_id or "-",
            self.instance_name,
            uid,
            handle.name,
            # transport 1 has no path at all; naming the handoff keeps the log
            # honest about why there is nothing to look at in /tmp.
            (
                f"fd-handoff(pid {handle.process.pid})"
                if handle.sock_path is None
                else handle.sock_path
            ),
            # The slot's own answer, not an assumption: a self-mapped namespace
            # makes the workload uid 0 inside (parity with the in-process
            # mediator), and an unavailable unprivileged userns leaves it at the
            # host uid. `unknown` means the wheel predates fork F18.
            handle.guest_uid or "unknown",
        )
        instance = OwnIdentityInstance(pool=pool, handle=handle, name=self.instance_name)
        # N25: start consuming the slot's pushed events. `False` means this
        # slot has no events channel (an older wheel, or the registered-path
        # transport), which costs acceleration only -- the accounting still
        # answers from the filesystem, and the ceiling from the ledger.
        sink = getattr(self, "_append_sink", None)
        logger.info(
            "sandbox_id=%s pushed-append wiring: sink=%s slot_events=%s",
            self._sandbox_id or "-",
            sink is not None,
            handle.events_socket is not None,
        )
        if sink is not None:
            started = instance.start_event_pump(self._on_slot_event)
            logger.info(
                "sandbox_id=%s pushed-append pump started=%s",
                self._sandbox_id or "-",
                started,
            )
            if not started:
                logger.info(
                    "sandbox_id=%s has no pushed-append channel (slot %s): "
                    "disk accounting keeps its polling path",
                    self._sandbox_id or "-",
                    self.instance_name,
                )
        return instance

    async def _ensure_instance_async(self):
        """:meth:`_ensure_instance` without ever blocking the event loop.

        Creating an in-process instance is a quick native call, so it runs
        inline under the lifecycle lock as before. Creating a route-B instance
        spawns a process and waits for its registered channel to answer, which
        takes the same lock on a worker thread.
        """
        if self._own_identity_active:
            return await asyncio.to_thread(self._ensure_instance)
        with self._lifecycle_lock:
            return self._ensure_instance_locked()

    async def _reopen_instance_after(self, reason: str, previous) -> object:
        """Release a closed/dead instance and build its replacement once.

        Shared by both backends: the executor must never leak a fresh
        instance after an explicit ``close()``/shutdown, and the replacement
        may block (route B restarts a slot process), so it runs off the loop.
        """
        retire = self._retire_previous_instance

        if self._own_identity_active:
            # Closing a route-B instance ends a *process* (shutdown verb +
            # reap), so it goes off the loop like the creation next to it.
            await asyncio.to_thread(retire, reason, previous)
        else:
            retire(reason, previous)
        return await self._ensure_instance_async()

    def _retire_previous_instance(self, reason: str, previous) -> None:
        """Drop a closed/dead instance handle under the lifecycle lock.

        Shared by both backends; only the in-process one is cheap enough to
        run inline on the event loop.
        """
        with self._lifecycle_lock:
            if self._closed:
                logger.warning(
                    "not rebuilding %s exec instance after executor shutdown "
                    "sandbox_id=%s instance_name=%s",
                    reason,
                    self._sandbox_id or "-",
                    self.instance_name,
                )
                raise RuntimeError(
                    f"sandlock instance is {reason} and the executor is shut down"
                )
            if self._instance is previous:
                try:
                    previous.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
                self._instance = None

    def _ensure_instance(self):
        """Lazily create the one long-lived exec instance (M4 D1/D3).

        The creation segment (policy build + ``SandboxInstance`` construction
        + the rebuild-once retry after a closed/dead launch) runs under the
        lifecycle lock so an ``update_network`` cannot observe a half-created
        instance or interleave preflight/apply with it.
        """
        with self._lifecycle_lock:
            return self._ensure_instance_locked()

    def _ensure_instance_locked(self):
        """Creation core; caller must hold ``_lifecycle_lock``.

        A route-B instance needs the supervise binary and the native channel
        client (checked by :meth:`_own_identity_decline_reason`), not the in-process
        ``SandboxInstance`` class, so the availability guard only applies to
        the in-process backend.
        """
        if self._instance is None and (
            self._own_identity_active or SandboxInstance is not None
        ):
            if self._instance_name is None:
                self._instance_name = self._instance_name_for()
            # Route B: the "instance" is a supervise slot leased to this
            # sandbox, so the ceiling travels as a policy document and the
            # exec verbs cross the channel instead of the FFI.
            if self._own_identity_active:
                self._instance = self._open_own_identity_instance()
            else:
                policy = self._build_instance_policy()
                try:
                    self._instance = SandboxInstance(
                        policy, name=self._instance_name
                    )
                except RuntimeError as exc:
                    # Typed classification only (B1 minor-3): the launch reason
                    # is the core's own text and may contain anything.
                    reason = _instance_gone_reason(exc)
                    if reason is None:
                        logger.warning(
                            "sandlock instance launch failed sandbox_id=%s "
                            "instance_name=%s error=%s",
                            self._sandbox_id or "-",
                            self._instance_name,
                            exc,
                        )
                        raise
                    # The prior session was closed (shutdown/idle reclaim) or died
                    # (machinery failure): rebuild exactly once, and let a second
                    # failure bubble up unchanged.
                    logger.info(
                        "sandlock instance relaunching after %s sandbox_id=%s "
                        "instance_name=%s",
                        reason,
                        self._sandbox_id or "-",
                        self._instance_name,
                    )
                    try:
                        self._instance = SandboxInstance(
                            policy, name=self._instance_name
                        )
                    except RuntimeError as exc2:
                        logger.warning(
                            "sandlock instance relaunch failed sandbox_id=%s "
                            "instance_name=%s error=%s",
                            self._sandbox_id or "-",
                            self._instance_name,
                            str(exc2),
                        )
                        raise
            if self._instance is not None:
                logger.info(
                    "sandlock instance created sandbox_id=%s instance_name=%s "
                    "max_memory=%s max_processes=%d chroot=%s",
                    self._sandbox_id or "-",
                    self._instance_name,
                    f"{self._memory_mb}M",
                    self._max_processes,
                    "yes" if self._has_sandbox_root else "no",
                )
        return self._instance

    def close(self) -> None:
        """Close the exec instance and release the handle (idempotent).

        Marks the executor shut down: later ``start()`` calls fail loudly
        instead of rebuilding a fresh instance (the closed/dead rebuild-once
        retry is for idle/24h expiry during a live sandbox, not for after an
        explicit teardown).
        """
        with self._lifecycle_lock:
            self._closed = True
            if self._instance is not None:
                logger.info(
                    "sandlock instance closed sandbox_id=%s instance_name=%s",
                    self._sandbox_id or "-",
                    self._instance_name,
                )
                self._instance.close()
                self._instance = None
            self._child_registry.clear()

    def _child_exited(self, child_id: int, pid: int) -> None:
        """Drop a reaped child from the staleness registry.

        ``pid`` guards against removing a newer entry if the fork recycles a
        child id after an instance rebuild: the entry is only removed while
        it still points at the process that just ended.
        """
        entry = self._child_registry.get(child_id)
        if entry is not None and entry[0] == pid:
            del self._child_registry[child_id]

    def _materialize_http_inject(
        self, entries: list[dict]
    ) -> list[dict]:
        """Turn ``http_inject`` entries carrying literal header values into
        sandlock ``secret`` sources.

        A literal value becomes a supervisor-only secret file (mode 0600,
        never granted to the sandbox); a ``${e2b.identity.tokens.<NAME>}``
        placeholder resolves either from the sandbox's registered ``iam``
        workload tokens (minting a JWT-SVID for the audience) or, as a
        fallback, from the ``E2B_IDENTITY_TOKEN_<NAME>`` env var of the
        worker (platform-injected literal secret). Raises when a placeholder
        has no backing source, so a misconfigured IAM secret fails at
        sandbox creation instead of silently sending the request
        unauthenticated. Every value is resolved *before* the first file is
        written, so that failure leaves the secrets dir exactly as it found
        it. The returned entries keep the input order, env- and file-backed
        alike (a header rule's order is part of what the caller configured).
        """
        if not entries:
            return []
        if self._secrets_dir is None:
            raise RuntimeError(
                "http_inject (rules[].transform.headers) requires a "
                "supervisor secrets dir"
            )
        import re

        placeholder = re.compile(
            r"\$\{e2b\.identity\.tokens\.([A-Za-z0-9_]+)\}"
        )
        # A1a: resolve **every** entry's value before anything is written.
        # Resolution is the step that can fail on configuration (a placeholder
        # no token or env var backs), and publishing entry-by-entry used to
        # leave the earlier entries' files on disk -- already chowned to the
        # sandbox uid -- for a sandbox that was never created. Fail the build
        # while the secrets dir is still empty; the write phase below cannot
        # raise from resolution at all.
        out: list[dict] = []
        # Phase 2 walks this in input order, so an env-backed entry (whose
        # value is already final and needs no file: ``None``) keeps its place
        # among the file-backed ones instead of being hoisted to the front.
        pending: list[tuple[dict, str | None]] = []
        for entry in entries:
            value = str(entry["value"])
            m = placeholder.fullmatch(value)
            if m is not None and m.group(1) not in self._iam_tokens:
                # Pure env-backed placeholder: keep the supervisor env source
                # (sandlock reads it at build time) instead of a file.
                var = f"E2B_IDENTITY_TOKEN_{m.group(1)}"
                if var not in os.environ:
                    raise RuntimeError(
                        f"header transform for {entry['matcher']} references "
                        f"identity token {m.group(1)!r} but {var} is not set "
                        f"(and no iam token named {m.group(1)!r} was registered)"
                    )
                entry = dict(entry)
                entry.pop("value", None)
                entry["secret"] = f"env:{var}"
                pending.append((entry, None))
                continue
            if "${e2b.identity.tokens." in value:

                def _resolve(mm: re.Match) -> str:
                    name = mm.group(1)
                    token_cfg = self._iam_tokens.get(name)
                    if token_cfg is not None:
                        # SDK workload identity (iam=...): mint a JWT-SVID for
                        # the requested audience (supports "Bearer ${...}").
                        return self._mint_iam_jwt(
                            audience=str(token_cfg.get("audience", ""))
                        )
                    var = f"E2B_IDENTITY_TOKEN_{name}"
                    if var in os.environ:
                        return os.environ[var]
                    raise RuntimeError(
                        f"header transform for {entry['matcher']} references "
                        f"identity token {name!r} but {var} is not set "
                        f"(and no iam token named {name!r} was registered)"
                    )

                value = placeholder.sub(_resolve, value)
            pending.append((entry, value))
        # The write phase: every value is resolved by now, so the only failures
        # from here on are I/O ones (cleaned up and raised, as before).
        from envd_service import priv_helpers

        for entry, value in pending:
            if value is None:
                # The env-backed entry was finished in phase 1; only its place
                # in the order is owed here.
                out.append(entry)
                continue
            secret_dir = self._secrets_dir / os.path.basename(
                self._workspace_dir.rstrip("/")
            )
            secret_dir.mkdir(parents=True, exist_ok=True)
            path = secret_dir / f"{entry['name']}.secret"
            # Reclaim the name before writing it. A previous build already
            # handed this exact path to the pooled uid, and an earlier fix is
            # not enough for the *second* write:
            # a non-root worker whose file now belongs to a sandbox uid has no
            # ownership, no CAP_FOWNER and no CAP_DAC_OVERRIDE, so ``open(w)``
            # -- and every later ``chmod`` -- is EACCES/EPERM. This is the
            # normal path, not a corner: ``_policy_ceiling()`` caches nothing
            # and runs again on every route-B (re)open and every idle/expiry
            # respawn, and nothing else ever deletes these files, so a sandbox
            # would otherwise live exactly one instance lifetime. ``unlink``
            # asks only for write permission on the parent directory, which is
            # the worker's own non-sticky ``<secrets>/<sandbox_id>`` -- so the
            # one step that still works is also the correct one (start the
            # name over rather than try to overwrite a file we gave away).
            #
            # A1c: "the worker's own non-sticky parent" is the *whole* premise
            # of that reclaim, so it is checked, not assumed. If something
            # re-chowned the directory (or made it sticky) the ``unlink`` no
            # longer holds and the create below would fail as a bare EACCES
            # naming neither the directory nor the contract -- fail closed
            # here, naming the parent, the owner it has and the one it needs.
            parent = os.stat(secret_dir)
            if parent.st_uid != os.geteuid() or parent.st_mode & stat.S_ISVTX:
                raise priv_helpers.PrivHelperError(
                    f"refusing to reclaim {path}: the parent directory "
                    f"{secret_dir} is not this worker's own non-sticky "
                    f"directory (owner uid {parent.st_uid}, mode "
                    f"{oct(parent.st_mode & 0o7777)}): reclaiming a "
                    "handed-over name needs a parent owned by the worker with "
                    "no sticky bit -- who chowned it or set its mode?"
                )
            path.unlink(missing_ok=True)
            # A1b: 0600 is the *create* mode, never a chmod afterwards. A plain
            # ``open(path, "w")`` creates at ``0666 & ~umask`` (0644 under the
            # usual 022) and only the following chmod tightened it -- a window
            # in which the credential was readable by every other tenant on the
            # host. ``os.open`` with the mode makes the file 0600 from its
            # first byte, and there is nothing left to set after the hand-over.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value)
            # The **slot** is what reads this file, and a slot runs as the
            # sandbox's own host uid (T5) -- so a 0600 file owned by the worker
            # is one the supervisor cannot open: the route-B policy then fails
            # validation ("invalid sandbox: credential file ... Permission
            # denied") and the sandbox never starts (measured 2026-09-25, the
            # first time a pure-shape header-injection case ran on a slot).
            # Ownership follows the reader; that is not an exposure, because the
            # file lives outside every fs grant the sandbox has -- outside the
            # rootfs in the image shape, and outside `can_read`'s allow-list in
            # the pure one -- so the sandbox cannot reach it even owning it.
            #
            # The mode lands **while the worker still owns the file**: after
            # the hand-over below the worker is neither the owner nor
            # CAP_FOWNER, so a chmod that ran after it would be EPERM and the
            # whole create would fail -- the same order
            # `checkpoint_store._prepare_image_parent` uses (mode, then hand
            # the tree over).
            identity = self._host_uid if self._per_sandbox_uid else None
            if identity is not None:
                from envd_service import agent_fileops

                agent_client = agent_fileops.active()
                if agent_client is not None:
                    # C3 Task 4: the hand-over is the agent's step, asked for as
                    # ``{sandbox_id, op}`` -- the secret path is derived from
                    # the control plane's own settings there (hard rule 3).
                    secret_sandbox_id = (
                        self._sandbox_id
                        if isinstance(self._sandbox_id, str)
                        else Path(self._workspace_dir).name
                    )
                    agent_client.chown_secret(secret_sandbox_id, entry["name"])
                elif os.geteuid() == 0:
                    with suppress(OSError):
                        os.chown(path, identity, -1)
                else:
                    # No agent and no root: nothing on this worker can hand the
                    # file to the sandbox's uid any more (the file-capability
                    # broker is retired, open-issues N52), and the slot would
                    # fail later at supervise with a permission error naming
                    # neither the path nor the reason. Name it here -- fail
                    # closed, never "hand it over if we can" -- and leave
                    # nothing behind: the file already carries a live
                    # credential, and the next create is what writes a fresh
                    # one.
                    path.unlink(missing_ok=True)
                    raise priv_helpers.PrivHelperError(
                        f"cannot hand {path} to sandbox uid {identity}: this "
                        "worker has no privileged file-step path (no per-node "
                        "agent is configured, and it is not root)"
                    )
            entry = dict(entry)
            entry.pop("value", None)
            entry["secret"] = f"file:{path}"
            out.append(entry)
        return out

    def _run_as_identity(self) -> tuple[int, int]:
        """Host uid/gid passed to sandlock ``RunAs`` (S1.2 contract).

        With per-sandbox uid enabled the allocated ``host_uid`` becomes the
        sandbox's host identity (inside the namespace it is still uid 0).
        A non-root worker cannot map an arbitrary host uid (S1.2 fail-closed:
        single-entry userns maps only the caller's own euid), so it degrades
        to the worker identity — fixed uid + Landlock, the E5.1 model.
        A root worker with per-sandbox uid enabled but no allocated uid is a
        configuration error and fails loudly instead of silently downgrading
        to a shared uid.
        """
        if self._per_sandbox_uid:
            if self._host_uid is not None:
                return self._host_uid, self._host_uid
            if os.geteuid() != 0:
                if not type(self)._non_root_fallback_warned:
                    type(self)._non_root_fallback_warned = True
                    logger.warning(
                        "non-root worker: cannot map per-sandbox host uids "
                        "(single-entry userns); using fixed worker identity "
                        "+ Landlock"
                    )
                return os.geteuid(), os.getegid()
            raise RuntimeError(
                "per-sandbox uid enabled but sandbox has no allocated "
                "host_uid (worker uid pool did not provision it)"
            )
        # Legacy default: all sandboxes share host uid 1000. Only a root
        # worker can map that uid; a non-root worker would be rejected by
        # S1.2's fail-closed RunAs check (single-entry userns maps only the
        # caller's own identity), so it falls back to the worker identity —
        # fixed uid + Landlock, the E5.1 model — instead of hardcoding 1000.
        if os.geteuid() == 0:
            return 1000, 1000
        return os.geteuid(), os.getegid()


    def _mint_iam_jwt(self, audience: str) -> str:
        """Mint a JWT-SVID for a registered workload identity.

        Local compatible layer: HS256-signed with the worker's IAM signing key
        (``E2B_IAM_SIGNING_KEY``), carrying the requested audience. Upstreams
        that validate must be configured with the same key.
        """
        import base64
        import hashlib
        import hmac
        import json
        import time

        def _b64(data: bytes) -> bytes:
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        header = _b64(b'{"alg":"HS256","typ":"JWT"}')
        now = int(time.time())
        payload = _b64(
            json.dumps(
                {
                    "aud": audience,
                    "iss": "e2b-sandlock",
                    "iat": now,
                    "exp": now + 600,
                },
                separators=(",", ":"),
            ).encode()
        )
        signing_input = header + b"." + payload
        sig = hmac.new(
            self._iam_signing_key.encode(), signing_input, hashlib.sha256
        ).digest()
        return (signing_input + b"." + _b64(sig)).decode()
    @staticmethod
    def resolve_cmd(cmd: list[str]) -> list[str]:
        """Translate ``/bin/bash`` to ``/bin/sh`` when bash is unavailable
        (slim base images do not ship bash; the official SDK always sends
        ``cmd=/bin/bash``)."""
        if cmd and cmd[0] == "/bin/bash":
            return ["/bin/sh"] + cmd[1:]
        return cmd

    def _bind_ports_for(self, config: ExecConfig) -> list[int] | None:
        """Per-exec bind allowance for ``instance.exec(bind_ports=...)``.

        Only the MCP gateway command may bind the pre-allocated MCP host
        port (sandboxes share the worker network namespace); ordinary
        commands exec without a bind allowance.
        """
        if (
            self._mcp_bind_port is not None
            and "mcp-gateway" in " ".join(config.cmd)
        ):
            return [self._mcp_bind_port]
        return None

    @property
    def _is_image_rootfs(self) -> bool:
        """Whether this sandbox has an extracted image rootfs to chroot into."""
        return bool(self._base_image and self._image_rootfs is not None)

    @property
    def _synthetic_rootfs(self) -> Path | None:
        """The synthesized root this pure sandbox pivots into, or None (N16).

        Keyed on the *absence* of a base image: a sandbox with an image always
        uses the image. ``E2B_PURE_ROOTFS=off`` (the default) leaves the pure
        shape on N15's identity translation, so this is a shape switch an
        operator flips -- not a silent change to a fleet.
        """
        if self._base_image or self._image_rootfs is not None:
            return None
        if self._pure_rootfs_dir is None:
            return None
        return Path(self._pure_rootfs_dir) / (self._sandbox_id or "unnamed")

    @property
    def _has_sandbox_root(self) -> bool:
        """Whether this sandbox has a root of its own to be confined to."""
        return self._is_image_rootfs or self._synthetic_rootfs is not None

    @property
    def _sandbox_root(self) -> Path | None:
        """The root the path mediator and the fork's real root work on."""
        if self._is_image_rootfs:
            return Path(self._image_rootfs)
        return self._synthetic_rootfs

    def _volume_only_mount_map(self) -> dict[str, str]:
        """N15's mount map: the workspace under both aliases, plus the volumes.

        Named so the two builders stop carrying a third copy of these five
        lines (``_policy_ceiling`` and ``_build_sandbox``).
        """
        mount_map = {
            "/home/user": self._workspace_dir,
            "/workspace": self._workspace_dir,
        }
        mount_map.update(self._fs_mounts)
        return mount_map

    def _volume_only_mount_map_writable(self, fs_writable: list[str]) -> list[str]:
        """The mount points the pure shape must declare writable.

        Load-bearing twice over: the fork derives a mount *source*'s rights from
        what the policy declares for its mount point, and the per-exec cwd
        (`/home/user`) has to be inside the instance ceiling or the exec is
        refused outright ("exec params exceed the instance policy ceiling").
        """
        return (
            list(fs_writable)
            + ["/workspace", "/home/user"]
            + [str(virtual) for virtual in self._fs_mounts]
        )

    def _materialize_root(self, root: Path) -> dict[str, str]:
        """This sandbox's mount map, with every target created on disk.

        The image branch is production code: its pre-created
        ``workspace``/``home/user``/``dev`` entries exist because slim images
        extract without them, and they go through ``_mkdir_traversable`` (the
        worker's own directories, so a umask may not decide their mode), while
        anything the image shipped is left alone. The synthesized branch adds
        the host system directories and the whole-tree ``/dev`` bind on top of
        the workspace aliases and the volumes, and heals its own tree
        (``_materialize_synthetic_rootfs``).
        """
        mount_map = self._volume_only_mount_map()
        if self._is_image_rootfs:
            # The workspace alias (/home/user, declared first: the fork breaks
            # host-source ties by declaration order) plus /workspace, and the
            # /dev parent dir minimal_dev's nodes hang under -- slim base
            # images extract without any of the three. They are the *worker's*
            # directories (the image never had them), so they are created by
            # ``_mkdir_traversable``'s explicit chmod: a plain ``mkdir`` lands
            # ``0700`` under ``umask 077``, and the ``_ensure_chroot_mount_points``
            # right below early-returns on the ones that already exist, so
            # nothing else would ever fix them. Directories the image *did*
            # ship (its ``home/``, its ``/usr``) are left exactly as they are --
            # that is the asymmetric half, and it is the image's call, not ours.
            for mount_point in ("workspace", "home/user", "dev"):
                _mkdir_traversable(root.joinpath(mount_point))
            for virtual in self._fs_mounts:
                _mkdir_traversable(root.joinpath(virtual.removeprefix("/")))
            # minimal_dev replaces the whole-tree host /dev mount: only the six
            # single-node mounts (ptmx, pts, null, urandom, zero, tty) are
            # visible under the chroot's /dev, so /dev/shm and /dev/mqueue
            # cannot leak in and no fs_deny carve-out is needed. Native
            # ExecStdio.PTY lives host-side, so no devpts node grants are part
            # of this shape either.
            mount_map.update(_minimal_dev_mounts())
            _ensure_chroot_mount_points(root, mount_map)
            return mount_map
        # The synthesized pure root: the host's system directories and the
        # whole-tree /dev bind go on top of the workspace aliases and volumes.
        mount_map.update(_synthetic_rootfs_mounts())
        _materialize_synthetic_rootfs(root, mount_map)
        return mount_map

    @property
    def _chroot_root(self) -> str:
        """The root the path mediator confines this sandbox to (N15/N16).

        An image-rootfs sandbox gets the extracted image. A **pure** sandbox
        (no base image, no rootfs) gets the **host root** -- unless the
        synthesized-root switch (N16, ``pure_rootfs_dir``) is on, in which case
        it gets its own directory and the synthetic mount table. Host root is
        what makes the pure shape mediated at all: virtual path == host path,
        i.e. identity translation, so every existing chroot handler applies
        unchanged and the syscalls Landlock has no access right for (stat/
        readlink/chmod/xattr/inotify_add_watch/open_tree/...) stop leaking host
        metadata.

        The alternative -- a second gate written for the pure shape -- was
        rejected in `docs/pure-shape-decision.md` §5: the same 33 entries would
        need per-syscall semantics, and every future syscall would land in that
        bucket again. Root "/" also carries the disk ledger and the volume
        mounts with it, because the mediator is the same code either way.

        Consequence, stated where it is decided: a mediated sandbox needs the
        mediator to run as *the sandbox's own* uid, so the pure shape now wants
        a route-B slot (`_own_identity_decline_reason`); where it cannot have one the
        in-process path is refused rather than silently attributing the
        sandbox's writes to the mediator (SL-1, fail closed).
        """
        root = self._sandbox_root
        return str(root) if root is not None else "/"

    def _view_cwd(self, config: ExecConfig) -> str | None:
        """Map a host-side command cwd into the sandbox's view for exec.

        Both shapes mount the workspace at /home/user (the canonical alias: the
        fork's ``host_to_virtual`` breaks host-source ties by declaration order
        and ``mount_map`` declares /home/user first) and /workspace, and both
        end up *inside* the sandbox at /home/user. They get there differently,
        and the difference is load-bearing:

        * the image shape answers with the **virtual** spelling, which the fork
          joins under the rootfs before its real ``chdir``;
        * the synthesized pure shape (N16) is a root of its own, so it answers
          the virtual spelling for the same reason the image shape does;
        * the pure shape without a root (N15) has root "/" -- joining would give
          the host's own ``/home/user``, which need not exist -- so it answers
          with the **host** workspace path, which is what the fork can actually
          chdir to; the mediator then maps that back to /home/user through the
          mount table (``resolve.rs::host_to_virtual`` checks mount targets
          first), so the sandbox still reports the canonical alias.

        Paths outside the workspace pass through unchanged in both shapes (S9:
        the image shape's fs_readable covers /).
        """
        cwd = (config.cwd or "").strip()
        if self._has_sandbox_root:
            # Both rooted shapes answer with the virtual spelling: the fork
            # joins it under the root before its real chdir, and both roots
            # carry /home/user as the workspace's canonical alias.
            if not cwd or cwd.startswith(str(self._workspace_dir)):
                return "/home/user"
            return cwd or None
        # Pure: default to the workspace rather than to "no chdir at all", so
        # the two shapes agree on where a command starts.
        return cwd or str(self._workspace_dir)

    def _exec_params(self, config: ExecConfig, *, bind_ports=None) -> dict:
        """Per-exec parameter dict for ``SandboxInstance.exec``.

        ``cwd`` maps through ``_view_cwd``; ``env`` starts from the command
        env plus the instance's HTTPS-MITM CA pinning when that branch is
        active; ``clean_env=True`` starts every child from an empty
        environment. None-valued params are dropped so the fork defaults
        apply.
        """
        env = dict(config.env)
        env.update(self._http_inject_env)
        params = {
            "cwd": self._view_cwd(config),
            "env": env,
            "clean_env": True,
            "bind_ports": bind_ports or None,
            # N25/C: a per-exec RLIMIT_FSIZE, if the caller has a fresh
            # measurement of what is left. The fork refuses anything above the
            # instance ceiling, so this can only ever tighten.
            "max_file_size": config.max_file_size,
        }
        return {k: v for k, v in params.items() if v is not None}

    def _max_file_size_bytes(self) -> int | None:
        """The single-file ceiling (``RLIMIT_FSIZE``) to ask the fork for (N28/C).

        ``None`` = do not ask, i.e. inherit the system limit. The *number* is
        the caller's own decision (``RuntimeRegistry.max_file_size_mb``, the
        largest budget the sandbox was sold); this only converts MiB to the
        bytes the fork's builder takes.

        The limit is what makes the disk story have a *hard* half. Every other
        disk bound here is either sold up front (the admission ledger) or
        measured after the fact (the L2b walk); this one is the kernel
        refusing a write that would cross it, at zero runtime cost to the
        worker. Because it is per *process*, it necessarily also covers the
        things a sandbox writes outside its tree (``/tmp`` inside the image
        rootfs, a volume's mount) -- the budget it is set to is the largest of
        those, so it can only ever refuse something already over budget.
        """
        # N25: zero is a ceiling, not "unset" -- over its disk budget a sandbox
        # may not grow a file at all, and this is the value that says so. Only
        # ``None`` means "this policy carries no file-size ceiling", and only a
        # negative number is nonsense.
        if self._max_file_size_mb is None or self._max_file_size_mb < 0:
            return None
        return int(self._max_file_size_mb) * 1024 * 1024

    def _policy_ceiling(self) -> dict:
        """Command-independent policy ceiling for the long-lived instance, as kwargs.

        Everything the instance grants regardless of the individual command:
        fs writable/readable/denied, the network ceiling (net_allow/deny,
        http_*, host_mask, egress_proxy, http_inject), resource limits,
        uid/gid/mediation tier, the chroot + fs_mount shape and
        net_isolation/fd_inject/port_mappings. Per-command cwd/env/clean_env
        live in ``_exec_params``; the MCP bind allowance comes from
        ``set_mcp_bind_port`` (the context pre-allocates the port before the
        first exec). ``_build_sandbox`` below keeps the same field mapping
        for the one-shot probes/security tests that still use it.
        """
        fs_writable = [self._workspace_dir]
        fs_writable.extend(self._extra_fs_writable)
        fs_readable = ["/usr", "/lib", "/bin", "/opt"]
        # Denials are only issued where the sandbox can actually see the
        # path: without a chroot, Landlock is an allow-list and shared paths
        # like /dev/shm are already unreachable (not in fs_readable), so
        # rules would add nothing -- and they would cost something: a denial is
        # enforced by an on-behalf open the *mediator* performs, so mediated
        # writes belong to whoever mediates. On a route-B slot that is this
        # sandbox's host uid (T5's fix); on a privileged in-process mediator it
        # would be host uid 0, which the fork refuses outright now that E2B no
        # longer asks for a mediation tier -- and fork B3 deleted the field
        # itself, so there is nothing left to ask for (SL-1).
        fs_denied: list[str] = []
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: "/" resolves inside the chroot (the image
            # rootfs), so the whole image is readable as its own filesystem.
            # The host filesystem stays unreachable: the chroot restricts the
            # path space, and shared volumes are only exposed via their exact
            # fs_writable directory. minimal_dev mounts only the six /dev
            # nodes, so /dev/shm and /dev/mqueue never exist in the sandbox
            # view and need no carve-out; /proc/kcore and /sys stay denied
            # as defensive entries.
            fs_readable = list(fs_readable) + ["/"]
            # The sandbox's own workspace (and every volume) is a *mount* in
            # this mode, and what the sandbox touches behind those mount points
            # lives on the host side. The fork grants a mount's rights to its
            # *source*, and it derives them from what the policy declares for
            # the mount point -- so the mount points have to be declared here,
            # in the sandbox's own namespace, next to the host spellings above
            # (which are what the mediator's on-behalf gate compares real paths
            # against). Without this the source gets no path rule at all, and a
            # binary the sandbox writes into its own workspace cannot be
            # exec'd (N35: a static ELF fails with EACCES while the same binary
            # inside the image rootfs runs).
            fs_writable = (
                list(fs_writable)
                + ["/workspace", "/home/user"]
                + [str(virtual) for virtual in self._fs_mounts]
            )
            fs_denied = ["/proc/kcore", "/sys"]
        else:
            # N15's pure allow-list, with or without a synthesized root: the
            # synthetic root does **not** add "/" (in the image shape that
            # spelling means "the whole rootfs"; here it would only name the
            # skeleton) and keeps `fs_denied` empty (Landlock's allow-list
            # already refuses everything outside it). What the mounts do need
            # is their mount points declared writable -- see
            # `_volume_only_mount_map_writable`.
            fs_writable = self._volume_only_mount_map_writable(fs_writable)
        net_allow: list[str] = []
        net_deny: list[str] = []
        http_allow: list[str] = []
        http_inject: list[dict] = []
        host_mask: str | None = None
        egress_proxy: dict | None = None
        if self._network:
            from gateway_common.network import sandlock_network_policy

            policy = sandlock_network_policy(
                self._network,
                allow_internet_access=self._allow_internet_access,
                enable_network=self._enable_network,
                private_deny_cidrs=list(self._network_deny_cidrs),
            )
            net_allow = policy["net_allow"]
            net_deny = policy["net_deny"]
            http_allow = policy["http_allow"]
            http_inject = self._materialize_http_inject(policy["http_inject"])
            host_mask = policy["host_mask"]
            egress_proxy = policy["egress_proxy"]
        elif self._allow_internet_access and self._enable_network:
            net_allow = [
                "files.pythonhosted.org:443",
                "pypi.org:443",
                "registry.npmjs.org:443",
                "proxy.golang.org:443",
                "static.crates.io:443",
                "github.com:443",
                "raw.githubusercontent.com:443",
            ]

        sandbox_uid, sandbox_gid = self._run_as_identity()
        kwargs: dict = {
            "fs_writable": fs_writable,
            "fs_readable": fs_readable,
            "fs_denied": fs_denied,
            "net_allow": net_allow,
            "net_deny": net_deny,
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": host_mask,
            "egress_proxy": egress_proxy,
            "max_memory": f"{self._memory_mb}M",
            "max_processes": self._max_processes,
            "max_open_files": self._max_open_files,
            "max_cpu": min(100, max(1, self._cpu_percent)),
            "max_disk": f"{self._disk_mb}M",
            # SEC-K0S-006: `statfs(2)` reports the host's volume; point the
            # sandbox at the platform's own accounting for it instead. The
            # file is written by the worker (it owns the quota and measures
            # the tree); the executor only names it.
            "disk_stats_path": self._disk_stats_path,
            "max_file_size": self._max_file_size_bytes(),
            "notify_rate_limit": self._notify_rate_limit_for_the_lane(),
            "uid": sandbox_uid,
            "gid": sandbox_gid,
        }
        if self._kernel_enforced_limits():
            # N83 phase 2 (Task 4, D7): the one lane in which the kernel is the
            # enforcer of this sandbox's memory budget tells the fork so, and
            # the fork retires the address-space accounting family it no longer
            # needs (`Sandbox::kernel_enforced_limits`). Deliberately absent --
            # not `False` -- off the lane, because that document has to stay
            # byte-for-byte the one it was before this field existed.
            kwargs["kernel_enforced_limits"] = True
        if self._mcp_bind_port is not None:
            # The SDK starts the MCP gateway inside the sandbox; the whole
            # instance may bind its HTTP port (per-sandbox allocated MCP
            # port, since sandboxes share the worker network namespace).
            kwargs["net_allow_bind"] = [self._mcp_bind_port]
            if self._enable_net_isolation:
                # E7.1: under net_isolation the gateway listens inside the
                # sandbox's own loopback-only netns, unreachable from the
                # worker; S2.5 inbound mapping serves the sandbox's accept()
                # from a supervisor host-loopback listener on the same port,
                # which is what the /mcp proxy dials (127.0.0.1:<port>).
                self._port_mappings.setdefault(
                    self._mcp_bind_port, self._mcp_bind_port
                )
        if self._pid_ns:
            # S2.2 sibling: own PID namespace. The engine creates the leader
            # directly inside its user namespace with clone3 (an unprivileged
            # CLONE_NEWPID needs that user namespace), so this is independent
            # of net_isolation.
            kwargs["pid_ns"] = True
        # N35, unconditional since N14 S5: build a real root instead of
        # emulating one, so the kernel resolves paths (a `#!` interpreter, a
        # static binary) inside the sandbox's own tree. Only meaningful with a
        # chroot root, and the shape is per-sandbox: one with neither an image
        # rootfs nor a synthesized one has nothing to pivot into, so it is a
        # no-op there (loud once per executor) rather than a failed create.
        if self._has_sandbox_root:
            kwargs["real_root"] = True
        else:
            logger.warning(
                "sandbox %s has no image rootfs and no synthesized root (pure "
                "shape): the real root has nothing to pivot into for it",
                self._sandbox_id or "<unnamed>",
            )
        if self._enable_net_isolation:
            kwargs["net_isolation"] = True
            if self._port_mappings:
                kwargs["port_mappings"] = dict(self._port_mappings)
                if self._bind_inject:
                    # S2.5 bind injection: the mapped port becomes a socket the
                    # sandbox itself listens on (created in the worker netns and
                    # injected at bind() time), so the supervisor leaves the
                    # accept/readiness path -- no host listener, no eager-accept
                    # worker, no poll/epoll_wait interception. Measured cost of
                    # the mapping path it replaces: ~390 ms per MCP request.
                    kwargs["net_bind_inject"] = True
            if self._fd_inject_connect:
                kwargs["fd_inject_connect"] = True
            elif not getattr(type(self), "_netns_no_inject_warned", False):
                type(self)._netns_no_inject_warned = True
                logger.warning(
                    "net_isolation enabled without fd_inject_connect: "
                    "sandboxes are loopback-only (all external egress fails). "
                    "create_app refuses this shape unless "
                    "E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY=1 -- reaching this "
                    "line means the executor was built outside that guard."
                )
        elif self._fd_inject_connect:
            kwargs["fd_inject_connect"] = True
        root = self._sandbox_root
        if root is not None:
            # A root of its own -- the extracted image, or the synthesized pure
            # root (N16) -- is what the mediator and the fork's real root work
            # on, and `_materialize_root` is the one place the mount targets are
            # created inside it (fs_mount only takes effect at runtime, so the
            # points must already exist for chdir() to work).
            kwargs["chroot"] = self._chroot_root
            kwargs["fs_mount"] = self._materialize_root(root)
            # No `fs_writable` write-back is needed here: the shape branch
            # above finalizes the list before `kwargs` is built, so the literal
            # already carries it. What keeps the per-exec cwd inside the
            # instance ceiling is `_volume_only_mount_map_writable`'s mount
            # points, not a late reassignment (the old comment here described a
            # version whose dict predated the branch).
        else:
            # N15: the pure shape (no base image, no synthesized root) is
            # mediated too, with the host root as the mediator's root --
            # identity translation, so the handlers it reaches are the ones
            # that already exist. The mount table is the image branch's minus
            # `minimal_dev`: on the host root `/dev` is the host's own, exactly
            # as it was before this change, and re-mounting six nodes into the
            # sandbox's view would be a second behaviour change smuggled into
            # this one.
            #
            # `fs_readable` deliberately does *not* gain "/" in either pure
            # shape -- in the image shape that spelling means "the whole image
            # rootfs", and on the host root it would grant the entire
            # filesystem. Keeping the allow-list is what makes this a *second*
            # gate rather than a new policy: `can_read`/`can_write` now enforce
            # on the syscalls Landlock cannot see exactly what Landlock already
            # enforces on the ones it can.
            #
            # Volumes used to reach this shape as symlinks inside the sandbox
            # directory ("virtual mount paths cannot be materialized"): they are
            # real mounts now, and the mediator resolves them before the root,
            # so the symlink path is no longer what carries them.
            #
            # The mount points go into `fs_writable` for the image branch's
            # reason (see `_volume_only_mount_map_writable`). The host spellings
            # that are granted this way are never resolved by the sandbox -- the
            # mount table wins before the root, for every mediated syscall.
            kwargs["chroot"] = self._chroot_root
            kwargs["fs_mount"] = self._volume_only_mount_map()
            kwargs["fs_writable"] = fs_writable
        if http_allow and self._image_rootfs is not None:
            # HTTPS MITM for rule-registered domains: sandlock intercepts 443
            # with an ephemeral CA; splice that CA into a per-sandbox copy of
            # the image trust bundle (never mutate the shared rootfs) and pin
            # the copy via SSL_CERT_FILE so in-sandbox clients trust it. The
            # env pinning travels per exec (the ceiling carries no env).
            ca_src = self._image_rootfs / "etc/ssl/certs/ca-certificates.crt"
            if ca_src.is_file():
                ca_dir = Path(self._workspace_dir) / ".e2b-ca"
                ca_dir.mkdir(parents=True, exist_ok=True)
                ca_dst = ca_dir / "ca-certificates.crt"
                try:
                    shutil.copy2(ca_src, ca_dst)
                except OSError:
                    ca_dst = None
                if ca_dst is not None:
                    # sandlock resolves http_inject_ca in the sandbox's view:
                    # the chroot-visible path, not the host path (the host
                    # path would be resolved under the rootfs and "not found").
                    ca_inside = (
                        Path("/workspace/.e2b-ca/ca-certificates.crt")
                        if kwargs.get("chroot")
                        else ca_dst
                    )
                    kwargs["http_inject_ca"] = [str(ca_inside)]
                    self._http_inject_env = {
                        "SSL_CERT_FILE": str(ca_inside),
                        "CURL_CA_BUNDLE": str(ca_inside),
                    }
        return kwargs

    def _build_instance_policy(self):
        """The ceiling as a native ``Sandbox`` policy object (or a plain
        namespace off-Linux, so the mapping stays unit-testable)."""
        kwargs = self._policy_ceiling()
        if sandlock is None:
            from types import SimpleNamespace

            return SimpleNamespace(**kwargs)
        return SandlockSandbox(**kwargs)

    def _build_sandbox(self, config: ExecConfig):
        """One-shot per-command ``Sandbox`` policy builder.

        Kept for the security/probe tests that still run one-shot sandboxes;
        production commands exec onto ``_build_instance_policy``'s ceiling
        through ``start()`` and carry cwd/env/clean_env/bind_ports per exec.
        """
        fs_writable = [self._workspace_dir]
        fs_writable.extend(self._extra_fs_writable)
        fs_readable = ["/usr", "/lib", "/bin", "/opt"]
        # Denials are only issued where the sandbox can actually see the
        # path: without a chroot, Landlock is an allow-list and shared paths
        # like /dev/shm are already unreachable (not in fs_readable), so
        # rules would add nothing -- and issuing them would cost the sandbox
        # its own file ownership: sandlock enforces denials through its
        # on-behalf open path, so every file the sandbox creates is then
        # attributed to the supervisor (host uid 0) instead of the sandbox
        # host uid, which silently voids both ``chmod`` inside the sandbox
        # and the per-uid isolation of shared volumes.
        fs_denied: list[str] = []
        if self._base_image and self._image_rootfs is not None:
            # Image rootfs mode: "/" resolves inside the chroot (the image
            # rootfs), so the whole image is readable as its own filesystem.
            # The host filesystem stays unreachable: the chroot restricts the
            # path space, and shared volumes are only exposed via their exact
            # fs_writable directory. minimal_dev mounts only the six /dev
            # nodes, so /dev/shm and /dev/mqueue never exist in the sandbox
            # view and need no carve-out; /proc/kcore and /sys stay denied
            # as defensive entries.
            fs_readable = list(fs_readable) + ["/"]
            # Same declaration as the instance ceiling above: the mount points
            # of the sandbox's own tree, in the sandbox's own namespace, so the
            # fork can grant their host sources the rights the policy declares.
            fs_writable = (
                list(fs_writable)
                + ["/workspace", "/home/user"]
                + [str(virtual) for virtual in self._fs_mounts]
            )
            fs_denied = ["/proc/kcore", "/sys"]
        else:
            # The one-shot twin of `_policy_ceiling`'s pure branch: same
            # allow-list, with or without a synthesized root, and the mount
            # points of the sandbox's own tree declared writable.
            fs_writable = self._volume_only_mount_map_writable(fs_writable)
        net_allow: list[str] = []
        net_deny: list[str] = []
        http_allow: list[str] = []
        http_inject: list[dict] = []
        host_mask: str | None = None
        egress_proxy: dict | None = None
        if self._network:
            from gateway_common.network import sandlock_network_policy

            policy = sandlock_network_policy(
                self._network,
                allow_internet_access=self._allow_internet_access,
                enable_network=self._enable_network,
                private_deny_cidrs=list(self._network_deny_cidrs),
            )
            net_allow = policy["net_allow"]
            net_deny = policy["net_deny"]
            http_allow = policy["http_allow"]
            http_inject = self._materialize_http_inject(policy["http_inject"])
            host_mask = policy["host_mask"]
            egress_proxy = policy["egress_proxy"]
        elif self._allow_internet_access and self._enable_network:
            net_allow = [
                "files.pythonhosted.org:443",
                "pypi.org:443",
                "registry.npmjs.org:443",
                "proxy.golang.org:443",
                "static.crates.io:443",
                "github.com:443",
                "raw.githubusercontent.com:443",
            ]

        sandbox_uid, sandbox_gid = self._run_as_identity()
        kwargs: dict = {
            "fs_writable": fs_writable,
            "fs_readable": fs_readable,
            "fs_denied": fs_denied,
            "net_allow": net_allow,
            "net_deny": net_deny,
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": host_mask,
            "egress_proxy": egress_proxy,
            "max_memory": f"{self._memory_mb}M",
            "max_processes": self._max_processes,
            "max_open_files": self._max_open_files,
            "max_cpu": min(100, max(1, self._cpu_percent)),
            "max_disk": f"{self._disk_mb}M",
            "disk_stats_path": self._disk_stats_path,
            "max_file_size": self._max_file_size_bytes(),
            "notify_rate_limit": self._notify_rate_limit_for_the_lane(),
            "clean_env": True,
            "env": dict(config.env),
            "cwd": config.cwd,
            "uid": sandbox_uid,
            "gid": sandbox_gid,
        }
        if "mcp-gateway" in " ".join(config.cmd):
            # The SDK starts the MCP gateway inside the sandbox; it must be
            # allowed to bind its HTTP port (per-sandbox MCP_PORT, since
            # sandboxes share the worker network namespace).
            mcp_port = str((config.env or {}).get("MCP_PORT", "50005"))
            kwargs["net_allow_bind"] = [mcp_port]
            if self._enable_net_isolation:
                # E7.1: under net_isolation the gateway listens inside the
                # sandbox's own loopback-only netns, unreachable from the
                # worker; S2.5 inbound mapping serves the sandbox's accept()
                # from a supervisor host-loopback listener on the same port,
                # which is what the /mcp proxy dials (127.0.0.1:<port>).
                port = int(mcp_port)
                self._port_mappings.setdefault(port, port)
        if self._pid_ns:
            # S2.2 sibling: own PID namespace. The engine creates the leader
            # directly inside its user namespace with clone3 (an unprivileged
            # CLONE_NEWPID needs that user namespace), so this is independent
            # of net_isolation.
            kwargs["pid_ns"] = True
        # N35, unconditional since N14 S5: build a real root instead of
        # emulating one, so the kernel resolves paths (a `#!` interpreter, a
        # static binary) inside the sandbox's own tree. Only meaningful with a
        # chroot root, and the shape is per-sandbox: one with neither an image
        # rootfs nor a synthesized one has nothing to pivot into, so it is a
        # no-op there (loud once per executor) rather than a failed create.
        if self._has_sandbox_root:
            kwargs["real_root"] = True
        else:
            logger.warning(
                "sandbox %s has no image rootfs and no synthesized root (pure "
                "shape): the real root has nothing to pivot into for it",
                self._sandbox_id or "<unnamed>",
            )
        if self._enable_net_isolation:
            kwargs["net_isolation"] = True
            if self._port_mappings:
                kwargs["port_mappings"] = dict(self._port_mappings)
                if self._bind_inject:
                    # S2.5 bind injection: the mapped port becomes a socket the
                    # sandbox itself listens on (created in the worker netns and
                    # injected at bind() time), so the supervisor leaves the
                    # accept/readiness path -- no host listener, no eager-accept
                    # worker, no poll/epoll_wait interception. Measured cost of
                    # the mapping path it replaces: ~390 ms per MCP request.
                    kwargs["net_bind_inject"] = True
            if self._fd_inject_connect:
                kwargs["fd_inject_connect"] = True
            elif not getattr(type(self), "_netns_no_inject_warned", False):
                type(self)._netns_no_inject_warned = True
                logger.warning(
                    "net_isolation enabled without fd_inject_connect: "
                    "sandboxes are loopback-only (all external egress fails)"
                )
        elif self._fd_inject_connect:
            kwargs["fd_inject_connect"] = True
        root = self._sandbox_root
        if root is not None:
            # A root of its own, either shape: chroot into it and expose the
            # sandbox directory as /home/user (canonical alias: declared first,
            # and the fork breaks host-source ties by declaration order) and
            # /workspace (official SDK spelling) inside it. The mount targets
            # are created by `_materialize_root` (fs_mount only takes effect at
            # runtime, so they must already exist for chdir() to work).
            kwargs["chroot"] = self._chroot_root
            kwargs["fs_mount"] = self._materialize_root(root)
            # Same as `_policy_ceiling`: the shape branch above finalizes
            # `fs_writable` before `kwargs` is built, so there is nothing to
            # write back here.
        else:
            # N15, the one-shot twin of `_policy_ceiling`'s pure branch: host
            # root, identity translation, the workspace under both aliases and
            # the sandbox's volumes as mounts (no `minimal_dev` -- see there).
            kwargs["chroot"] = self._chroot_root
            kwargs["fs_mount"] = self._volume_only_mount_map()
            kwargs["fs_writable"] = fs_writable
        kwargs["cwd"] = self._view_cwd(config)
        if http_allow and self._image_rootfs is not None:
            # HTTPS MITM for rule-registered domains: sandlock intercepts 443
            # with an ephemeral CA; splice that CA into a per-sandbox copy of
            # the image trust bundle (never mutate the shared rootfs) and pin
            # the copy via SSL_CERT_FILE so in-sandbox clients trust it.
            ca_src = self._image_rootfs / "etc/ssl/certs/ca-certificates.crt"
            if ca_src.is_file():
                ca_dir = Path(self._workspace_dir) / ".e2b-ca"
                ca_dir.mkdir(parents=True, exist_ok=True)
                ca_dst = ca_dir / "ca-certificates.crt"
                try:
                    shutil.copy2(ca_src, ca_dst)
                except OSError:
                    ca_dst = None
                if ca_dst is not None:
                    # sandlock resolves http_inject_ca in the sandbox's view:
                    # the chroot-visible path, not the host path (the host
                    # path would be resolved under the rootfs and "not found").
                    ca_inside = (
                        Path("/workspace/.e2b-ca/ca-certificates.crt")
                        if kwargs.get("chroot")
                        else ca_dst
                    )
                    kwargs["http_inject_ca"] = [str(ca_inside)]
                    env = dict(kwargs.get("env") or {})
                    env["SSL_CERT_FILE"] = str(ca_inside)
                    env["CURL_CA_BUNDLE"] = str(ca_inside)
                    kwargs["env"] = env
        if sandlock is None:
            # Non-Linux / missing native library: return a plain object so the
            # policy mapping stays unit-testable without executing anything.
            from types import SimpleNamespace

            return SimpleNamespace(**kwargs)
        return SandlockSandbox(**kwargs)

    async def start(self, config: ExecConfig) -> SandlockRunningProcess:
        """Exec ``config.cmd`` onto the long-lived instance (M4 D3).

        PTY commands use the fork-native ``ExecStdio.PTY`` (host-side master,
        resized through ``ExecProcess.resize``) instead of the removed
        in-sandbox bridge; everything else uses ``ExecStdio.PIPED``. Per-exec
        cwd/env/clean_env/bind_ports come from ``_exec_params``. A
        closed/dead failure from ``inst.exec`` -- the in-process FFI's typed
        ``InstanceClosedError``/``InstanceDeadError``, or a route-B slot's
        coded refusal (idle-15min/24h instance expiry surfaces at exec time,
        not construction) -- rebuilds the instance exactly once under the
        lifecycle lock and retries the exec; after an explicit
        ``close()``/shutdown the executor fails loudly
        instead of rebuilding.
        """
        if sandlock is None:
            raise unimplemented("Sandlock is not available on this platform")
        if self._closed:
            raise RuntimeError(
                "sandlock executor is shut down; refusing to start a command "
                "on a rebuilt instance"
            )
        # The normal exec path takes the lifecycle lock around instance
        # creation too, so a concurrent ``update_network`` serializes against
        # it exactly like any other ``_ensure_instance`` caller -- but a
        # route-B creation (spawn + readiness probe) runs off the loop.
        inst = await self._ensure_instance_async()
        if inst is None:
            raise unimplemented("Sandlock is not available on this platform")
        stdio = ExecStdio.PTY if config.pty else ExecStdio.PIPED
        resolved = self.resolve_cmd(config.cmd)

        async def _exec_once(target) -> object:
            return await asyncio.to_thread(
                target.exec,
                resolved,
                stdio,
                **self._exec_params(
                    config, bind_ports=self._bind_ports_for(config)
                ),
            )

        try:
            proc = await _exec_once(inst)
        except Exception as exc:  # noqa: BLE001 - classified below, re-raised whole
            # Two independent ways a session can be gone, and both rebuild
            # exactly once: the in-process FFI reports it as a *typed* error,
            # while a route-B slot answers a *served* refusal whose type the
            # channel cannot carry -- but which arrives with the fork's stable
            # refusal *code* attached (``_refusal_reason``). Neither shape is
            # ever classified from prose (B1 minor-3: a message that merely
            # mentions closed/dead must not decide a rebuild; the pin lives in
            # ``tests/unit/test_sandlock_executor_instance.py``).
            reason = _instance_gone_reason(exc)
            if reason is None:
                reason = _refusal_reason(exc)
            if reason is None:
                logger.warning(
                    "sandlock exec failed sandbox_id=%s instance_name=%s "
                    "argv=%s error_type=%s error=%s",
                    self._sandbox_id or "-",
                    self.instance_name,
                    resolved,
                    type(exc).__name__,
                    exc,
                )
                self._log_exec_failure_context(config, resolved)
                raise
            # Idle/24h expiry, machinery death or a collapsed route-B
            # generation surfaced at exec time: rebuild exactly once and
            # retry. Never after an explicit close() (a concurrent shutdown
            # must not leak a fresh instance).
            logger.info(
                "sandlock instance %s during exec; rebuilding once "
                "sandbox_id=%s instance_name=%s argv=%s",
                reason,
                self._sandbox_id or "-",
                self.instance_name,
                resolved,
            )
            inst = await self._reopen_instance_after(reason, inst)
            if inst is None:
                raise unimplemented("Sandlock is not available on this platform")
            # Exactly one retry; a second closed/dead failure propagates.
            proc = await _exec_once(inst)
        # F4.3/S2 staleness mapping: register the fork child (id -> pid +
        # resolved argv) before the running process is returned so a later
        # ``update_network`` can log which children keep their old policy.
        self._child_registry[proc.child_id] = (proc.pid, resolved)
        # SEC-K0S-003: the reader thread below pushes through
        # ``call_soon_threadsafe``, so this queue is where output piles up when
        # the consumer is slow -- it used to be unbounded and was the buffer
        # the live acceptance measured (59 MiB -> 330 MiB anon on a 256 MiB
        # command with a frozen client, while the subscriber queue and the
        # relay's own counters stayed untouched). A thread cannot apply
        # backpressure, so this hop drops (with the marker and a WARNING).
        queue = ByteBudgetQueue(max_bytes=self._stream_limit_bytes)
        limit_bytes = self._stream_limit_bytes
        # SEC-K0S-003, the hop that actually OOMKilled the worker: the pump
        # below runs in a reader *thread*, and ``call_soon_threadsafe`` is a
        # queue no bound above can see. The gate makes the thread wait for
        # room, which stops it draining the command's pipe (real backpressure,
        # nothing lost).
        handoff = ThreadHandoff(max_bytes=self._stream_limit_bytes)
        stdin_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_running_loop()
        running = SandlockRunningProcess(
            proc=proc,
            queue=queue,
            loop=loop,
            stdin_queue=stdin_queue,
            handoff=handoff,
            pty_mode=config.pty,
            on_exit=self._child_exited,
            on_setup_failure=lambda code: self._log_exec_failure_context(
                config, resolved, exit_code=code
            ),
            # ``kill(sig)`` on an in-process child is always SIGKILL (see
            # ``SandlockRunningProcess.kill``); a slot child takes the signal
            # number through ``kill_child``, so the pause/resume fallback may
            # use it (M4 D5 / FUP #8).
            signal_pause_supported=self._own_identity_active,
        )
        if config.pty:
            # The removed in-sandbox bridge applied the requested window size
            # at spawn; a fresh pty starts with the kernel default (0x0), so
            # apply the create-time rows/cols once before any output flows.
            running.resize(config.rows, config.cols)

        def _enqueue(item: tuple) -> None:
            """Runs on the event loop (``call_soon_threadsafe``)."""
            try:
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    if item[0] == "__eof__":
                        queue.put_control(item)
                        return
                    if not queue.note_dropped(item_bytes(item)):
                        return
                    logger.warning(
                        "sandbox_id=%s: the output queue is full (budget %s "
                        "bytes); dropping command output from here on and "
                        "marking the stream truncated",
                        self._sandbox_id or "-",
                        limit_bytes,
                    )
                    queue.put_control((item[0], TRUNCATED_MARK))
            finally:
                handoff.release(item_bytes(item))

        def _pump(stream, kind: str) -> None:
            if stream is None:
                loop.call_soon_threadsafe(running._mark_eof)
                return
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    handoff.acquire(len(chunk))
                    loop.call_soon_threadsafe(_enqueue, (kind, chunk))
            except Exception:  # pragma: no cover - defensive
                pass
            finally:
                loop.call_soon_threadsafe(_enqueue, ("__eof__", kind))
                loop.call_soon_threadsafe(running._mark_eof)

        if config.pty:
            streams = [(proc.pty, "pty")]
        else:
            streams = [
                (proc.stdout, "stdout"),
                (proc.stderr, "stderr"),
            ]
        for stream, kind in streams:
            threading.Thread(
                target=_pump, args=(stream, kind), daemon=True
            ).start()

        return running
