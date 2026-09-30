"""Kubernetes workload backend using the k8s REST API directly (no extra
dependency). Scales the worker Deployment *or* StatefulSet.

Both kinds are supported because the shape decides whether a worker's identity
survives a restart: the baseline runs a StatefulSet so the node ids are stable
(``e2b-worker-0``/``-1``, matching the compose stack's ``E2B_NODE_ID``), while a
cluster that only has the Deployment form (pre-N20 manifests) must stay
scalable. ``has_node``/``remove_node`` are kind-agnostic either way: they work on
pods, and the node id *is* the pod name.

Where the two kinds differ is *who* dies on scale-down, and that difference is
enforced, not assumed (open-issues N51):

* :meth:`remove_node` raises the drained pod's ``pod-deletion-cost`` and scales
  the workload down by one. The **ReplicaSet** controller honours that
  annotation, so a Deployment retires the pod the loop chose.
* A **StatefulSet** does not honour it -- ``spec.replicas - 1`` deletes the
  highest ordinal, full stop -- so on the baseline kind the annotation is inert
  and the *top* pod is the one that goes.

:meth:`retire_victim` is the seam that keeps the two honest: the loop asks which
candidate a scale-down would actually take, and shrinks only when the answer is
one of the nodes it may drain. Without that question the ordinary scale-down
looks fine (the idle node after a scale-up *is* the newest one) right up to the
case where a *busy* top ordinal holds live sandboxes and an older node is the
idle candidate -- then scaling down would delete the busy one.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DELETION_COST = "controller.kubernetes.io/pod-deletion-cost"

#: ``kind`` -> the REST path segment for it. Only these two are meaningful: the
#: autoscaler scales worker replicas, not arbitrary workloads.
_RESOURCE_FOR_KIND = {
    "deployment": "deployments",
    "statefulset": "statefulsets",
}


def _service_account_token() -> str | None:
    path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return os.getenv("KUBERNETES_TOKEN")


class KubernetesBackend:
    def __init__(
        self,
        *,
        namespace: str,
        deployment: str,
        kind: str = "deployment",
        client: httpx.Client | None = None,
        in_cluster: bool = True,
    ) -> None:
        self._namespace = namespace
        self._deployment = deployment
        self._kind = kind.strip().lower()
        if self._kind not in _RESOURCE_FOR_KIND:
            raise ValueError(
                f"unsupported workload kind {kind!r}: expected one of "
                f"{sorted(_RESOURCE_FOR_KIND)}"
            )
        self._resource = _RESOURCE_FOR_KIND[self._kind]
        if client is not None:
            self._client = client
            return
        host = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
        token = _service_account_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        verify = str(ca_path) if ca_path.is_file() else True
        self._client = httpx.Client(
            base_url=f"https://{host}:{port}",
            headers=headers,
            timeout=15.0,
            verify=verify,
        )

    def _deploy_url(self) -> str:
        return (
            f"/apis/apps/v1/namespaces/{self._namespace}"
            f"/{self._resource}/{self._deployment}"
        )

    def current(self) -> int:
        resp = self._client.get(self._deploy_url())
        resp.raise_for_status()
        return int(resp.json().get("spec", {}).get("replicas", 0))

    def has_node(self, node_id: str) -> bool:
        resp = self._client.get(
            f"/api/v1/namespaces/{self._namespace}/pods/{node_id}"
        )
        return resp.status_code == 200

    def retire_victim(self, candidates: list[str]) -> str | None:
        """Which candidate a scale-down would really take (N51).

        A **Deployment**'s pods are owned by a ReplicaSet, whose controller
        reads the ``pod-deletion-cost`` :meth:`remove_node` sets -- so the loop's
        own first choice is the pod that goes, and no API call is needed to say
        so.

        A **StatefulSet** shrinks from the top: ``spec.replicas - 1`` deletes
        the highest ordinal, full stop. So the only node this backend may
        retire is the top one, and only when the loop's candidate set contains
        it -- otherwise the scale-down would kill a pod the loop never chose,
        which on this fleet can be a worker holding live sandboxes. ``None``
        then means "wait for the fleet to become shrinkable"; the loop retries
        next interval.

        The pod list is read from the API rather than derived from the naming
        rule, because what matters is which pods *exist*: a crashed ordinal
        that the controller has not recreated yet is not a pod that can be
        deleted, and the node registry's rows outlive pods (they age out on
        heartbeats).
        """
        if not candidates:
            return None
        if self._kind != "statefulset":
            return candidates[0]
        ordinals = self._statefulset_ordinals()
        if not ordinals:
            return None
        top = max(ordinals, key=lambda name: ordinals[name])
        return top if top in candidates else None

    def _statefulset_ordinals(self) -> dict[str, int]:
        """This StatefulSet's live pods, by name -> ordinal.

        Scoped by ``ownerReferences`` rather than by a label, so nothing here
        depends on a naming convention the manifest does not actually enforce.
        A pod whose name does not end in an ordinal is skipped (it cannot be
        what ``spec.replicas - 1`` deletes).
        """
        resp = self._client.get(f"/api/v1/namespaces/{self._namespace}/pods")
        resp.raise_for_status()
        prefix = f"{self._deployment}-"
        found: dict[str, int] = {}
        for pod in resp.json().get("items", []):
            metadata = pod.get("metadata") or {}
            owners = metadata.get("ownerReferences") or []
            if not any(
                owner.get("kind") == "StatefulSet"
                and owner.get("name") == self._deployment
                for owner in owners
            ):
                continue
            name = metadata.get("name") or ""
            tail = name[len(prefix) :] if name.startswith(prefix) else ""
            if tail.isdigit():
                found[name] = int(tail)
        return found

    def scale_to(self, replicas: int) -> None:
        """Set the workload's replica count through its scale subresource.

        A **merge patch**, not a replace: the replace form of `.../scale`
        requires the body to carry the object's own ``metadata.name``, and this
        call has nothing to put there -- measured on k0s 2026-09-30, the PUT
        answered ``400 BadRequest`` / *"the name of the object (e2b-worker based
        on URL) was undeterminable: name must be provided"*, so the fleet never
        grew while every tick logged `autoscaler tick failed`. The patch form
        takes the name from the URL and needs no ``resourceVersion`` either
        (no lost-update retry loop: replicas is an absolute count, and the loop
        re-reads it with :meth:`current` on the next tick).
        """
        resp = self._client.patch(
            f"{self._deploy_url()}/scale",
            json={"spec": {"replicas": int(replicas)}},
            headers={"Content-Type": "application/merge-patch+json"},
        )
        resp.raise_for_status()
        logger.info(
            "scaled %s %s to %s", self._kind, self._deployment, replicas
        )

    def remove_node(self, node_id: str) -> None:
        """Retire one specific pod: prefer deleting it on scale-down."""
        pod_url = (
            f"/api/v1/namespaces/{self._namespace}/pods/{node_id}"
        )
        patch = {"metadata": {"annotations": {_DELETION_COST: "1000"}}}
        resp = self._client.patch(
            pod_url,
            json=patch,
            headers={"Content-Type": "application/strategic-merge-patch+json"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"failed to raise deletion cost for pod {node_id}: {resp.status_code}"
            )
        self.scale_to(self.current() - 1)
