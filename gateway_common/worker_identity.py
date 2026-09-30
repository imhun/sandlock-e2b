"""The C3 worker-identity values, validated by shape (shared by both sides).

Three opaque strings travel between the worker, the control plane and the
per-node agent, and all three are *looked up* rather than parsed by either
receiver:

* a **pid namespace identity** -- what ``readlink /proc/self/ns/pid`` prints
  (``pid:[4026532458]``). The worker reports its own at register/heartbeat; the
  **slot** path's agent face (face A, uid 65534, the same identity as the
  worker's) compares it against a candidate's ``/proc/<pid>/ns/pid`` to turn the
  worker's container pid into a host pid (ruling D9.3). Narrow on purpose: it
  is only readable by that uid, which is exactly who runs the lookup.
* a **container identity** -- the worker's hostname, which the container runtime
  sets to (a prefix of) the container's id. Ruling **D25**: this is the
  **file-operation** path's anchor, and the reason it replaced the pid
  namespace there is that the file path runs on face B, which is root *without*
  ``CAP_SYS_PTRACE`` and cannot read ``ns/pid`` of another uid at all -- while
  ``/proc/<pid>/cgroup``, the value this anchor is matched against, is
  world-readable. The agent requires the candidate's host-side cgroup path to
  *contain* the reported value; zero candidates (including a deployment that
  overrode ``hostname:``) is a named refusal.
* a **pod UID** -- the k8s lane's stronger proof, resolved by the control plane
  from the pod API rather than reported by the worker. The agent requires the
  candidate's host-side cgroup path to carry ``pod<uid>``.

All three are shape-checked here for the same reason ``gateway_common.paths``
checks ids: the value is interpolated into a ``/proc`` lookup and a cgroup
substring match, and a value that is not of this shape is a *refusal*, never a
looser match. Shape is not the security property -- the comparison in
``c3_agent/lookup.py`` is -- but a value that cannot be a container id
can never be silently treated as one.
"""

from __future__ import annotations

import re

#: ``readlink /proc/self/ns/pid`` -- the nsfs inode, as the kernel prints it.
_PID_NAMESPACE_RE = re.compile(r"^pid:\[[1-9][0-9]{0,19}\]$")

#: A k8s pod UID is a lowercase UUID (``metav1.ObjectMeta.UID``).
_POD_UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: A container id as the runtimes spell it: lowercase hex. Docker's *hostname*
#: default is the first 12 characters of the full 64-character id, and both
#: forms appear in a cgroup path, so 12..64 is the range this validator has to
#: accept. The lower bound is what makes a substring match meaningful: 12 hex
#: characters is a 1-in-2**48 collision among the containers of one host, and
#: it is the shortest value the runtimes themselves use as an identity.
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")


def validate_pid_namespace(value: str) -> bool:
    """True when ``value`` is a pid namespace identity and nothing else."""
    return bool(value) and bool(_PID_NAMESPACE_RE.match(value))


def validate_pod_uid(value: str) -> bool:
    """True when ``value`` could be the pod UID the k8s API hands back."""
    return bool(value) and bool(_POD_UID_RE.match(value))


def validate_container_id(value: str) -> bool:
    """True when ``value`` could be a container id (or its hostname prefix).

    This is also the shape rule that turns "the deployment set ``hostname:`` to
    something that is not a container id" into a **named refusal** instead of a
    loose substring match: see :func:`container_cgroup_token`.
    """
    return bool(value) and bool(_CONTAINER_ID_RE.match(value))


def container_cgroup_token(container_id: str) -> str:
    """The substring a container's processes carry in their host-side cgroup.

    Measured on the compose lanes (2026-09-29, OrbStack): a worker's cgroup
    reads ``0::/../e4a98a0c528215e…`` -- the container id verbatim, with no
    decoration, which is why the token is the id itself (unlike the k8s lane's
    ``pod<uid>``). ``hostname`` is the same id's 12-character prefix, which is
    a substring of that path by construction.
    """
    return container_id


def pod_cgroup_token(pod_uid: str) -> str:
    """The substring a pod's processes carry in their host-side cgroup path.

    Measured on the k0s cluster (``docs/c3-privilege-relocation.md`` §14.2.7):
    ``0::/../../../burstable/pod6d3cdd7b-…/cfee67…``. Only
    :func:`validate_pod_uid`-shaped values reach here, so the token cannot be a
    short prefix that would match several pods at once.
    """
    return f"pod{pod_uid}"
