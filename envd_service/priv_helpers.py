"""The worker's privileged file steps, as a shape (C3).

One shape performs them now (2026-09-30, open-issues N52): the **per-node
agent**, asked through :mod:`envd_service.agent_fileops` as ``{sandbox_id,
op}`` and executed by the agent on the control plane's instruction (C3 hard
rules 1/3, §14.4). The file-capability binaries this module used to own
(``e2b-slot-spawn`` / ``e2b-maint``), the local ``exec`` transport and C1's
per-node ``socket`` daemon are **gone**; they live in git if a deployment ever
needs that shape back.

What is left here is the worker's own side of the contract:

* the shape switch -- ``E2B_PRIV_HELPER_TRANSPORT`` accepts ``auto``/``agent``
  (both mean the agent) and **refuses the retired ``exec``/``socket`` by
  name**, so a deployment that still names an old shape is told which one it
  named instead of being quietly given a different one;
* the management of a tree the worker owns: a sandbox tree is ``0770
  owner=<sandbox uid> group=<worker gid>`` (:data:`WORKSPACE_MODE`). The worker
  is the data-plane owner -- the files API, the watcher, the command-log
  writer, snapshots and the whole lifecycle run in this process and must keep
  working while a sandbox is paused, frozen or gone -- so its access is a
  *permission* (membership in the tree's group), not a capability borrowed for
  one syscall. :func:`remove_tree` and :func:`dir_size` are the two steps that
  rely on it;
* :func:`request_identity` -- the worker's *whole* contribution to the C3
  identity hand-off. It is deliberately not a privileged call: the only host
  it ever dials is the control plane's, and the only credential it ever sends
  is the worker's own internal key. There is no worker-to-agent channel to
  reach from here (hard rule 5);
* :func:`check_worker_identity_outside_pool` -- the startup guard that keeps a
  sandbox's uid/gid out of the worker's own trust boundary.

A root worker is untouched by any of this: root already has the capabilities,
and own identity keeps using its own privileged starter.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TRANSPORT_ENV = "E2B_PRIV_HELPER_TRANSPORT"

#: The shape switch values this worker still has. ``auto`` resolves to
#: ``agent``: the per-node agent is the only shape that performs a privileged
#: file step or grants a slot its identity (C3). ``exec`` (the worker running
#: the file-capability binaries itself) and C1's ``socket`` were retired on
#: 2026-09-30 (open-issues N52) and are refused **by name** rather than
#: quietly resolving to something else.
TRANSPORTS = ("auto", "agent")

#: Named, so the refusal can say what it replaced.
RETIRED_TRANSPORTS = ("exec", "socket")
#: The transport value that selects the C3 shape.
AGENT_TRANSPORT = "agent"


def _transport_setting() -> str:
    """``E2B_PRIV_HELPER_TRANSPORT`` (default ``auto``), validated by name."""
    value = str(os.environ.get(TRANSPORT_ENV, "auto") or "auto").lower()
    if value in RETIRED_TRANSPORTS:
        raise PrivHelperError(
            f"E2B_PRIV_HELPER_TRANSPORT={value!r} is retired (2026-09-30, "
            "open-issues N52): privileged file steps and slot identities are "
            "served by the per-node agent now; that shape lives in git if a "
            "deployment ever needs it back"
        )
    if value not in TRANSPORTS:
        raise PrivHelperError(
            f"E2B_PRIV_HELPER_TRANSPORT must be 'auto' or 'agent' (got {value!r})"
        )
    return value


#: Mode of a sandbox-owned directory (workspace root / volume slice).
#:
#: ``0770 owner=<sandbox uid> group=<worker gid>`` -- fix round 1 (裁定 c1).
#: The worker is the **data-plane owner** of every workspace: the files API,
#: the watcher, the command-log writer, snapshots and the whole lifecycle run
#: in the worker process and must keep working while a sandbox is paused,
#: frozen or gone. Its access requirement is therefore not a capability it
#: borrows for one syscall, it is a *permission* on the tree. Membership in the
#: group grants it; a sandbox (uid Y, gid Y, ``setgroups([])``) is never in
#: that group and the ``other`` bits are 0, so cross-sandbox isolation is still
#: a plain kernel DAC check.
WORKSPACE_MODE = 0o770


class PrivHelperError(RuntimeError):
    """A privileged-file-step request the worker must refuse."""


@dataclass(frozen=True)
class WalkEntry:
    """One line of the agent's ``walk`` answer."""

    kind: str
    uid: int
    gid: int
    mode: int
    size: int
    path: str

    @classmethod
    def parse(cls, line: str) -> "WalkEntry":
        kind, uid, gid, mode, size, path = line.split(" ", 5)
        return cls(
            kind=kind,
            uid=int(uid),
            gid=int(gid),
            mode=int(mode, 8),
            size=int(size),
            path=path,
        )


def request_identity(
    pid: int,
    sandbox_id: str,
    *,
    control_plane_url: str,
    node_id: str,
    internal_key: str,
    timeout_s: float = 5.0,
    transport=None,
) -> dict:
    """Report a slot child's ``{sandbox_id, pid}`` to the control plane.

    C3 Task 3 (ruling D9.1): this is the worker's *whole* contribution to the
    identity hand-off. The signature is the shape -- there is deliberately **no
    uid here and no uid on the wire**: the worker names the sandbox and the pid
    it sees, the control plane looks the uid up in its own records, and the
    agent writes it. A worker that could name a uid would be an identity
    authority, which is exactly what C3 removes.

    It lives beside the worker's file-step plumbing because that is where the
    identity hand-off already is, but it is *not* a privileged call: the only
    host it ever dials is the control plane's, and the only credential it ever
    sends is the worker's own internal key. There is no worker↔agent channel to
    reach from here (hard rule 5).

    Raises :class:`PrivHelperError` -- named, fail-closed -- when the control
    plane refuses the report or cannot be reached, so a slot that can never be
    granted an identity fails the create instead of polling forever.
    """
    import httpx

    url = (
        f"{str(control_plane_url).rstrip('/')}/internal/nodes/{node_id}"
        "/slot-identity"
    )
    try:
        with httpx.Client(timeout=float(timeout_s), transport=transport) as client:
            response = client.post(
                url,
                json={"sandbox_id": sandbox_id, "pid": int(pid)},
                headers={"X-Internal-Key": internal_key},
            )
    except httpx.HTTPError as exc:
        detail = str(exc) or type(exc).__name__
        raise PrivHelperError(
            "the control plane is unreachable for the slot-identity report of "
            f"sandbox {sandbox_id}: {detail}"
        ) from exc
    if response.status_code >= 300:
        detail = _error_detail(response)
        raise PrivHelperError(
            "the control plane refused the slot-identity report for sandbox "
            f"{sandbox_id} (HTTP {response.status_code}): {detail}"
        )
    try:
        answer = response.json()
    except ValueError as exc:
        raise PrivHelperError(
            "the control plane answered the slot-identity report for sandbox "
            f"{sandbox_id} with a non-JSON body"
        ) from exc
    return answer if isinstance(answer, dict) else {"answer": answer}


def request_cgroup_delegate(
    *,
    control_plane_url: str,
    node_id: str,
    internal_key: str,
    timeout_s: float = 5.0,
    transport=None,
) -> dict:
    """Ask the control plane for this worker's one-shot cgroup delegation.

    N83 phase 1 (shape W): the worker manages the sandbox cgroups nested under
    its own container cgroup, and the one privileged step is the handshake this
    call makes -- the control plane instructs the node's agent (face B) to hand
    the worker's **container cgroup directory** (plus ``cgroup.procs`` /
    ``cgroup.subtree_control``; never ``cpu.max``, and never ``cgroup.kill`` --
    the worker's kill lands on the ``sbx_<id>`` cgroups it creates itself, whose
    ``cgroup.kill`` the kernel already hands to it as their creator) to the
    worker's uid. The agent's ``chown`` is idempotent, so this request is too: a
    worker that re-asks (a restart, or a retry its caller owns) gets the same
    answer.

    Like :func:`request_identity` this dials the control plane and nothing else
    (hard rule 5), and the worker names **nothing** -- the node comes from the
    URL and the credential, and the anchor the agent locates the container by
    comes from the control plane's own records (k8s: the worker pod uid it read
    from the API; compose: the container id recorded at register/heartbeat).

    There is deliberately **no retry loop inside**: the caller owns retries, so
    a refusal is raised once, named, and never silently re-attempted behind it.
    Every failure is a :class:`PrivHelperError`.
    """
    import httpx

    url = (
        f"{str(control_plane_url).rstrip('/')}/internal/nodes/{node_id}"
        "/cgroup-delegate"
    )
    try:
        with httpx.Client(timeout=float(timeout_s), transport=transport) as client:
            response = client.post(url, headers={"X-Internal-Key": internal_key})
    except httpx.HTTPError as exc:
        detail = str(exc) or type(exc).__name__
        raise PrivHelperError(
            f"the control plane is unreachable for the cgroup delegation: {detail}"
        ) from exc
    if response.status_code >= 300:
        detail = _error_detail(response)
        raise PrivHelperError(
            "the control plane refused the cgroup delegation "
            f"(HTTP {response.status_code}): {detail}"
        )
    try:
        answer = response.json()
    except ValueError as exc:
        raise PrivHelperError(
            "the control plane answered the cgroup delegation with a non-JSON body"
        ) from exc
    return answer if isinstance(answer, dict) else {"answer": answer}


def _error_detail(response) -> str:
    """The refusal's own words: ``message`` (control plane) or ``error``."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(payload, dict):
        for key in ("message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text.strip()


def configure_priv_helpers(settings) -> None:
    """Wire this worker's privileged file steps -- the agent client, and only it.

    One shape is left (2026-09-30, open-issues N52): the per-node agent performs
    those steps, and this call installs the client that asks it
    (``envd_service.agent_fileops``). ``E2B_PRIV_HELPER_TRANSPORT`` is still
    read for one reason -- a deployment that names the retired ``exec`` or
    ``socket`` must be *refused by name* rather than quietly run a shape it did
    not ask for; the values it accepts (``auto``/``agent``) both mean "the
    agent".
    """
    from envd_service import agent_fileops

    _transport_setting()
    # Only wire the client when the deployment actually names the agent shape:
    # a shape that names neither keeps whatever it installed (the unit lanes
    # inject one), and the startup warning says which model it is running.
    if agent_fileops.enabled(settings):
        agent_fileops.configure(settings)
    return None


def file_steps_available(settings=None) -> bool:
    """Whether *some* shape can perform this worker's privileged file steps.

    One shape can: C3's agent client. The call sites gate on this rather than
    on "am I root" alone -- a root worker performs the steps itself, and a
    non-root worker needs the agent wired to reach them at all.
    """
    from envd_service import agent_fileops

    return agent_fileops.active() is not None


def remove_tree(path: str | Path, *, on_error: str = "ignore") -> None:
    """Delete a managed tree in the worker's own process.

    ``0770 owner=<sandbox uid> group=<worker gid>`` gives the worker group
    write on the tree it manages, so teardown is an ordinary ``rmtree``: no
    privileged step is involved (C3). ``on_error`` is the caller's choice --
    ``"raise"`` is the confirming shape the delete path uses (a tree that
    cannot be removed has to surface, not be silently left behind).
    """
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        if on_error != "ignore":
            raise
        logger.debug("cannot remove %s in-process: %s", path, exc)


def dir_size(path: str | Path) -> int | None:
    """Bytes under ``path`` for ``/metrics``; ``None`` means "unknown".

    The worker's own walk, which reaches a ``0770`` tenant workspace through
    its group membership. A tree it cannot read (a foreign-owned one, or a
    ``1777`` volume root full of files the worker does not own) answers
    ``None``: "unknown" must never be read as "empty".

    The quantity is *files plus directories*: each directory the walk visits
    contributes its allocated size (``st_blocks x 512``), which the file-only
    number never saw (N31: 2000 empty entries moved the platform number by 0
    bytes; measured 2026-09-21, that space is 512 bytes per directory on this
    NAS even at 2000 entries, while the directory's ``st_size`` -- 4096 to
    16384 -- is not what `du` reports). `DirLedger` maintains the same number
    incrementally and the byte equality between the two is a pinned contract
    (`tests/unit/test_dir_ledger.py`).

    The size comes from :func:`envd_service.runtime.brief_stat.entry_size`
    rather than ``os.path.getsize``: on NFS the latter flushes a file's dirty
    pages before answering (measured at 1405 ms for a file being written, where
    the size-only ``statx`` took 0.01 ms and returned the same number). This
    number is asked for every sandbox on a cadence, so it is the one place
    where paying the flush would be a permanent tax.
    """
    total = 0

    from envd_service.runtime.brief_stat import directory_cost, entry_size

    def _raise(exc: OSError) -> None:
        raise exc

    try:
        for root, _dirs, files in os.walk(path, onerror=_raise):
            try:
                total += directory_cost(root)
            except OSError:
                pass
            for name in files:
                try:
                    total += entry_size(os.path.join(root, name))
                except OSError:
                    continue
        return total
    except OSError:
        return None


def helpers_unavailable_reason(settings) -> str | None:
    """Why a non-root worker has no privileged file-step path, or ``None``.

    One shape can perform those steps now -- C3's agent -- so this is the line
    for a worker that names neither an agent nor a root identity: it keeps the
    in-process (E5.1) model, which means no per-sandbox host uid and no own-identity
    slots, and the caller logs it once at startup rather than letting the
    difference be discovered from a sandbox that behaves differently.
    """
    from envd_service import agent_fileops

    if agent_fileops.active() is not None or os.geteuid() == 0:
        return None
    return (
        "this worker has no privileged file-step path: no per-node agent is "
        "configured, so it keeps the in-process (E5.1) shape (no per-sandbox "
        "host uids, no own-identity slots)"
    )


def check_worker_identity_outside_pool(
    *, uid: int, gid: int, start: int, size: int
) -> None:
    """The pool must never hand a sandbox the worker's own uid or gid (c1).

    Fix round 1 makes the worker a *member* of every sandbox tree's group
    (``0770 owner=<sandbox uid> group=<worker gid>``). If a sandbox were
    allocated that same uid or gid it would be inside the worker's trust
    boundary -- it could read and write other sandboxes' workspaces -- so the
    configuration is refused by name at startup rather than shipping a silent
    hole. Checked for every worker shape (a root worker included): the group
    model -- and therefore the guard -- is what makes the shared tree safe.
    """
    end = start + size - 1
    for kind, value in (("uid", uid), ("gid", gid)):
        if start <= value <= end:
            raise PrivHelperError(
                f"the sandbox {kind} pool {start}..{end} contains the worker's "
                f"own {kind} ({value}): a sandbox would share the worker's "
                "identity and could read every other sandbox's 0770 workspace "
                "(the group model relies on the sandbox gid differing from the "
                f"worker's); move E2B_UID_POOL_START/SIZE off {value}"
            )
