"""The C3 worker-identity values, validated by shape (shared by both sides).

Two opaque strings travel between the worker, the control plane and the per-node
agent, and both are *looked up* rather than parsed by either receiver:

* a **pid namespace identity** -- what ``readlink /proc/self/ns/pid`` prints
  (``pid:[4026532458]``). The worker reports its own at register/heartbeat; the
  agent compares it against the candidate's ``/proc/<pid>/ns/pid``. It is the
  one identity value that exists in *both* directions in *both* lanes: a
  compose worker cannot read its own container id (private cgroup namespace),
  but every worker can read its pid namespace (controller ruling D9.3).
* a **pod UID** -- the k8s lane's stronger proof, resolved by the control plane
  from the pod API rather than reported by the worker. The agent requires the
  candidate's host-side cgroup path to carry ``pod<uid>``.

Both are shape-checked here for the same reason ``gateway_common.paths`` checks
ids: the value is interpolated into a ``/proc`` lookup and a cgroup substring
match, and a value that is not of this shape is a *refusal*, never a looser
match. Shape is not the security property -- the exact-equality comparison in
``deploy/c3_agent/lookup.py`` is -- but a value that cannot be a namespace
inode can never be silently treated as one.
"""

from __future__ import annotations

import re

#: ``readlink /proc/self/ns/pid`` -- the nsfs inode, as the kernel prints it.
_PID_NAMESPACE_RE = re.compile(r"^pid:\[[1-9][0-9]{0,19}\]$")

#: A k8s pod UID is a lowercase UUID (``metav1.ObjectMeta.UID``).
_POD_UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def validate_pid_namespace(value: str) -> bool:
    """True when ``value`` is a pid namespace identity and nothing else."""
    return bool(value) and bool(_PID_NAMESPACE_RE.match(value))


def validate_pod_uid(value: str) -> bool:
    """True when ``value`` could be the pod UID the k8s API hands back."""
    return bool(value) and bool(_POD_UID_RE.match(value))


def pod_cgroup_token(pod_uid: str) -> str:
    """The substring a pod's processes carry in their host-side cgroup path.

    Measured on the k0s cluster (``docs/c3-privilege-relocation.md`` §14.2.7):
    ``0::/../../../burstable/pod6d3cdd7b-…/cfee67…``. Only
    :func:`validate_pod_uid`-shaped values reach here, so the token cannot be a
    short prefix that would match several pods at once.
    """
    return f"pod{pod_uid}"
