"""The agent's ``/proc`` reads: a slot's host pid, and the worker's own identity.

Two questions are answered here, both against the *host's* process table (the
agent is the only component with ``hostPID``/``pid: host``) and both by the same
discipline -- exact matches, named refusals, never a guess:

* **which host pid is this slot's child** (C3 Task 3, ruling D9.3) --
  :meth:`ProcLookup.host_pid`;
* **which uid/gid does this worker itself run as** (C3 Task 4, ruling D21
  option 2) -- :meth:`ProcLookup.worker_uid_gid`, used by the compose lane,
  whose face B has no pod spec to read the way the k8s lane does.

**The slot half (D9.3).** The worker forks the slot's child and reports the pid
**it** knows; the agent,
which is the only component with ``hostPID``, has to name the *host* pid that
``as_uid`` will write. The rendezvous is ``NSpid`` -- and ``NSpid`` alone is not
enough, because one host runs several workers (three, in
``deploy/compose/docker-compose.multinode.yml``) and two of them can have a
container pid 42 at the same time.

So a candidate is accepted only when **all** of the following hold:

1. its ``NSpid`` chain has more than one entry and ends in the reported
   container pid (a single-entry chain is a process that never left its own pid
   namespace -- the agent's own ``/proc`` is full of them);
2. ``readlink /proc/<pid>/ns/pid`` equals the worker's recorded pid namespace
   identity, exactly. Two workers in two namespaces cannot collide here, and the
   value cannot be substituted by a worker that cannot see the host's pids;
3. in the k8s lane, the candidate's host-side ``cgroup`` path carries the
   worker pod's UID -- the value the control plane read from the pod API, not
   from the worker.

Every failure is a named refusal and none of them is a fallback to "the first
process whose ``NSpid`` ends in N": a pid that is gone is named as such (the
name the task brief fixes), a candidate in another namespace is named, and two
surviving candidates are refused as ambiguous.

The module is deliberately free of FastAPI and of ``as_uid``: it is a pure
function of a ``/proc`` tree, so the DaemonSet can drive it unchanged
(``deploy/c3_agent/app.py``) and this lane can drive it against a synthetic one.

The worker-identity half has one more moving part, and it is the kernel's rather
than this module's: a process may only read *another* process's ``ns/pid`` when
their identities match (``ptrace_may_access``'s same-uid shortcut, or
``CAP_SYS_PTRACE``). Face A is the workers' own uid and can read them directly;
face B is root without ``CAP_SYS_PTRACE`` (it shares ``pid: host`` with the
control plane, so that capability is deliberately absent), so *its* reads run in
:class:`SubprocessWorkerIdentityResolver`'s child, which the deployment tells to
run as the workers' identity. :func:`main` is that child.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gateway_common.worker_identity import (
    pod_cgroup_token,
    validate_pid_namespace,
    validate_pod_uid,
)

logger = logging.getLogger(__name__)

#: The host's process table (the agent runs with ``hostPID: true``).
DEFAULT_PROC_ROOT = Path("/proc")

#: ``/proc/<pid>/status`` is a few hundred bytes; a file that is much larger is
#: not a status file and is refused rather than partially parsed.
_STATUS_MAX_BYTES = 65536

#: The exit code the resolver entry point uses for a *named* refusal (the same
#: "the privileged/reference step ran and declined" shape ``maint.c`` has).
RESOLVER_REFUSED = 3


class LookupRefusal(Exception):
    """A named, fail-closed refusal: no host pid may be written from this."""


@dataclass(frozen=True)
class WorkerIdentity:
    """Who the control plane says the reported pid belongs to.

    ``pid_namespace`` is the worker's own reported identity and ``pod_uid`` is
    the k8s lane's independent proof. A lane that cannot supply
    ``pid_namespace`` cannot be resolved at all (see :meth:`ProcLookup.host_pid`).
    """

    node_id: str
    pid_namespace: str
    pod_uid: str | None = None


@dataclass(frozen=True)
class SlotProcess:
    """The slot's child, identified the way the kernel identifies a process.

    A pid on its own is not an identity: the kernel reuses numbers, so "pid
    990425 is gone" and "pid 990425 is somebody else now" are different facts
    that a bare number cannot tell apart. ``start_time`` is the kernel's own
    discriminator for one process instance (``/proc/<pid>/stat`` field 22), and
    ``pid_namespace`` is the worker whose namespace it was resolved in -- both
    are read at resolution time so a later check can tell the two apart.
    """

    host_pid: int
    start_time: str
    pid_namespace: str


def missing_slot_pid_message(sandbox_id: str) -> str:
    """The name for "the child is not there any more" (task brief, verbatim).

    It is the one failure an operator sees when a slot's child crashes between
    the worker's report and the agent's write, so it is spelled once, here, and
    asserted verbatim by the tests on both sides of the hop.
    """
    return f"沙箱 {sandbox_id} 的槽位 pid 已不在"


def worker_identity_refusal_message(
    node_id: str, pid_namespace: str, reader: tuple[int, int]
) -> str:
    """The name for "this anchor holds no process *this resolver* can see".

    Spelled once because it is raised from two places -- the ``/proc`` walk in
    :meth:`ProcLookup.worker_uid_gid` and the entry point the resolver child
    runs -- and an operator greps for one spelling. The reader's own identity is
    part of the name on purpose: the kernel only lets a process read *another*
    process's namespace when the two identities match (``ptrace_may_access``'s
    same-uid shortcut, or ``CAP_SYS_PTRACE``), so a resolver running as the
    wrong uid sees **nothing at all** -- a deployment error, not an unknown
    worker.
    """
    return (
        f"worker {node_id}'s pid namespace ({pid_namespace}) holds no process "
        f"this identity resolver can see (it runs as {reader[0]}:{reader[1]}): "
        "refusing to derive its own uid/gid"
    )


def _effective_column(line: str) -> int | None:
    """The effective number of a ``Uid:``/``Gid:`` line, or ``None``.

    ``/proc/<pid>/status`` prints **four** numbers there (real, effective,
    saved, fs) and the line is refused outright when it is not exactly that
    shape. The effective one is read because that is the identity the worker
    itself reports (``os.geteuid()``/``os.getegid()``) and the identity a
    privileged step acts as.
    """
    fields = line.split()[1:]
    if len(fields) != 4 or any(not field.isdigit() for field in fields):
        return None
    return int(fields[1])


def _nspid_chain(status_text: str) -> list[int] | None:
    """The ``NSpid:`` line as ints, or ``None`` when there is not one."""
    for line in status_text.splitlines():
        if not line.startswith("NSpid:"):
            continue
        fields = line.split()[1:]
        if not fields or any(not field.isdigit() for field in fields):
            return None
        return [int(field) for field in fields]
    return None


class ProcLookup:
    """Resolves container pids against a ``/proc`` tree (the host's, in prod)."""

    def __init__(self, proc_root: Path | str = DEFAULT_PROC_ROOT) -> None:
        self._proc_root = Path(proc_root)

    @property
    def proc_root(self) -> Path:
        return self._proc_root

    def still_alive(self, slot: SlotProcess) -> bool:
        """Is this host pid still *the* process the lookup resolved?

        Asked *after* a failed write: a child that ended between the lookup and
        the write must be named as gone rather than reported as an opaque
        refusal from the primitive. A recycled pid is not that child either --
        the kernel's ``start_time`` differs, and a pid that came back in another
        worker's namespace is a different process by construction.
        """
        current = self._process_identity(slot.host_pid)
        if current is None:
            return False
        pid_namespace, start_time = current
        if slot.start_time and start_time and slot.start_time != start_time:
            # The number was reused: the slot's child is gone even though its
            # pid is not.
            return False
        return pid_namespace == slot.pid_namespace

    def _process_identity(self, pid: int) -> tuple[str, str] | None:
        """``(pid namespace, start time)`` for a live pid, or ``None``."""
        pid_namespace = self._pid_namespace(pid)
        if pid_namespace is None:
            return None
        return pid_namespace, self._start_time(pid)

    def _start_time(self, pid: int) -> str:
        """``/proc/<pid>/stat`` field 22: this pid instance's birth tick.

        ``comm`` may contain spaces and parentheses, which is why the split is
        anchored on the *last* ``)``. An unreadable stat is ``""`` (unknown),
        never a match for a recorded value.
        """
        try:
            text = (
                (self._proc_root / str(pid) / "stat")
                .read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            return ""
        fields = text.rsplit(")", 1)[-1].split()
        # After ``comm`` the first field is the state, so field N is index N-3.
        return fields[19] if len(fields) > 19 else ""

    def _status(self, pid: int) -> str | None:
        try:
            text = (self._proc_root / str(pid) / "status").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return None
        if len(text) > _STATUS_MAX_BYTES:
            return None
        return text

    def _pid_namespace(self, pid: int) -> str | None:
        try:
            return str((self._proc_root / str(pid) / "ns" / "pid").readlink())
        except OSError:
            return None

    def _cgroup(self, pid: int) -> str | None:
        try:
            return (
                (self._proc_root / str(pid) / "cgroup")
                .read_text(encoding="utf-8", errors="replace")
                .strip()
            )
        except OSError:
            return None

    def _uid_gid(self, pid: int) -> tuple[int, int] | None:
        """``(effective uid, effective gid)`` of a live pid, or ``None``.

        ``None`` -- never a partially parsed value -- when the status file is
        unreadable, oversized, or does not carry both lines in the four-number
        shape the kernel documents.
        """
        text = self._status(pid)
        if text is None:
            return None
        uid: int | None = None
        gid: int | None = None
        for line in text.splitlines():
            if line.startswith("Uid:"):
                uid = _effective_column(line)
            elif line.startswith("Gid:"):
                gid = _effective_column(line)
        if uid is None or gid is None:
            return None
        return uid, gid

    def worker_uid_gid(
        self,
        identity: WorkerIdentity,
        *,
        claimed: tuple[int, int] | None = None,
        reader: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        """The worker's **own** uid/gid, read out of the kernel (D21 option 2).

        The anchor is the worker's pid namespace identity -- the value the
        worker reports and the control plane records for the slot hand-off
        (ruling D9.3) -- and the answer is the identity the kernel prints for
        the process(es) in it. Nothing about the worker's *claim* decides the
        answer: ``claimed`` is only compared against what the kernel says, so a
        claim the kernel does not confirm is a refusal, never a value handed to
        ``--worker``.

        ``reader`` is the identity this resolver runs as (the process's own
        ``euid``/``egid`` when it is not given). It is not decoration: the
        kernel's ``ptrace_may_access`` rule means a process can only read the
        ``/proc/<pid>/ns/pid`` of a process whose identity matches its own, so
        the reader decides what the walk can see at all -- which is why the
        "nothing in this namespace" refusal names it.

        Raises :class:`LookupRefusal` -- always by name, never with a guess --
        for an unusable anchor, for a namespace that holds no readable process,
        for one that holds more than one, for a status file the kernel's
        identity cannot be read from, for a worker that runs as root, and for a
        claim the kernel does not confirm.
        """
        if not validate_pid_namespace(identity.pid_namespace):
            raise LookupRefusal(
                f"worker {identity.node_id} carries no usable pid namespace "
                f"identity ({identity.pid_namespace!r}): refusing to derive its "
                "own uid/gid from the kernel"
            )
        who = reader if reader is not None else (os.geteuid(), os.getegid())
        in_namespace: list[int] = []
        for pid in self._iter_entries():
            # ``None`` is "not readable from here" (another identity's process)
            # or "gone": neither is this worker's own process.
            if self._pid_namespace(pid) == identity.pid_namespace:
                in_namespace.append(pid)
        if not in_namespace:
            raise LookupRefusal(
                worker_identity_refusal_message(
                    identity.node_id, identity.pid_namespace, who
                )
            )
        if len(in_namespace) != 1:
            raise LookupRefusal(
                f"worker {identity.node_id}'s pid namespace "
                f"({identity.pid_namespace}) holds more than one process: "
                "refusing (ambiguous)"
            )
        pid = in_namespace[0]
        values = self._uid_gid(pid)
        if values is None:
            raise LookupRefusal(
                f"the kernel's uid/gid for worker {identity.node_id} (pid "
                f"namespace {identity.pid_namespace}) cannot be read: refusing"
            )
        uid, gid = values
        if uid <= 0 or gid <= 0:
            # ``--worker`` writes this value and the ``--gid`` gate compares
            # against it; uid 0 is not a worker identity (the same rule the
            # control plane applies to a report, and ``maint.c``'s pool gate
            # applies to a pooled uid).
            raise LookupRefusal(
                f"worker {identity.node_id}'s process in "
                f"{identity.pid_namespace} runs as uid/gid {(uid, gid)} "
                "according to the kernel: refusing (a worker may not run as root)"
            )
        if claimed is not None and (uid, gid) != claimed:
            raise LookupRefusal(
                f"worker {identity.node_id} claims uid/gid {claimed}, but the "
                f"kernel says {(uid, gid)} for {identity.pid_namespace}: "
                "refusing (a worker does not name the identity its privileged "
                "steps act as)"
            )
        return uid, gid

    def host_pid(
        self, container_pid: int, identity: WorkerIdentity, *, sandbox_id: str
    ) -> SlotProcess:
        """The host process the worker knows as ``container_pid``.

        The answer is a :class:`SlotProcess` -- the host pid *plus* the identity
        the kernel gives that pid instance -- so a later "is it still there?"
        cannot mistake a recycled number for the slot's child.

        Raises :class:`LookupRefusal` -- always by name, never with a guess --
        when the identity is unusable, when nothing matches, when nothing
        matches *in the worker's namespace*, when the k8s cgroup proof fails, or
        when more than one candidate survives.
        """
        if not validate_pid_namespace(identity.pid_namespace):
            raise LookupRefusal(
                f"worker {identity.node_id} carries no usable pid namespace "
                f"identity ({identity.pid_namespace!r}): refusing to resolve a "
                "container pid without one"
            )
        if identity.pod_uid is not None and not validate_pod_uid(identity.pod_uid):
            raise LookupRefusal(
                f"the pod uid ({identity.pod_uid!r}) carried for worker "
                f"{identity.node_id} is not a pod uid: refusing"
            )
        # A worker that unshares its own pid namespace (E2B_PID_NS) is not in
        # the tree at all, so "no candidate" is the expected shape for a pid
        # that has ended -- including the one the kernel reused in between.
        by_nspid: list[int] = []
        for entry in self._iter_entries():
            status = self._status(entry)
            if status is None:
                continue
            chain = _nspid_chain(status)
            # ``len > 1``: a process that never entered a child pid namespace
            # cannot be the worker's child, however its number is spelled.
            if chain is None or len(chain) < 2 or chain[-1] != container_pid:
                continue
            by_nspid.append(entry)
        if not by_nspid:
            raise LookupRefusal(missing_slot_pid_message(sandbox_id))
        in_namespace = [
            pid
            for pid in by_nspid
            if self._pid_namespace(pid) == identity.pid_namespace
        ]
        if not in_namespace:
            # The count is diagnostics, not part of the name: on a busy host the
            # number of unrelated processes that happen to share this container
            # pid is whatever the neighbours are doing, and a refusal an operator
            # greps for must not change spelling with them.
            logger.warning(
                "c3-agent lookup: %d process(es) carry container pid %d, none in "
                "worker %s's pid namespace (%s)",
                len(by_nspid),
                container_pid,
                identity.node_id,
                identity.pid_namespace,
            )
            raise LookupRefusal(
                f"container pid {container_pid} is not in worker "
                f"{identity.node_id}'s pid namespace ({identity.pid_namespace}): "
                "refusing"
            )
        if identity.pod_uid is not None:
            token = pod_cgroup_token(identity.pod_uid)
            in_pod = [
                pid
                for pid in in_namespace
                if token in (self._cgroup(pid) or "")
            ]
            if not in_pod:
                raise LookupRefusal(
                    f"container pid {container_pid} is in a process of worker "
                    f"{identity.node_id} but not in pod {identity.pod_uid}'s "
                    "cgroup: refusing"
                )
            in_namespace = in_pod
        if len(in_namespace) != 1:
            raise LookupRefusal(
                f"container pid {container_pid} matches more than one process "
                f"of worker {identity.node_id}: refusing (ambiguous)"
            )
        host_pid = in_namespace[0]
        return SlotProcess(
            host_pid=host_pid,
            start_time=self._start_time(host_pid),
            pid_namespace=identity.pid_namespace,
        )

    def _iter_entries(self) -> list[int]:
        """Every numeric pid in the tree, or none when it cannot be listed."""
        try:
            names = [entry.name for entry in self._proc_root.iterdir()]
        except OSError:
            return []
        return sorted(int(name) for name in names if name.isdigit())


class WorkerIdentityResolver(Protocol):
    """Face B's seam: one anchor in, the kernel's uid/gid out -- or a refusal."""

    def resolve(
        self, identity: WorkerIdentity, *, claimed: tuple[int, int]
    ) -> tuple[int, int]: ...


class ProcWorkerIdentityResolver:
    """In-process resolution against a ``/proc`` tree.

    This is the code the resolver entry point runs (:func:`main`), and the shape
    a test or an embedder drives directly. ``reader`` is explicit here for the
    same reason it is a parameter of :meth:`ProcLookup.worker_uid_gid`: the
    kernel's rule for reading another process's namespace is what decides what
    the walk can see, and a synthetic tree has no such rule.
    """

    def __init__(
        self, lookup: ProcLookup, *, reader: tuple[int, int] | None = None
    ) -> None:
        self._lookup = lookup
        self._reader = reader

    def resolve(
        self, identity: WorkerIdentity, *, claimed: tuple[int, int]
    ) -> tuple[int, int]:
        return self._lookup.worker_uid_gid(
            identity, claimed=claimed, reader=self._reader
        )


class SubprocessWorkerIdentityResolver:
    """Face B's shipped resolver: the kernel read runs as the workers' identity.

    Face B is root **without** ``CAP_SYS_PTRACE`` on purpose -- it runs with
    ``pid: host`` beside the control plane, so giving it that capability would
    let it read the control plane's memory, which is a worse hole than the one
    this lane closes. The kernel's own rule is the way through instead: a reader
    whose identity matches a process's may inspect it, so the child that does
    the ``/proc`` walk runs as the identity the deployment's workers run as
    (``E2B_C3_AGENT_RESOLVER_UID``/``_GID`` -- the worker image's ``USER`` by
    default).

    The child is one short-lived process per kernel-anchored instruction: it
    prints exactly one JSON line (``{"uid":…,"gid":…}`` on success,
    ``{"error":…}`` on a named refusal) and exits 0/``RESOLVER_REFUSED``. Its
    environment is built from scratch rather than inherited, so face B's own
    ``E2B_C3_AGENT_TOKEN`` never reaches it.

    ``process_runner`` is the single seam a test replaces.
    """

    def __init__(
        self,
        *,
        uid: int,
        gid: int,
        timeout_s: float = 10.0,
        python: str | None = None,
        process_runner=None,
        package_root: Path | None = None,
    ) -> None:
        self._uid = int(uid)
        self._gid = int(gid)
        self._timeout_s = float(timeout_s)
        self._python = python or sys.executable
        self._run_process = process_runner or subprocess.run
        self._package_root = (
            Path(package_root) if package_root is not None else _package_root()
        )

    def resolve(
        self, identity: WorkerIdentity, *, claimed: tuple[int, int]
    ) -> tuple[int, int]:
        argv = [
            self._python,
            "-m",
            "deploy.c3_agent.lookup",
            "resolve-worker",
            "--node-id",
            identity.node_id,
            "--pid-namespace",
            identity.pid_namespace,
            "--claimed-uid",
            str(claimed[0]),
            "--claimed-gid",
            str(claimed[1]),
        ]
        try:
            proc = self._run_process(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
                env=_resolver_env(self._package_root),
                user=self._uid,
                group=self._gid,
                extra_groups=[],
            )
        except OSError as exc:
            # ``strerror`` keeps the message operator-sized; an agent container
            # without a python at ``sys.executable`` is a deployment error.
            detail = exc.strerror or type(exc).__name__
            raise LookupRefusal(
                f"could not run the identity resolver as {self._uid}:{self._gid}: "
                f"{detail}"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise LookupRefusal(
                f"the identity resolver did not answer within {self._timeout_s}s "
                f"({type(exc).__name__}): refusing"
            ) from exc
        refused = _resolver_error(proc.stdout)
        if proc.returncode != 0:
            if refused is not None:
                # The child's own words, verbatim: the same rule the agent
                # applies to ``e2b-maint``'s refusals, so one spelling travels
                # all the way to the operator.
                raise LookupRefusal(refused)
            detail = (proc.stderr or proc.stdout or "").strip()
            raise LookupRefusal(
                f"the identity resolver refused (exit {proc.returncode}): "
                f"{detail}"
            )
        answer = _resolver_answer(proc.stdout)
        if answer is None:
            raise LookupRefusal(
                "the identity resolver answered something that is not one "
                f"uid/gid ({proc.stdout!r}): refusing"
            )
        return answer


def _package_root() -> Path:
    """The directory ``deploy``'s package lives in (the child's ``PYTHONPATH``)."""
    return Path(__file__).resolve().parents[2]


def _resolver_env(package_root: Path) -> dict[str, str]:
    """The resolver child's environment: enough to import this package, nothing else.

    Built rather than inherited on purpose: the child is another process on the
    same host, and face B's environment holds the CP→agent credential.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONPATH": str(package_root),
        "PYTHONUNBUFFERED": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }


def _resolver_answer(stdout: str) -> tuple[int, int] | None:
    """``(uid, gid)`` from the resolver's one-line answer, or ``None``."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    gid = payload.get("gid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        return None
    if not isinstance(gid, int) or isinstance(gid, bool) or gid <= 0:
        return None
    return uid, gid


def _resolver_error(stdout: str) -> str | None:
    """The named refusal in the resolver's answer, when it carries one."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) and error else None


def main(argv: list[str] | None = None) -> int:
    """``python -m deploy.c3_agent.lookup resolve-worker …`` (the resolver child).

    This entry point *is* the one thing face B cannot do itself: read another
    identity's ``/proc/<pid>/ns/pid``. It prints exactly one JSON line and exits
    0 (an identity) or ``RESOLVER_REFUSED`` (a named refusal), so the parent's
    judgement is the same strict one this package gives every other
    subprocess -- a half-printed or extra line is refused there.
    """
    parser = argparse.ArgumentParser(prog="deploy.c3_agent.lookup")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser(
        "resolve-worker", help="the kernel's uid/gid for a worker's pid namespace"
    )
    resolve.add_argument("--node-id", required=True)
    resolve.add_argument("--pid-namespace", required=True)
    resolve.add_argument("--claimed-uid", type=int, required=True)
    resolve.add_argument("--claimed-gid", type=int, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    identity = WorkerIdentity(
        node_id=args.node_id, pid_namespace=args.pid_namespace
    )
    try:
        uid, gid = ProcLookup().worker_uid_gid(
            identity, claimed=(args.claimed_uid, args.claimed_gid)
        )
    except LookupRefusal as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return RESOLVER_REFUSED
    print(json.dumps({"uid": uid, "gid": gid}), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - driven by the agent's subprocess
    sys.exit(main())
