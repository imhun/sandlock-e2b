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
compose file the control plane never sees. It therefore uses
``NoWorkerIdentitySource`` -- the same fail-closed posture -- and closing it
(the agent deriving the identity from the kernel, which needs ``pid: host`` on
face B) is recorded as a slice-B item in ``docs/c3-privilege-relocation.md``
§11.2.1.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol

logger = logging.getLogger(__name__)


class WorkerIdentitySource(Protocol):
    """A trusted answer to "which uid/gid does this worker run as?"."""

    def identity_for(self, node_id: str) -> tuple[int, int] | None:
        """``(uid, gid)`` of the worker pod, or ``None`` when unknowable."""
        ...


class NoWorkerIdentitySource:
    """A shape with no trusted source: every answer is "unknown" (fail closed).

    Not "the worker told us nothing, so allow it" -- the caller stores no
    identity and the operations that need one refuse by name.
    """

    def identity_for(self, node_id: str) -> tuple[int, int] | None:
        return None


class StaticWorkerIdentitySource:
    """A fixed table (tests, embedders, an operator-pinned fleet)."""

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
    logger.info(
        "C3 file operations: this shape (%s) has no trusted source for the "
        "worker's own uid/gid, so the control plane records none and the "
        "operations that need one refuse by name (never the worker's report)",
        mode,
    )
    return NoWorkerIdentitySource()
