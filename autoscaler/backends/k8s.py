"""Kubernetes Deployment backend using the k8s REST API directly (no extra
dependency). Scales the worker Deployment and retires the drained pod by
raising its pod-deletion-cost before scaling down, so the controller picks
the safe pod to terminate."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DELETION_COST = "controller.kubernetes.io/pod-deletion-cost"


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
        client: httpx.Client | None = None,
        in_cluster: bool = True,
    ) -> None:
        self._namespace = namespace
        self._deployment = deployment
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
            f"/deployments/{self._deployment}"
        )

    def current(self) -> int:
        resp = self._client.get(self._deploy_url())
        resp.raise_for_status()
        return int(resp.json().get("spec", {}).get("replicas", 0))

    def scale_to(self, replicas: int) -> None:
        resp = self._client.put(
            f"{self._deploy_url()}/scale",
            json={"spec": {"replicas": int(replicas)}},
        )
        resp.raise_for_status()
        logger.info("scaled deployment %s to %s", self._deployment, replicas)

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
