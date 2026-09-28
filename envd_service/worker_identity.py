"""The worker's own identity, and the control-plane report that carries it.

Two things the worker has to say about itself, and neither is a secret it must
keep: an identity value it *observes* (its pid namespace) and a report it
*makes* (``{sandbox_id, pid}`` of a child it just forked).

The pid namespace is the value that makes the agent's container-pid → host-pid
lookup unambiguous when one host runs several workers (C3 Task 3, ruling D9.3):
a compose worker cannot read its own container id -- its cgroup namespace is
private, so ``/proc/self/cgroup`` is ``0::/`` -- but ``readlink
/proc/self/ns/pid`` is right there in every lane, and from *both* sides (the
agent sees the same inode for the worker's processes in its own ``/proc``).

The report is fire-and-forget in the sense that matters -- nothing "releases"
the child, it polls ``setresuid`` itself -- but it is not silent: a control
plane that refuses it (or cannot be reached) fails the create by name rather
than leaving a child polling for an identity that will never come.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from gateway_common.worker_identity import validate_pid_namespace

#: Where the worker reads its own identity from (Linux ``procfs``).
DEFAULT_PROC_ROOT = Path("/proc")


def worker_pid_namespace(proc_root: Path | str = DEFAULT_PROC_ROOT) -> str | None:
    """``readlink /proc/self/ns/pid`` -- the worker's namespace identity.

    ``None`` when this platform has no such link (a macOS dev box, an embedder)
    or when what it points at is not a pid namespace identity. The control plane
    stores whatever arrives and refuses *grants* without one, so an absent value
    is a named refusal downstream, never a weaker match here.
    """
    try:
        value = os.readlink(Path(proc_root) / "self" / "ns" / "pid")
    except OSError:
        return None
    return value if validate_pid_namespace(value) else None


def build_identity_reporter(
    settings,
    *,
    control_plane_url: str | None = None,
    node_id: str | None = None,
) -> Callable[[str, int], Any] | None:
    """The ``(sandbox_id, pid) -> answer`` reporter, or ``None`` when unwired.

    ``None`` is the honest answer for a worker that does not know where its
    control plane is (or which node it is): route B then declines the
    identity-grant path by name instead of forking a child nobody will grant.
    """
    url = (
        control_plane_url
        if control_plane_url is not None
        else getattr(settings, "control_plane_url", None)
        or os.getenv("E2B_CONTROL_PLANE_URL", "")
    )
    node = (
        node_id
        if node_id is not None
        else getattr(settings, "node_id", None) or os.getenv("E2B_NODE_ID", "")
    )
    if not url or not node:
        return None
    internal_key = getattr(settings, "internal_api_key", "") or ""
    timeout = float(getattr(settings, "slot_identity_timeout_s", 5.0) or 5.0)

    def _report(sandbox_id: str, pid: int) -> dict[str, Any]:
        from envd_service.priv_helpers import request_identity

        return request_identity(
            pid,
            sandbox_id,
            control_plane_url=str(url),
            node_id=str(node),
            internal_key=internal_key,
            timeout_s=timeout,
        )

    return _report
