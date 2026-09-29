"""Where a worker's own uid/gid may come from -- and what may not be trusted.

C3 Task 4's fourth review (②): face B hands sandbox trees to *the worker's*
identity (``e2b-maint chown --uid X --gid <worker gid>`` and the ``--worker``
form that scopes a slot's credential-bearing ``policy.json``). In C1 that value
came from ``SO_PEERCRED`` and could not be forged; in the first cut of the agent
it came from the **worker's own register/heartbeat body**, which a compromised
worker can write freely -- so it could ask the agent to hand a tree (or the
egress-proxy policy) to *another tenant's* uid. That is exactly the surface C3
§14.3 exists to close.

This module is the fix, in the review's first-preference shape: the control
plane **verifies the claim against a trusted source** and never stores an
unverified value. When no trusted source exists for a shape, the node records
**no** identity, and every operation that needs one fails closed with its own
named refusal -- reading an unverified value as the worker's identity is the one
outcome that is never allowed.

The trusted source for the k8s lane is the worker pod itself: ``node_id`` is the
pod name, the control plane already reads that pod for the node's address
(RBAC: ``get,list pods``), and the pod spec's ``securityContext.runAsUser`` /
``runAsGroup`` (pod level, or the single container's) is set by whoever deploys
the worker -- not by the worker. The shipped manifest must pin them for the
source to answer; until it does (slice B), this lane records no identity and the
file operations fail closed, which is the deliberate posture.

The compose lane has no such object to read: the agent service is defined in a
compose file the control plane never sees. The ruling's second shape (D21
option 2) is that it does not have to: the **agent** reads the worker's own
process identity out of the kernel (face B runs with ``pid: host`` in the
compose stacks), and the control plane's part is to carry the anchor that makes
it a lookup -- the worker's recorded pid namespace -- with every instruction
that acts as the worker. ``KernelWorkerIdentitySource`` is that shape: it keeps
the reported uid/gid as the value the agent confirms, and the agent refuses by
name when the kernel disagrees (``deploy/c3_agent/lookup.py``).

``NoWorkerIdentitySource`` therefore remains for a shape that genuinely cannot
answer -- an embedder, or a deployment whose agent has no ``pid: host`` at all
-- and never as a fallback: a shape either names a source or records nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol

logger = logging.getLogger(__name__)


class WorkerIdentitySource(Protocol):
    """A trusted answer to "which uid/gid does this worker run as?"."""

    #: Whether this *shape* has a trusted source at all. False for a deployment
    #: that can never answer (compose today), True for one whose source exists
    #: even when this particular pod pins nothing -- the two are different
    #: operator problems and are reported differently.
    configured: bool

    #: Whether the answer this shape *stores* is a claim the agent confirms
    #: against the kernel at use time (ruling D21 option 2: the compose lane,
    #: whose face B runs ``pid: host``). True for exactly one shape, and the
    #: file-operation path reads it to decide whether the anchor -- the worker's
    #: pid namespace -- travels with the instruction. A shape that verifies its
    #: own answer (k8s) carries no anchor and behaves as it always did.
    kernel_verified: bool

    def identity_for(self, node_id: str) -> tuple[int, int] | None:
        """``(uid, gid)`` of the worker pod, or ``None`` when unknowable."""
        ...


class NoWorkerIdentitySource:
    """A shape with no trusted source: every answer is "unknown" (fail closed).

    Not "the worker told us nothing, so allow it" -- the caller stores no
    identity and the operations that need one refuse by name.
    """

    configured = False
    kernel_verified = False

    def identity_for(self, node_id: str) -> tuple[int, int] | None:
        return None


class StaticWorkerIdentitySource:
    """A fixed table (tests, embedders, an operator-pinned fleet)."""

    configured = True
    kernel_verified = False

    def __init__(self, identities: Mapping[str, tuple[int, int]]) -> None:
        self._identities = dict(identities)

    def identity_for(self, node_id: str) -> tuple[int, int] | None:
        return self._identities.get(node_id)


class K8sWorkerIdentitySource:
    """The worker pod's own ``securityContext``, read through the API.

    Pod-level first (it overrides the containers in k8s), then the single
    container's. A pod that pins neither answers ``None`` -- and so does any
    read that fails: "cannot determine" must never become "trust the report".
    """

    def __init__(
        self, *, namespace: str, client: Any | None = None
    ) -> None:
        self._namespace = namespace
        self._client = client or self._in_cluster_client()

    configured = True
    kernel_verified = False

    @staticmethod
    def _in_cluster_client():
        from control_plane.node_address import K8sPodAddressResolver

        return K8sPodAddressResolver._in_cluster_client()

    def identity_for(self, node_id: str) -> tuple[int, int] | None:
        from gateway_common.paths import validate_node_id

        if not validate_node_id(node_id):
            return None
        try:
            resp = self._client.get(
                f"/api/v1/namespaces/{self._namespace}/pods/{node_id}"
            )
        except Exception:  # noqa: BLE001 - any transport failure is "unknown"
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return _identity_from_pod(payload)


def _identity_from_pod(pod: dict) -> tuple[int, int] | None:
    """``(uid, gid)`` from a pod spec, or ``None`` when it does not pin them."""
    spec = pod.get("spec")
    if not isinstance(spec, dict):
        return None
    candidates: list[dict] = []
    pod_security = spec.get("securityContext")
    if isinstance(pod_security, dict):
        candidates.append(pod_security)
    containers = spec.get("containers")
    if isinstance(containers, list):
        for container in containers:
            security = (
                container.get("securityContext")
                if isinstance(container, dict)
                else None
            )
            if isinstance(security, dict):
                candidates.append(security)
    # First candidate that names both: the pod-level context wins because it is
    # the one k8s applies on top of the containers.
    for candidate in candidates:
        uid = candidate.get("runAsUser")
        gid = candidate.get("runAsGroup")
        if _is_uid(uid) and _is_gid(gid):
            return int(uid), int(gid)
    return None


def _is_uid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_gid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class KernelWorkerIdentitySource:
    """Compose: the agent reads the worker's identity from the kernel (D21②).

    This shape has no pod spec for the control plane to read, so it cannot
    answer *here* -- but it is not a shape that cannot answer at all. The answer
    is the kernel's, produced by the agent when a file operation is executed:
    the worker's own process identity, read from ``/proc/<pid>/status`` for the
    process(es) in the pid namespace the control plane records for it
    (:meth:`deploy.c3_agent.lookup.ProcLookup.worker_uid_gid`).

    What that means for the stored value, and why it is not the hole D21 option
    1 exists to close: the reported uid/gid is kept as the value the agent will
    **confirm**, never as an answer the control plane acts on. Every instruction
    that acts as the worker carries the anchor (the node's recorded pid
    namespace) beside it, and the agent refuses by name when the kernel does not
    agree -- so a worker that names another tenant's uid gets a refusal, not a
    tree. The claim is carried; the kernel decides.

    ``identity_for`` therefore answers ``None``: this class is the *deferral*,
    and the value it defers to is produced on the other side of the hop.
    """

    configured = True
    kernel_verified = True

    def identity_for(self, node_id: str) -> tuple[int, int] | None:
        return None


def build_worker_identity_source(settings) -> WorkerIdentitySource:
    """The source for this deployment's shape.

    The mode is the same switch the node-address resolver uses
    (``E2B_NODE_ADDRESS_MODE``): a deployment that told the control plane how to
    find its workers has also told it whether a pod spec exists to read.
    """
    mode = (getattr(settings, "node_address_mode", "auto") or "auto").strip().lower()
    if mode == "auto":
        from control_plane.node_address import _service_account_present

        mode = "k8s" if _service_account_present() else "hostname"
    if mode == "k8s":
        return K8sWorkerIdentitySource(
            namespace=getattr(settings, "node_address_namespace", "sandlock")
        )
    if mode == "hostname":
        # The compose shapes: no pod API, but a face B that runs with
        # ``pid: host`` beside the workers, so the kernel can answer where the
        # control plane cannot (D21 option 2). This is the *only* shape that
        # defers, and it is not the same state as "no source at all".
        return KernelWorkerIdentitySource()
    logger.info(
        "C3 file operations: this shape (%s) has no trusted source for the "
        "worker's own uid/gid, so the control plane records none and the "
        "operations that need one refuse by name (never the worker's report)",
        mode,
    )
    return NoWorkerIdentitySource()
