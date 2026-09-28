"""The container-pid → host-pid reverse lookup (C3 Task 3, ruling D9.3).

The worker forks the slot's child and reports the pid **it** knows; the agent,
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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

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


def missing_slot_pid_message(sandbox_id: str) -> str:
    """The name for "the child is not there any more" (task brief, verbatim).

    It is the one failure an operator sees when a slot's child crashes between
    the worker's report and the agent's write, so it is spelled once, here, and
    asserted verbatim by the tests on both sides of the hop.
    """
    return f"沙箱 {sandbox_id} 的槽位 pid 已不在"


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

    def present(self, host_pid: int) -> bool:
        """Is this host pid still in the process table?

        Asked *after* a failed write as well as before one: a child that ends
        between the lookup and the write must be named as gone rather than
        reported as an opaque refusal from the primitive.
        """
        return (self._proc_root / str(host_pid)).is_dir()

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

    def host_pid(
        self, container_pid: int, identity: WorkerIdentity, *, sandbox_id: str
    ) -> int:
        """The host pid of the worker's process known as ``container_pid``.

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
        return in_namespace[0]

    def _iter_entries(self) -> list[int]:
        """Every numeric pid in the tree, or none when it cannot be listed."""
        try:
            names = [entry.name for entry in self._proc_root.iterdir()]
        except OSError:
            return []
        return sorted(int(name) for name in names if name.isdigit())
