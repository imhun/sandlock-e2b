"""Kubernetes workload backend using the k8s REST API directly (no extra
dependency). Scales the worker Deployment *or* StatefulSet and retires the
drained pod by raising its pod-deletion-cost before scaling down, so the
controller picks the safe pod to terminate.

Both kinds are supported because the shape decides whether a worker's identity
survives a restart: the baseline runs a StatefulSet so the node ids are stable
(``e2b-worker-0``/``-1``, matching the compose stack's ``E2B_NODE_ID``), while a
cluster that only has the Deployment form (pre-N20 manifests) must stay
scalable. ``has_node``/``remove_node`` are kind-agnostic either way: they work on
pods, and the node id *is* the pod name.
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

    def scale_to(self, replicas: int) -> None:
        resp = self._client.put(
            f"{self._deploy_url()}/scale",
            json={"spec": {"replicas": int(replicas)}},
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
