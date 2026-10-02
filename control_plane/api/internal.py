"""Internal APIs used by worker agents and the envd gateway.

C3 Task 2 / N49 closes the "self-declared node" hole here. ``X-Internal-Key``
used to be a fleet-wide bearer credential and the node identity came from the
URL/body, so any component holding the key could speak for any node -- and C3
turns that into privilege amplification (the control plane then instructs the
privileged agent). Every handler is now in exactly one of two named classes:

**Node-scoped** (a worker speaks for *itself*; the credential must be bound to
the node it claims, the object must be the control plane's own record for that
node, and the request must come from the node's expected address):

* ``POST /internal/nodes/register`` -- claim is ``body["nodeID"]``
* ``POST /internal/nodes/{node_id}/heartbeat``
* ``GET  /internal/nodes/{node_id}/sandboxes``
* ``POST /internal/nodes/{node_id}/reconcile``
* ``POST /internal/nodes/{node_id}/slot-identity`` -- the worker reports the
  ``{sandbox_id, pid}`` of a slot child it just forked (C3 Task 3, ruling D9.1);
  the control plane answers by instructing the node's agent, **with the uid and
  the worker identity taken from its own records**

**Fleet/ops scope** (the caller is not a node and has no node identity to bind:
the autoscaler, the envd gateway, or an operator). These keep the shared key
and are *explicitly* exempt, by name -- see :func:`_require_fleet_key`. This is
pre-existing behavior the task does not change; the exemption is named here and
in ``docs/open-issues.md`` row N49 so it cannot be mistaken for coverage:

* ``GET  /internal/routes/{sandbox_id}`` (gateway route lookup)
* ``GET  /internal/fleet/sandboxes`` (a worker's fleet-wide ownership sweep:
  "does anyone anywhere own this tree?" -- not "may I act for node X")
* ``GET  /internal/nodes``, ``GET /internal/fleet/metrics`` (autoscaler/ops)
* ``POST /internal/nodes/{node_id}/drain|undrain`` (operator action *on* a node)
* ``GET  /internal/tenants`` (ops reconciliation)

The three-step validation itself lives in :func:`_require_node_identity`: ①
credential → node (``control_plane.auth.node_id_for_key``); ② the request's
claim must equal it; ③ the objects are the control plane's own records, scoped
by that node (done by the registry calls the handler already makes); plus the
source-IP second factor, whose expected value comes from a resolver -- never
from the request.

Two keys are *not* interchangeable here, and the difference is the whole point
of N49:

* a **node-bound** key (``E2B_INTERNAL_NODE_KEYS``) fixes the identity before
  the request is read: the claim must equal it (403 otherwise), and the request
  must then come from that node's expected address.
* a **fleet** key (the shared ``E2B_INTERNAL_API_KEY``) cannot say *which* node
  is calling. It is accepted on a node-scoped handler only when the claim
  **resolves to an expected address** and the request comes from it -- i.e. only
  where the network position can vouch for the node. A claim that resolves
  nowhere is **refused** (503, named), never taken on faith, and the register
  address is never ``body["address"]``. This keeps the unbound key working
  where the resolver is configured (every shipped shape sets
  ``E2B_NODE_ADDRESS_MODE``) while closing the "claim any node from anywhere"
  hole the review found.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Header, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import node_id_for_key, verify_agent_key, verify_internal_key
from control_plane.c3_agent_client import AgentClientError, AgentTarget
from control_plane import file_ops
from control_plane import fleet_view
from control_plane import self_heal
from control_plane.node_address import NodeEndpoint
from control_plane.registry.manager import UnknownSandboxError
from gateway_common.paths import (
    is_reserved_platform_namespace,
    validate_node_id,
    validate_sandbox_id,
)
from gateway_common.worker_identity import (
    validate_container_id,
    validate_pid_namespace,
)

router = APIRouter()

logger = logging.getLogger(__name__)

#: Fleet keys already reported as unbound, so the degradation is named once per
#: key rather than on every 5-second heartbeat.
_fleet_keys_reported: set[str] = set()

#: Nodes whose expected address could not be determined, already reported. A
#: fleet-wide resolver/RBAC outage is one cause for *every* node, so the
#: refusal (unchanged, 503) must not flood the log at heartbeat cadence.
_unresolvable_nodes_reported: set[str] = set()

#: ``(node, reason)`` pairs whose worker-identity mismatch/unverifiability has
#: already been reported, so one mispinned fleet does not print a line every
#: five seconds per worker (the same once-per-node discipline as the two sets
#: above). A node whose identity is verified again is dropped from it, so a
#: later regression is reported again.
_unverified_identity_reported: set[tuple[str, str]] = set()


def _require_internal_key(request: Request) -> str:
    """The credential half: a valid ``X-Internal-Key``, else 401."""
    settings = request.app.state.settings
    key = request.headers.get("X-Internal-Key")
    if not verify_internal_key(key, settings):
        raise OfficialError(401, "Unauthorized")
    return key or ""


def _require_fleet_key(request: Request) -> None:
    """Editorial name for the *non-node-scoped* surfaces (see module docstring).

    The autoscaler, the gateway and an operator are not nodes: they have no node
    identity to bind, so these endpoints authenticate with the shared key and
    are exempt from the node identity/IP checks **by name** -- called out here
    and in the module docstring, never silently. Any valid internal credential
    is accepted (a node-bound key included: it is a valid internal key), because
    the surface acts for no node and so has nothing to bind an identity to.
    """
    _require_internal_key(request)


def _worker_pid_namespace(body: dict[str, Any]) -> str | None:
    """The worker's reported pid namespace identity, or a named refusal.

    C3 Task 3 / ruling D9.3: the value is compared against ``/proc/<pid>/ns/pid``
    links and against a cgroup path, so it is shape-checked before it is stored
    -- a value that cannot be a namespace identity is refused here rather than
    compared loosely later. Absent (an older worker during a rollout) is *not*
    an error: the record simply keeps no identity, and the slot-identity
    endpoint then refuses those grants by name.
    """
    value = body.get("pidNamespace")
    if value is None:
        return None
    if not isinstance(value, str) or not validate_pid_namespace(value):
        raise OfficialError(
            400,
            "pidNamespace must be a pid namespace identity such as "
            "'pid:[4026532458]'",
        )
    return value


def _worker_container_id(body: dict[str, Any]) -> str | None:
    """The worker's reported container identity, or a named refusal (D25).

    The value is matched as a *substring* of a candidate's host-side cgroup
    path, so it is shape-checked before it is stored: a value that cannot be a
    container id (12..64 lowercase hex characters -- Docker's hostname is the
    12-character prefix of the id) is refused here rather than matching loosely
    later. Absent is *not* an error (an older worker during a rollout, or a
    platform without a container identity): the record keeps none, and the
    file-operation endpoint then refuses those instructions by name.
    """
    value = body.get("containerID")
    if value is None:
        return None
    if not isinstance(value, str) or not validate_container_id(value):
        raise OfficialError(
            400,
            "containerID must be a container id such as the runtime's default "
            "hostname (12..64 lowercase hex characters)",
        )
    return value


def _worker_identity_fields(body: dict[str, Any]) -> tuple[int | None, int | None]:
    """The worker's reported ``workerUID`` / ``workerGID``, or a named refusal.

    C3 Task 4: face B's file operations act *as* the worker in two places
    (``chown --worker`` and the group a sandbox tree is handed to), so the
    control plane has to know the identity -- and it takes it from its own node
    record, never from the file-op request itself (hard rule 3). Both halves
    are reported together or not at all: half an identity is not one.
    """
    uid = body.get("workerUID")
    gid = body.get("workerGID")
    if uid is None and gid is None:
        return None, None
    for name, value in (("workerUID", uid), ("workerGID", gid)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            # The shape rule and the *reason* are one message: this is what a
            # worker that runs as root reports (``os.geteuid() == 0``), and the
            # deployment has to be told that the condition -- not the spelling
            # of one field -- is what it must fix (C3 Task 4 third review, m-1).
            raise OfficialError(
                400,
                f"{name} must be a positive integer: a worker may not run as "
                "root (uid 0) or report a non-identity, because the group a "
                "sandbox tree is handed to and the `--worker` form both mean "
                "*this* worker's own non-zero identity",
            )
    return uid, gid


def _verified_worker_identity(
    request: Request, node_id: str, claimed: tuple[int | None, int | None]
) -> tuple[int | None, int | None]:
    """The worker identity to **store**, or ``(None, None)`` -- never the claim.

    C3 Task 4's fourth review (②): in C1 the ``--worker`` identity came from
    ``SO_PEERCRED`` and could not be forged; the first cut of the agent made it
    worker-**reported**, so a compromised worker could have a tree -- or the
    credential-bearing slot policy -- handed to another tenant's uid. The value
    that reaches the agent now has to come from a trusted source
    (:mod:`control_plane.worker_identity_source`): the worker pod's own
    ``securityContext`` in k8s, which the worker cannot edit. A claim that does
    not match it, or a shape with no source at all, stores **nothing** and says
    so; the node stays joined and every operation that needs the identity
    refuses by name (the fail-closed posture the ruling prescribes).

    The compose lane is the ruling's second shape (D21 option 2), and it takes
    the other branch below: this control plane cannot verify a report there, so
    it stores the report **as a claim** and carries the worker's pid namespace
    with every instruction, for the agent to confirm against the kernel. That is
    not "trusting the report" -- the value is never used without the kernel's
    answer (:meth:`_worker_identity_anchor`) -- and it is what lets the compose
    lanes complete a create at all (the ownership hand-over is their first
    privileged step).
    """
    source = getattr(request.app.state, "worker_identity_source", None)
    configured = bool(getattr(source, "configured", source is not None))
    if getattr(source, "kernel_verified", False):
        if claimed[0] is None or claimed[1] is None:
            return None, None
        _unverified_identity_reported.discard((node_id, "no-source"))
        _unverified_identity_reported.discard((node_id, "no-pin"))
        logger.debug(
            "internal API: node %s's worker identity %s is recorded as a claim "
            "this shape cannot verify (the agent confirms it against the "
            "kernel); the pid namespace anchor travels with every instruction",
            node_id,
            claimed,
        )
        return claimed[0], claimed[1]
    trusted = source.identity_for(node_id) if source is not None else None
    claimed_uid, claimed_gid = claimed
    if claimed_uid is None and claimed_gid is None:
        # An older worker reports nothing; the trusted answer (when there is
        # one) is still the deployment's fact and is what gets stored.
        if trusted is not None:
            _unverified_identity_reported.discard((node_id, "no-source"))
            _unverified_identity_reported.discard((node_id, "no-pin"))
        return trusted if trusted is not None else (None, None)
    if trusted is not None and trusted == (claimed_uid, claimed_gid):
        # Verified: forget any earlier complaint about this node, so a later
        # regression is reported again.
        _unverified_identity_reported.discard((node_id, "no-source"))
        _unverified_identity_reported.discard((node_id, "no-pin"))
        return trusted
    # Unverifiable from here on. Two different operator problems, and the log
    # says which -- once per node each, because the heartbeat runs every few
    # seconds (the ``_unresolvable_nodes_reported`` discipline).
    if trusted is None and not configured:
        reason = "no-source"
        message = (
            "internal API: node %s reported worker identity %s, but this shape "
            "has no trusted source for it (only the k8s lane can verify a "
            "report): recording no identity -- the file operations that need "
            "one will refuse by name"
        )
        arguments: tuple = (node_id, (claimed_uid, claimed_gid))
    elif trusted is None:
        reason = "no-pin"
        message = (
            "internal API: node %s reported worker identity %s, but its pod "
            "spec pins no runAsUser/runAsGroup (the worker relies on the "
            "image's USER, which the API cannot read): recording no identity "
            "-- pin both in the worker manifest, or the file operations that "
            "need one will refuse by name"
        )
        arguments = (node_id, (claimed_uid, claimed_gid))
    else:
        reason = "mismatch"
        message = (
            "internal API: node %s reported worker identity %s, but the "
            "deployment pins %s: refusing the reported value (a worker does not "
            "name the identity its privileged steps act as)"
        )
        arguments = (node_id, (claimed_uid, claimed_gid), trusted)
    if (node_id, reason) not in _unverified_identity_reported:
        _unverified_identity_reported.add((node_id, reason))
        logger.warning(message, *arguments)
    return None, None


def _worker_identity_anchor(request: Request, node, node_id: str) -> str | None:
    """The container identity the agent must confirm a claim against, or ``None``.

    Ruling **D25**: the anchor is the worker's **container id** (what its
    hostname carries and what its host-side cgroup path contains), not its pid
    namespace -- face B is root without ``CAP_SYS_PTRACE`` and cannot read
    another uid's ``/proc/<pid>/ns/pid`` at all, while ``/proc/<pid>/cgroup`` is
    world-readable. The file-operation path therefore confirms the claim against
    a value face B can actually read, with no capability and no uid change.

    ``None`` has two readings, and both are correct: this deployment's shape
    verifies the identity itself (k8s -- no anchor travels), or the shape defers
    to the kernel but the node has recorded no container id to defer *with*
    (a platform without one, or a compose stack that overrode ``hostname:``).
    The second is a named refusal, not "send it anyway": an instruction the
    agent cannot confirm is exactly the one that must not be sent.

    The value is the control plane's own record (:meth:`_worker_container_id`
    shape-checked it at register/heartbeat) and never the request's, so a
    file-op body still cannot name anything.
    """
    source = getattr(request.app.state, "worker_identity_source", None)
    if not getattr(source, "kernel_verified", False):
        return None
    anchor = getattr(node, "container_id", None)
    if not validate_container_id(anchor or ""):
        raise OfficialError(
            503,
            f"node {node_id} has reported no container id for the agent to "
            "confirm its worker identity against (a C3 worker must keep the "
            "runtime's hostname): refusing to instruct the agent",
        )
    return anchor


def _resolve_node(request: Request, node_id: str) -> NodeEndpoint | None:
    """The expected endpoint for ``node_id``, or ``None`` when unknown.

    ``None`` is never a guess and never the observed peer address (D4): the
    caller **fails closed** and names the node.
    """
    resolver = getattr(request.app.state, "node_address_resolver", None)
    if resolver is None:
        return None
    return resolver.resolve(node_id)


def _enforce_source_ip(request: Request, node_id: str, endpoint: NodeEndpoint) -> None:
    """Second factor: the request must arrive from the node's own address.

    This is the layer that defends against a *stolen* key (the credential layer
    only defends against no key at all, §11.1 item 9). The refusal names both
    the observed and the expected value so a binding drift ("this node suddenly
    rejects everything") is visible in one line.
    """
    observed = request.client.host if request.client else None
    expected_ips = endpoint.source_ips
    if observed not in expected_ips:
        expected_text = (
            expected_ips[0]
            if len(expected_ips) == 1
            else "one of " + ", ".join(expected_ips)
        )
        logger.warning(
            "internal API: request for node %s came from %s, expected %s; "
            "refusing (source-IP second factor)",
            node_id,
            observed,
            expected_text,
        )
        raise OfficialError(
            403,
            f"request for node {node_id} came from {observed}, expected {expected_text}",
        )


def _require_agent_identity(request: Request, claimed_host: str) -> AgentTarget:
    """The agent behind a report: credential, host claim, and source IP (Task 6).

    The agent reports on its own behalf, so the identity path is the agent's,
    not a worker's (D12 puts the agent's identity on the **host**):

    1. the credential is ``E2B_C3_AGENT_TOKEN`` -- the agent's own, accepted on
       the agent's surfaces only (``control_plane.auth.verify_agent_key``, never
       part of the fleet-wide internal key list);
    2. the claim in the URL must be the host the control plane can *find an
       agent on*: the lookup goes to the agent pods themselves (k8s) or the
       configured agent name (compose), never to a worker -- a worker that
       crashed and never came back must not be able to make its node
       unaddressable, and a claim nothing resolves is a named 503;
    3. the source IP must be one the same lookup named (§11.1 item 9's layer 4:
       the credential stops "no key", the network position stops "stolen key").
       A lane that cannot state an expected address refuses here rather than
       accepting a claim it cannot verify.
    """
    settings = request.app.state.settings
    if not (getattr(settings, "c3_agent_token", "") or ""):
        raise OfficialError(
            503,
            "this control plane names no agent credential "
            "(E2B_C3_AGENT_TOKEN): refusing agent reports",
        )
    if not verify_agent_key(request.headers.get("X-Internal-Key"), settings):
        raise OfficialError(401, "Unauthorized")
    if not validate_node_id(claimed_host):
        raise OfficialError(
            400, f"an agent report must name its node (got {claimed_host!r})"
        )
    client = getattr(request.app.state, "c3_agent_client", None)
    if client is None:
        raise OfficialError(
            503,
            "this control plane has no C3 agent client configured: refusing "
            "agent reports",
        )
    try:
        target = client.resolve_agent(claimed_host)
    except AgentClientError as exc:
        raise OfficialError(exc.status_code, str(exc)) from exc
    observed = request.client.host if request.client else None
    expected_ips = target.source_ips
    if observed not in expected_ips:
        if len(expected_ips) == 1:
            expected_text = expected_ips[0]
        elif expected_ips:
            expected_text = "one of " + ", ".join(expected_ips)
        else:
            # The lane could not state an address: "cannot check" is a refusal
            # here, never "so accept it" (the ruling's fail-closed sharpening).
            expected_text = "an address this control plane can resolve for the agent"
        logger.warning(
            "internal API: a report for agent %s came from %s, expected %s; "
            "refusing (source-IP second factor)",
            claimed_host,
            observed,
            expected_text,
        )
        raise OfficialError(
            403,
            f"a report for agent {claimed_host} came from {observed}, "
            f"expected {expected_text}",
        )
    return target


def _require_node_identity(
    request: Request, claimed_node_id: str | None
) -> tuple[str, NodeEndpoint]:
    """Steps ①② + the source-IP layer for a node-scoped handler.

    Returns the node the request acts for (the credential-derived one for a
    bound key, the declared one otherwise) and its **resolved** endpoint. Both
    failure modes are named: a bound key whose claim disagrees is a 403, and any
    claim that cannot be resolved to an expected address -- bound or not -- is a
    503. There is deliberately no "unresolvable, so take the request's word"
    branch (that was N49).
    """
    key = _require_internal_key(request)
    settings = request.app.state.settings
    derived = node_id_for_key(key, settings)
    if derived is not None and claimed_node_id is not None and claimed_node_id != derived:
        logger.warning(
            "internal API: X-Internal-Key is bound to node %s but the request "
            "claims node %s; refusing",
            derived,
            claimed_node_id,
        )
        raise OfficialError(
            403,
            f"X-Internal-Key is bound to node {derived}; "
            f"request claims node {claimed_node_id}",
        )
    node_id = derived if derived is not None else claimed_node_id
    if derived is None:
        # Step ① cannot bind this key to a node, so the network position is the
        # only thing that can vouch for the claim. Named once per key: the
        # credential half is off here, and an operator greps for this line.
        if key not in _fleet_keys_reported:
            _fleet_keys_reported.add(key)
            logger.warning(
                "internal API: X-Internal-Key is a fleet key with no node binding "
                "(E2B_INTERNAL_NODE_KEYS): the credential cannot say which node is "
                "calling, so a node-scoped request is accepted only from the claim's "
                "resolved address; bind keys to nodes to restore step ①",
            )
        if node_id is None:
            # Only ``register`` can get here (the URL carries the node for the
            # other three): a fleet key must say which node it is registering
            # as, because there is nothing else to check the claim against.
            logger.warning(
                "internal API: register with a fleet key carried no nodeID; "
                "refusing (nothing to verify the identity against)",
            )
            raise OfficialError(
                403,
                "a node-scoped request with a fleet key must declare the node "
                "it acts for (register: body.nodeID)",
            )
    endpoint = _resolve_node(request, node_id)
    if endpoint is None:
        # Fail closed: "I cannot determine where this node is" must never mean
        # "so take the caller's word (or the body's address)". The line below is
        # the alarm for a fleet-wide, self-inflicted refusal (the pod API / DNS
        # is down, or the node id does not exist) -- named once per node, not
        # once per heartbeat.
        if node_id not in _unresolvable_nodes_reported:
            _unresolvable_nodes_reported.add(node_id)
            logger.warning(
                "internal API: cannot determine the expected address for node %s; "
                "refusing (fail closed)",
                node_id,
            )
        raise OfficialError(
            503, f"cannot determine the expected address for node {node_id}"
        )
    _enforce_source_ip(request, node_id, endpoint)
    return node_id, endpoint


@router.post("/internal/nodes/register")
async def register_node(request: Request) -> dict[str, Any]:
    # The credential first (401 before any body error), then the claim in the
    # body: for a node-bound key the node id comes from the credential, not the
    # request (steps ①②), and the address the control plane dials is the
    # resolver's, never ``body["address"]`` (N49 / D4).
    _require_internal_key(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    node_id, endpoint = _require_node_identity(request, body.get("nodeID"))
    address = body.get("address")
    if address and address != endpoint.address:
        # A self-declared address that disagrees is a rewrite attempt (or a
        # stale worker): the resolver's answer is the only one ever dialed.
        logger.warning(
            "internal API: node %s registered the address %s but the "
            "resolver expects %s; using the resolver's",
            node_id,
            address,
            endpoint.address,
        )
    address = endpoint.address
    pid_namespace = _worker_pid_namespace(body)
    container_id = _worker_container_id(body)
    worker_uid, worker_gid = _verified_worker_identity(
        request, node_id, _worker_identity_fields(body)
    )
    record = request.app.state.nodes.register(
        node_id=node_id,
        address=address,
        total_memory_mb=int(body.get("totalMemoryMB", 0)),
        total_cpu_percent=int(body.get("totalCPUPercent", 0)),
        total_disk_mb=int(body.get("totalDiskMB", 0)),
        total_processes=int(body.get("totalProcesses", 0)),
        images=body.get("images") or [],
        labels=body.get("labels") or {},
        pid_namespace=pid_namespace,
        container_id=container_id,
        worker_uid=worker_uid,
        worker_gid=worker_gid,
    )
    _rebuild_node_reservations(request, record)
    return {"nodeID": record.node_id}


def _rebuild_node_reservations(request: Request, record) -> None:
    """Restore a re-registering node's reserved quota from sandbox records.

    Sandbox records persist in Redis across control-plane restarts but the
    node registry's reserved fields are in-memory; on re-registration the
    reservations start at zero, so fleet utilization would be misreported
    (and nodes over-committed). Aggregating the node's records here keeps the
    two views consistent.

    N59: "the two views" is exactly the problem -- the *shared* ledger
    (``e2b:node:quota:<node>``) is the copy every replica checks, and it had no
    reconciliation path at all, so a leaked reservation outlived every restart
    until an operator deleted the key by hand. Both halves are now rebuilt from
    this aggregate in one call. The aggregate is authoritative *here* because
    the node's runtime has just been (re)created: every reservation serving a
    sandbox on it has a record. A correction is named in the log rather than
    applied quietly -- a downward one (the leak direction) is the one worth
    reading.

    Named residual: a create placed against this node in the instant between
    "it looked healthy" and this rebuild has a reservation but no record yet,
    and this drops it (over-sell by that one sandbox's dims). The window needs a
    node that re-registers exactly while a create is being placed on it, and the
    alternative -- a ledger that drifts up forever and jams placement -- is the
    failure this heals.
    """
    dims = {"memory": 0, "cpu": 0, "disk": 0, "processes": 0}
    for sandbox in request.app.state.registry.list():
        # E9.2: a paused sandbox gave its reservation back, so it must not be
        # re-booked here (that would strand capacity forever).
        if sandbox.node_id != record.node_id or sandbox.quota_released:
            continue
        dims["memory"] += sandbox.memory_mb
        dims["cpu"] += sandbox.cpu_count * 100
        dims["disk"] += sandbox.disk_size_mb
        dims["processes"] += sandbox.max_processes
    request.app.state.nodes.set_reserved(
        record.node_id,
        memory_mb=dims["memory"],
        cpu_percent=dims["cpu"],
        disk_mb=dims["disk"],
        processes=dims["processes"],
    )
    deltas = request.app.state.nodes.reconcile_quota_ledger(
        record.node_id,
        memory_mb=dims["memory"],
        cpu_percent=dims["cpu"],
        disk_mb=dims["disk"],
        processes=dims["processes"],
    )
    if deltas:
        logger.warning(
            "node %s re-registered: the shared quota ledger did not match the "
            "sandbox records and was reconciled to them (deltas: %s; negative "
            "values are the leaked reservations this heals)",
            record.node_id,
            deltas,
        )


@router.post("/internal/nodes/{node_id}/heartbeat")
async def node_heartbeat(node_id: str, request: Request) -> Response:
    _require_node_identity(request, node_id)
    body: dict[str, Any] = {}
    raw = await request.body()
    if raw:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise OfficialError(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise OfficialError(400, "Heartbeat body must be a JSON object")
    record = request.app.state.nodes.heartbeat(node_id)
    if record is None:
        raise OfficialError(404, f"Node {node_id} not found")
    # C3 Task 3: the worker's identity travels with every heartbeat, so a
    # restarted container (new pid namespace inode, same node id) is never
    # pinned to the namespace it had before -- and an older worker that reports
    # nothing leaves the stored value alone.
    pid_namespace = _worker_pid_namespace(body)
    if pid_namespace is not None:
        record.pid_namespace = pid_namespace
    # D25: the file-operation path's anchor travels the same way (a recreated
    # container is a new id under the same node id).
    container_id = _worker_container_id(body)
    if container_id is not None:
        record.container_id = container_id
    # C3 Task 4: same treatment for the worker's own uid/gid -- refreshed on
    # every heartbeat (a restart can change them under a k8s ``runAsGroup``),
    # and left alone when an older worker reports nothing.
    worker_uid, worker_gid = _worker_identity_fields(body)
    verified_uid, verified_gid = _verified_worker_identity(
        request, node_id, (worker_uid, worker_gid)
    )
    if verified_uid is not None and verified_gid is not None:
        record.worker_uid = verified_uid
        record.worker_gid = verified_gid
    record.update_usage(
        used_disk_mb=body.get("diskUsedMB"),
        disk_total_mb=body.get("diskTotalMB"),
        quota_over_limit=body.get("quotaOverLimit"),
        quota_near_limit=body.get("quotaNearLimit"),
        quota_over_limit_count=body.get("quotaOverLimitCount"),
        quota_near_limit_count=body.get("quotaNearLimitCount"),
        disk_warn_count=body.get("diskWarnCount"),
        disk_error_count=body.get("diskErrorCount"),
        mcp_ports_in_use=body.get("mcpPortsInUse"),
        mcp_ports_capacity=body.get("mcpPortsCapacity"),
        platform_disk_used_mb=body.get("platformDiskUsedMB"),
        platform_disk_budget_mb=body.get("platformDiskBudgetMB"),
    )
    # F11 step 1: the usage numbers ride the same shared view the health and the
    # reservations do, so every replica places work against what the worker just
    # reported instead of against its own stale copy.
    request.app.state.nodes.publish(record)
    activity = body.get("sandboxActivity")
    if activity is not None and not isinstance(activity, dict):
        raise OfficialError(400, "sandboxActivity must be a JSON object")
    if isinstance(activity, dict) and activity:
        # E9.1: the worker is the only observer of in-sandbox traffic, so its
        # report is what makes idle detection (and eviction) possible.
        request.app.state.registry.apply_activity_report(node_id, activity)
    disk_usage = body.get("sandboxDiskUsage")
    if disk_usage is not None and not isinstance(disk_usage, dict):
        raise OfficialError(400, "sandboxDiskUsage must be a JSON object")
    if isinstance(disk_usage, dict) and disk_usage:
        await _enforce_disk_reports(request, node_id, disk_usage)
    return Response(status_code=204)


async def _enforce_disk_reports(
    request: Request, node_id: str, reports: dict[str, Any]
) -> None:
    """Turn a worker's measured tree sizes into accounting (N25/L2b).

    The worker measures because it owns the mount; the control plane records
    because it owns state. Being over budget is **not** a freeze: the worker
    already blocks writes (a zero file-size ceiling, plus `ENOSPC` for the
    operations a ceiling cannot reach), and the owner keeps the reads, the exec
    and -- above all -- the deletes that bring the sandbox back inside. So this
    reports the crossing and nothing else: no pause, no reservation change.
    """
    for record in request.app.state.registry.enforce_disk_budget(reports):
        # `record.workspace_disk_used_bytes` is already stored by the registry;
        # this is the operator-facing half (one line per crossing report).
        logger.warning(
            "sandbox %s on node %s is over its workspace budget "
            "(%d MiB used of %d MiB): writes are blocked until it is back inside",
            record.sandbox_id,
            node_id,
            (record.workspace_disk_used_bytes or 0) // (1024 * 1024),
            record.disk_size_mb,
        )



@router.get("/internal/nodes/{node_id}/sandboxes")
async def node_sandboxes(node_id: str, request: Request) -> dict[str, Any]:
    """Control-plane view of one node's sandbox records (E6.1 recovery).

    The worker uses this as the authoritative list when reconciling its
    local runtime after a partition: any local runtime not in this list is
    an orphan and is torn down locally. The returned ``sandboxIDs`` are the
    reconcile snapshot — the worker must echo them back in
    ``POST .../reconcile``'s ``snapshotIDs`` so the control plane only ever
    removes records that existed when the snapshot was taken (a record
    created after the snapshot is a concurrent create and must survive).
    """
    _require_node_identity(request, node_id)
    records = request.app.state.registry.list_by_node(node_id)
    return {"nodeID": node_id, "sandboxIDs": [r.sandbox_id for r in records]}


@router.post("/internal/nodes/{node_id}/reconcile")
async def node_reconcile(node_id: str, request: Request) -> dict[str, Any]:
    """Reconcile control-plane records against the worker's local runtime.

    Body: ``{"sandboxIDs": [...], "snapshotIDs": [...]}`` — ``sandboxIDs``
    are the sandboxes this worker currently runs; ``snapshotIDs`` are the
    records the worker saw in ``GET .../sandboxes`` before computing its
    diff. Records the worker still has are un-orphaned (recovery); records
    it no longer has are removed only when they were part of the snapshot —
    records created after the snapshot (concurrent creates) are kept. The
    result mirrors :meth:`SandboxRegistry.recover_node`.
    """
    _require_node_identity(request, node_id)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("sandboxIDs"), list)
        or not isinstance(body.get("snapshotIDs"), list)
    ):
        raise OfficialError(
            400, "Body must be {\"sandboxIDs\": [...], \"snapshotIDs\": [...]}"
        )
    sandbox_ids = [s for s in body["sandboxIDs"] if isinstance(s, str)]
    snapshot_ids = [s for s in body["snapshotIDs"] if isinstance(s, str)]
    if any(not validate_sandbox_id(s) for s in sandbox_ids) or any(
        not validate_sandbox_id(s) for s in snapshot_ids
    ):
        raise OfficialError(400, "sandboxIDs/snapshotIDs must be valid sandbox ids")
    return request.app.state.registry.recover_node(
        node_id,
        set(sandbox_ids),
        set(snapshot_ids),
        timeout=request.app.state.settings.default_timeout,
    )


@router.post("/internal/nodes/{node_id}/slot-identity")
async def node_slot_identity(node_id: str, request: Request) -> dict[str, Any]:
    """Forward a slot child's reported pid to this node's agent (C3 Task 3).

    The worker forks the slot's child and reports ``{sandbox_id, pid}`` -- the
    pid it knows (its own pid namespace) and **no uid** (ruling D9.1). This
    handler is the middle of C3's only two channels: it validates the caller
    with the same identity layer every node-scoped handler uses (① credential →
    node, ② claim == credential, ③ the object is the control plane's own record
    and belongs to that node, plus the source-IP second factor), then instructs
    the node's agent with the uid **from its own records** and the worker's
    identity it stored at registration (ruling D9.3).

    Every hop is named and fail-closed: an unknown sandbox is a 404, another
    node's sandbox a 403, a sandbox with no allocated uid or a node with no
    reported identity a 503, a stuck agent a 504 and an unreachable one a 502.
    None of them is allowed to look like "the sandbox create hangs".
    """
    node_id, _endpoint = _require_node_identity(request, node_id)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    if "uid" in body:
        # Silently ignoring it would leave the next reader believing the value
        # was considered; the shape itself is what the worker sent that is wrong
        # (hard rule 1/3: the identity is the control plane's to name).
        raise OfficialError(
            400,
            "a slot-identity report carries {sandbox_id, pid} and no uid: the "
            "identity comes from the control plane's records",
        )
    sandbox_id = body.get("sandbox_id")
    pid = body.get("pid")
    if not isinstance(sandbox_id, str) or not validate_sandbox_id(sandbox_id):
        raise OfficialError(400, "sandbox_id must be a valid sandbox id")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise OfficialError(400, "pid must be a positive integer")
    try:
        record = request.app.state.registry.get(sandbox_id)
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found") from None
    owner = record.node_id or "local"
    if owner != node_id:
        # Step ③: the object is the control plane's own record and it says this
        # sandbox lives elsewhere. A worker may not act outside its node.
        raise OfficialError(
            403, f"Sandbox {sandbox_id} belongs to node {owner}, not {node_id}"
        )
    if record.host_uid is None:
        raise OfficialError(
            503,
            f"sandbox {sandbox_id} has no allocated host uid: refusing to "
            "instruct the agent",
        )
    node = request.app.state.nodes.get(node_id)
    worker_pid_namespace = getattr(node, "pid_namespace", None)
    if not worker_pid_namespace:
        raise OfficialError(
            503,
            f"node {node_id} has reported no pid namespace identity: refusing "
            "to instruct the agent without it",
        )
    client = getattr(request.app.state, "c3_agent_client", None)
    if client is None:
        raise OfficialError(
            503,
            "this control plane has no C3 agent client configured: refusing to "
            "report a slot identity",
        )
    try:
        answer = await client.grant_slot(
            node_id=node_id,
            sandbox_id=sandbox_id,
            container_pid=pid,
            uid=int(record.host_uid),
            worker_pid_namespace=worker_pid_namespace,
        )
    except AgentClientError as exc:
        raise OfficialError(exc.status_code, str(exc)) from exc
    return {
        "nodeID": node_id,
        "sandboxID": sandbox_id,
        "uid": int(record.host_uid),
        "pid": pid,
        "agent": answer,
    }


def _owned_sandbox(request: Request, node_id: str, sandbox_id: Any):
    """The control plane's record for ``sandbox_id``, or a named refusal (③).

    The object check shared by the node-scoped file surfaces: the record must
    exist and must be scheduled onto the node the caller proved itself to be.
    Kept in one place so the two surfaces cannot drift on *which* records a
    node may act on.
    """
    if not isinstance(sandbox_id, str) or not validate_sandbox_id(sandbox_id):
        raise OfficialError(400, "sandbox_id must be a valid sandbox id")
    try:
        record = request.app.state.registry.get(sandbox_id)
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found") from None
    owner = record.node_id or "local"
    if owner != node_id:
        raise OfficialError(
            403, f"Sandbox {sandbox_id} belongs to node {owner}, not {node_id}"
        )
    return record


@router.post("/internal/nodes/{node_id}/file-op")
async def node_file_op(node_id: str, request: Request) -> dict[str, Any]:
    """Run one file operation for a worker, through that node's agent.

    This is the second half of C3's face B and the reason the worker no longer
    needs a privileged binary: the worker reports ``{sandbox_id, op}`` --
    never a path and never a uid (hard rules 1/3, §14.4) -- and the control
    plane, which holds the records, derives the target and instructs the
    agent, which executes it (C3 §11.2: the agent *is* the executor; nothing
    here hands an identity to a worker helper).

    Every layer is named and fail-closed: the identity layer (①② + source IP)
    from every node-scoped handler, the object check (③), the derivation's own
    root check, and the agent hop's typed errors (504 stuck, 502 refused or
    unreachable, 503 no client or no identity).
    """
    node_id, _endpoint = _require_node_identity(request, node_id)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    try:
        spec = file_ops.spec_for(body.get("op"))
        file_ops.validate_params(spec, body)
    except file_ops.FileOpRefusal as exc:
        raise OfficialError(exc.status_code, str(exc)) from exc
    record = _owned_sandbox(request, node_id, body.get("sandbox_id"))
    sandbox_id = record.sandbox_id
    node = request.app.state.nodes.get(node_id)
    if node is None:
        raise OfficialError(404, f"Node {node_id} not found")
    state = request.app.state

    def _derive():
        paths = file_ops.control_paths(state, state.settings)
        return file_ops.derive(
            spec,
            body,
            paths=paths,
            host_uid=record.host_uid,
            node_id=node_id,
            worker_gid=getattr(node, "worker_gid", None),
        )

    try:
        # Off the event loop (fourth review, minor): ``control_paths`` calls
        # ``volumes.list()`` -- a directory glob plus one store read per
        # candidate -- and ``derive`` resolves paths; both are blocking calls in
        # an async handler, once per file operation. The work is small but it is
        # I/O against the shared store, so it does not belong on the loop.
        instruction = await asyncio.to_thread(_derive)
    except file_ops.FileOpRefusal as exc:
        raise OfficialError(exc.status_code, str(exc)) from exc
    # Every file op carries the worker's identity to the agent -- the
    # ``--worker`` form *is* that identity, and the group a tree is handed to is
    # the worker's own gid -- so a node that has not reported one cannot be
    # instructed for **any** op, not only the chown ones the derivation happens
    # to check. Guarding here (and not just on the chown arm) is what keeps a
    # rollout window -- an older worker that reports no identity -- a named 503
    # instead of an unhandled ``TypeError`` behind a bare 500.
    worker_uid = getattr(node, "worker_uid", None)
    worker_gid = getattr(node, "worker_gid", None)
    if worker_uid is None or worker_gid is None:
        raise OfficialError(
            503,
            f"node {node_id} has reported no worker identity (workerUID/"
            "workerGID): refusing to instruct the agent",
        )
    client = getattr(state, "c3_agent_client", None)
    if client is None:
        raise OfficialError(
            503,
            "this control plane has no C3 agent client configured: refusing "
            f"to run {spec.op}",
        )
    common = {
        "node_id": node_id,
        "sandbox_id": sandbox_id,
        "path": instruction.path,
        "worker_uid": int(worker_uid),
        "worker_gid": int(worker_gid),
    }
    # D21 option 2: a shape whose value is a claim the agent confirms carries
    # the anchor the agent confirms it *against*. A shape that verified the
    # value itself (k8s) sends nothing extra, exactly as before.
    anchor = _worker_identity_anchor(request, node, node_id)
    if anchor is not None:
        common["worker_container_id"] = anchor
    try:
        if spec.verb == "chown":
            answer = await client.chown(
                **common,
                uid=instruction.uid,
                gid=instruction.gid,
                recursive=instruction.recursive,
                worker_owned=instruction.worker_owned,
            )
        elif spec.verb == "rm":
            answer = await client.rm(**common)
        else:
            answer = await client.walk(**common)
    except AgentClientError as exc:
        raise OfficialError(exc.status_code, str(exc)) from exc
    response = {
        "nodeID": node_id,
        "sandboxID": sandbox_id,
        "op": spec.op,
        "verb": spec.verb,
        "path": instruction.path,
        "agent": answer,
    }
    if spec.verb == "walk":
        stdout = answer.get("stdout") if isinstance(answer, dict) else None
        response["stdout"] = stdout if isinstance(stdout, str) else None
    return response


@router.post("/internal/nodes/{node_id}/agent/inventory")
async def agent_inventory(node_id: str, request: Request) -> dict[str, Any]:
    """An agent reports the sandbox trees it can see; the control plane decides.

    C3 Task 6's self-heal shape (option (e)): **the agent is the eyes and the
    control plane is the brain**. The body is ``{"sandboxes": [id, ...]}`` and
    nothing else -- no path, no uid, no verb, no "please delete" (§14.4, hard
    rules 1/3): the ids are what the agent observed on its own mount, and the
    control plane is the only party that may derive a target from them
    (``control_plane/self_heal.py``).

    Why the direction is the agent's, not the worker's: the sweep must survive
    a worker that crashed and never restarted, so nothing here reads a worker
    pod or a worker record. ``node_id`` is the **agent's** identity -- the host
    it runs on (D12) -- checked against the credential and the source IP by
    :func:`_require_agent_identity`.

    Every outcome is named: a deferral is a normal answer (200) whose
    ``deferred`` field carries the reason (the agent logs it and backs off), a
    refusal is 400/401/403/503, and a removal that the agent refused is
    reported in ``failed`` -- never as a removal that happened.
    """
    # The identity step is a k8s pod listing (or a ``getaddrinfo`` in the
    # compose lane) plus the source-IP compare: blocking I/O, so it runs off the
    # event loop, the same discipline the path derivation below follows (a stall
    # here stalls every sandbox API on this replica -- I-2 / the fourth review's
    # ``/metrics`` lesson). The headers and app state it reads are immutable
    # context, so a thread is safe.
    target = await asyncio.to_thread(_require_agent_identity, request, node_id)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "an agent inventory report must be a JSON object")
    extra = set(body) - {"sandboxes"}
    if extra:
        # Refused by name: an ignored ``path``/``uid`` would leave its author
        # believing it was considered (the same rule the file-op surface uses).
        raise OfficialError(
            400,
            "an inventory report carries only its sandbox ids: refusing "
            + ", ".join(sorted(extra)),
        )
    sandboxes = body.get("sandboxes")
    if not isinstance(sandboxes, list):
        raise OfficialError(400, "sandboxes must be a list of sandbox ids")
    for sandbox_id in sandboxes:
        if (
            not isinstance(sandbox_id, str)
            or not validate_sandbox_id(sandbox_id)
            or is_reserved_platform_namespace(sandbox_id)
        ):
            raise OfficialError(
                400,
                "sandboxes must be a list of sandbox ids: "
                f"{sandbox_id!r} is not one",
            )
    outcome = await self_heal.run_sweep(
        request.app.state,
        client=request.app.state.c3_agent_client,
        node_id=node_id,
        target=target,
        reported=sorted(set(sandboxes)),
    )
    return outcome.as_response()


@router.get("/internal/fleet/sandboxes")
async def fleet_sandboxes(request: Request) -> dict[str, Any]:
    """Every sandbox id the control plane records, attributed to its node (D6/D7).

    Fleet scope, not node scope. A worker's ownership sweep asks "does anyone
    *anywhere* own this tree/image?", which is not a statement about the node it
    speaks for -- so this endpoint is in the named fleet-scope set and
    authenticates with the shared key like the other ops surfaces.

    Why it exists: enumerating the fleet through the *per-node* endpoints would
    make a registered-but-unresolvable node (worker permanently gone;
    ``reap_unhealthy`` keeps its row until its sandboxes' TTL) refuse that
    worker's whole round, which is exactly when orphan reclamation must work.
    The per-node endpoints stay identity-guarded; this one answers the fleet
    question directly.

    **Attribution is part of the shape** (D7): ``{"sandboxes": {node_id: [id,
    ...]}}``. A fleet-scope caller (the out-of-cluster acceptance script is the
    reason) can then ask "which ids does node X own?" without *impersonating*
    X, which is what the node-scoped endpoint requires and what an operator
    outside the cluster cannot do. Records with no node (the in-process
    ``local`` worker) are attributed to ``"local"`` -- the id the control plane
    uses for that node everywhere else -- so the view is complete: a caller can
    always account for *every* record.
    """
    _require_fleet_key(request)
    by_node: dict[str, list[str]] = {}
    for record in request.app.state.registry.list():
        by_node.setdefault(record.node_id or "local", []).append(record.sandbox_id)
    # Sorted per node: the view is read by operators and by a diff-friendly
    # acceptance script, and the registry's own order carries no meaning.
    return {"sandboxes": {node: sorted(ids) for node, ids in by_node.items()}}


@router.get("/internal/routes/{sandbox_id}")
async def get_route(sandbox_id: str, request: Request) -> dict[str, Any]:
    _require_fleet_key(request)
    registry = request.app.state.registry
    try:
        record = registry.get(sandbox_id)
    except Exception:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        raise OfficialError(404, f"Node {record.node_id} not found")
    if node.status != "healthy":
        raise OfficialError(502, f"Node {record.node_id} unavailable")
    return {"nodeID": node.node_id, "address": node.address}


@router.get("/internal/nodes")
async def list_nodes_internal(request: Request) -> list[dict[str, Any]]:
    _require_fleet_key(request)
    return [n.to_dict() for n in request.app.state.nodes.list()]


@router.post("/internal/nodes/{node_id}/drain")
async def drain_node(node_id: str, request: Request) -> dict[str, Any]:
    _require_fleet_key(request)
    result = fleet_view.drain_node(request.app.state, node_id)
    if result is None:
        raise OfficialError(404, f"Node {node_id} not found")
    return result


@router.post("/internal/nodes/{node_id}/undrain")
async def undrain_node(node_id: str, request: Request) -> Response:
    _require_fleet_key(request)
    record = request.app.state.nodes.set_draining(node_id, False)
    if record is None:
        raise OfficialError(404, f"Node {node_id} not found")
    return Response(status_code=204)


@router.get("/internal/fleet/metrics")
async def fleet_metrics(request: Request) -> dict[str, Any]:
    """Aggregate fleet state for the autoscaler.

    Returns per-node utilization/active sandboxes, fleet aggregates, the
    remaining standard-sandbox capacity, and the recent 503 error count.

    The body lives in :func:`control_plane.fleet_view.fleet_metrics_payload`,
    because the autoscaler hosts its loop in this same app now and reads the
    very same function -- one computation, an HTTP reader and an in-process
    one, instead of two endpoints that have to agree.
    """
    _require_fleet_key(request)
    return fleet_view.fleet_metrics_payload(request.app.state)


_EMPTY_TENANT_USAGE = {
    "sandboxes": 0,
    "memoryMB": 0,
    "cpuPercent": 0,
    "diskMB": 0,
    "processes": 0,
}


@router.get("/internal/tenants")
async def internal_tenants(request: Request) -> dict[str, Any]:
    """Per-tenant usage and configured limits (ops reconciliation, E3.1).

    Internal API: authenticated with X-Internal-Key, never tenant-scoped.
    ``unowned`` usage (tenant_id None) is included when present so operators
    can detect resources that still need the migration script.
    """
    _require_fleet_key(request)
    settings = request.app.state.settings
    usage = request.app.state.registry.tenant_usage()
    tenant_ids = (
        set(settings.tenant_map) | set(settings.tenant_limits) | set(usage)
    )
    tenant_ids.discard(None)
    tenants = [
        {
            "tenantID": tenant_id,
            "used": usage.get(tenant_id, dict(_EMPTY_TENANT_USAGE)),
            "limits": settings.tenant_limits.get(tenant_id, {}),
        }
        for tenant_id in sorted(tenant_ids)
    ]
    body: dict[str, Any] = {
        "tenants": tenants,
        "compatibleMode": not settings.tenants_enabled,
    }
    if usage.get(None):
        body["unowned"] = usage[None]
    return body
