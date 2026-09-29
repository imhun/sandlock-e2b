"""The agent's ``/proc`` reads: a slot's host pid, and the worker's own identity.

Two questions are answered here, both against the *host's* process table (the
agent is the only component with ``hostPID``/``pid: host``) and both by the same
discipline -- exact matches, named refusals, never a guess:

* **which host pid is this slot's child** (C3 Task 3, ruling D9.3) --
  :meth:`ProcLookup.host_pid`;
* **which uid/gid does this worker itself run as** (C3 Task 4, ruling D21
  option 2 as amended by ruling **D25**) -- :meth:`ProcLookup.worker_uid_gid`,
  used by the compose lane, whose face B has no pod spec to read the way the
  k8s lane does.

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

**The worker-identity half (D25).** The two faces ask different questions and
read different files, and that is deliberate:

* **face A** (uid 65534, the workers' own identity) resolves a *slot's* pid and
  may read ``ns/pid`` -- the kernel allows it because the identities match.
  That path is unchanged.
* **face B** (root, ``pid: host``, *no* ``CAP_SYS_PTRACE`` and no ability to
  change uid) cannot read another uid's ``ns/pid`` at all, so it resolves the
  worker's identity from files that are **world-readable**: it matches
  candidates by ``/proc/<pid>/cgroup`` (which carries the container id the
  runtime also puts in the worker's hostname) and reads the identity out of
  ``/proc/<pid>/status``. No capability is added, no uid is changed, and the
  slot path stays as narrow as it was.

Every failure is a named refusal: a pid that is gone is named as such (the name
the task brief fixes), a candidate in another namespace is named, a container
the agent can see no process of is named, and candidates whose kernel
identities *disagree* are refused as ambiguous -- while several candidates that
agree are fine, because one container's processes share one identity (the old
"exactly one process" rule is what squeezed the compose lanes to one live slot
per worker; see :meth:`ProcLookup.worker_uid_gid`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gateway_common.worker_identity import (
    container_cgroup_token,
    pod_cgroup_token,
    validate_container_id,
    validate_pid_namespace,
    validate_pod_uid,
)

logger = logging.getLogger(__name__)

#: The host's process table (the agent runs with ``hostPID: true``).
DEFAULT_PROC_ROOT = Path("/proc")

#: ``/proc/<pid>/status`` is a few hundred bytes; a file that is much larger is
#: not a status file and is refused rather than partially parsed.
_STATUS_MAX_BYTES = 65536

class LookupRefusal(Exception):
    """A named, fail-closed refusal: no host pid may be written from this."""


@dataclass(frozen=True)
class WorkerIdentity:
    """Who the control plane says the reported pid belongs to.

    ``pid_namespace`` is the worker's own reported identity and ``pod_uid`` is
    the k8s lane's independent proof. A lane that cannot supply
    ``pid_namespace`` cannot be resolved at all (see :meth:`ProcLookup.host_pid`).

    This is the **slot** path's identity (face A, the workers' own uid, where
    ``/proc/<pid>/ns/pid`` is readable). The file-operation path's anchor is
    :class:`WorkerAnchor` -- a different question, asked by a different face,
    with a different value (ruling D25).
    """

    node_id: str
    pid_namespace: str
    pod_uid: str | None = None


@dataclass(frozen=True)
class WorkerAnchor:
    """Which worker a *file-operation* claim is about (ruling D25).

    ``container_id`` is the worker's hostname (the runtime sets it to a prefix
    of the container id). The agent accepts a candidate process only when its
    host-side ``/proc/<pid>/cgroup`` *contains* this value -- and that file is
    world-readable, which is the whole point: the face that asks this question
    cannot read ``ns/pid`` of another uid at all.
    """

    node_id: str
    container_id: str


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

    def _is_container_init(self, pid: int) -> bool:
        """Is this process the *container's* own init (i.e. the worker itself)?

        The anchor alone is not enough on this lane, and the reason is measured
        (compose multinode, 2026-09-29): a sandbox's slot processes live in the
        **worker's own container cgroup** -- ``sandlock-supervise`` runs as the
        pooled uid (10000…) while the worker runs as 65534 -- so "processes
        whose cgroup carries the anchor" is a set with *more than one identity
        in it*, and picking an answer from it by agreement would refuse every
        legitimate operation (and picking the majority would be a guess).

        The worker's own process is the one the runtime started as the
        container's init: its ``NSpid`` chain, read from the host's pid
        namespace (this face runs ``pid: host``), is exactly ``[<host pid>, 1]``.
        The slot processes are ``[<host pid>, N]`` with ``N != 1`` (they were
        forked inside the worker's namespace) or ``[<host pid>, N, 1]`` /
        ``[<host pid>, N, M]`` (inside the sandbox's own pid namespace, which
        ``E2B_PID_NS=true`` gives them). Only the init matches, and that is
        exactly the process whose uid/gid the file operations must act as.

        A deployment that runs the worker with ``pid: host`` (no container pid
        namespace) has no such process: that is a **named refusal**, the same
        class of constraint as overriding ``hostname:``.
        """
        status = self._status(pid)
        if status is None:
            return False
        chain = _nspid_chain(status)
        return chain is not None and len(chain) == 2 and chain[-1] == 1

    def worker_uid_gid(
        self,
        anchor: WorkerAnchor,
        *,
        claimed: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        """The worker's **own** uid/gid, read out of the kernel (D25).

        **Why this does not read ``ns/pid``.** The file-operation path runs on
        face B: root **without** ``CAP_SYS_PTRACE`` (it shares ``pid: host``
        with the control plane, so that capability is deliberately absent).
        ``ptrace_may_access`` allows ``readlink /proc/<pid>/ns/pid`` only for a
        process of the same uid or with that capability, so face B sees nothing
        there -- and giving it the capability, or letting it change uid to the
        worker's, both cost more than the job needs. ``/proc/<pid>/cgroup`` and
        ``/proc/<pid>/status`` are **world-readable**, and the container id the
        runtime puts in the worker's hostname appears verbatim in its cgroup
        path, so this is the same lookup with none of that cost (ruling D25;
        measured on the compose lanes 2026-09-29: ``0::/../e4a98a0c528215e…``).

        **The predicate is: the container's init, then agreement among
        candidates** (ruling D4 as measured on this lane). Two steps, and the
        first one is not decoration:

        1. candidates are the processes whose cgroup carries the anchor **and
           which are the container's init** (:meth:`_is_container_init`). One
           container's *processes* do **not** all share one uid on this lane --
           a sandbox's ``sandlock-supervise`` runs as the pooled uid (10000…)
           inside the worker's container cgroup, while the worker runs as 65534
           (measured 2026-09-29: the same cgroup holds ``uid=65534 NSpid=[host,
           1]`` and ``uid=10000 NSpid=[host, 71]``) -- so the cgroup alone names
           a *container*, and the init names the *worker*;
        2. **zero** candidates ⇒ refuse by name (a recreated worker, a stale
           anchor, a deployment that overrode ``hostname:``, or one that gave
           the worker the host pid namespace -- never a guess); all candidates
           must agree on ``(uid, gid)``, and a disagreement ⇒ refuse by name.
           That is what stops a value the kernel did not report for a worker
           process from ever being adopted.

        Uniqueness of the *process set* is deliberately **not** required: a
        running slot, any ``docker exec``, and an unreaped child (a zombie still
        counts as a process) all made the old "exactly one process in the
        namespace" rule refuse -- which squeezed the compose lanes to one live
        slot per worker.

        Nothing about the worker's *claim* decides the answer: ``claimed`` is
        only compared against what the kernel says, so a claim the kernel does
        not confirm is a refusal, never a value handed to ``--worker``.

        Raises :class:`LookupRefusal` -- always by name, never with a guess --
        for an unusable anchor, for a container the agent can see no process of,
        for a status file the kernel's identity cannot be read from, for
        candidates that disagree, for a worker that runs as root, and for a
        claim the kernel does not confirm.
        """
        if not validate_container_id(anchor.container_id):
            raise LookupRefusal(
                f"worker {anchor.node_id} carries no usable container id "
                f"({anchor.container_id!r}): refusing to derive its own uid/gid "
                "from the kernel"
            )
        token = container_cgroup_token(anchor.container_id)
        candidates: list[int] = []
        for pid in self._iter_entries():
            cgroup = self._cgroup(pid)
            # ``None`` is "gone" or "unreadable"; neither is a process of this
            # container (the file is world-readable, so this is not a
            # permission question).
            if cgroup is not None and token in cgroup and self._is_container_init(pid):
                candidates.append(pid)
        if not candidates:
            raise LookupRefusal(
                f"worker {anchor.node_id}'s container ({anchor.container_id}) "
                "holds no process this agent can identify as the worker (the "
                "container's init): refusing to derive its own uid/gid from the "
                "kernel"
            )
        values: set[tuple[int, int]] = set()
        for pid in candidates:
            pair = self._uid_gid(pid)
            if pair is None:
                if (self._proc_root / str(pid)).exists():
                    raise LookupRefusal(
                        f"the kernel's uid/gid for one of worker "
                        f"{anchor.node_id}'s processes cannot be read: refusing"
                    )
                # Gone between the walk and the read: it is not a process of
                # this container any more, and a recycled pid cannot come back
                # inside the same container id.
                continue
            values.add(pair)
        if not values:
            raise LookupRefusal(
                f"worker {anchor.node_id}'s container ({anchor.container_id}) "
                "holds no process this agent can identify as the worker (the "
                "container's init): refusing to derive its own uid/gid from the "
                "kernel"
            )
        if len(values) != 1:
            raise LookupRefusal(
                f"worker {anchor.node_id}'s container ({anchor.container_id}) "
                f"holds processes whose kernel identities disagree "
                f"({sorted(values)}): refusing to derive one identity from them"
            )
        uid, gid = values.pop()
        if uid <= 0 or gid <= 0:
            # ``--worker`` writes this value and the ``--gid`` gate compares
            # against it; uid 0 is not a worker identity (the same rule the
            # control plane applies to a report, and ``maint.c``'s pool gate
            # applies to a pooled uid).
            raise LookupRefusal(
                f"worker {anchor.node_id}'s processes in "
                f"{anchor.container_id} run as uid/gid {(uid, gid)} "
                "according to the kernel: refusing (a worker may not run as root)"
            )
        if claimed is not None and (uid, gid) != claimed:
            raise LookupRefusal(
                f"worker {anchor.node_id} claims uid/gid {claimed}, but the "
                f"kernel says {(uid, gid)} for {anchor.container_id}: "
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
        self, anchor: WorkerAnchor, *, claimed: tuple[int, int]
    ) -> tuple[int, int]: ...


class ProcWorkerIdentityResolver:
    """The shipped resolver: in-process, against the host's own ``/proc``.

    Face B runs with ``pid: host``, so ``/proc`` *is* the host's process table
    and the two files this reads (``cgroup``, ``status``) are world-readable --
    no uid change, no capability, no child process (ruling D25). The lookup
    itself lives in :meth:`ProcLookup.worker_uid_gid`, which is a pure function
    of a ``/proc`` tree so the same code can be driven against a synthetic one.
    """

    def __init__(self, lookup: ProcLookup) -> None:
        self._lookup = lookup

    def resolve(
        self, anchor: WorkerAnchor, *, claimed: tuple[int, int]
    ) -> tuple[int, int]:
        return self._lookup.worker_uid_gid(anchor, claimed=claimed)


if __name__ == "__main__":  # pragma: no cover - there is nothing to run
    raise SystemExit("deploy.c3_agent.lookup is a library: the agent imports it")
