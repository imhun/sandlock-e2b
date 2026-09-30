"""The scale backend: the cluster's worker workload (Deployment or StatefulSet).

The local Docker pool backend was removed with the standalone autoscaler
(2026-09-30): the only thing left to scale is what the control plane can name
by namespace/kind/name, which is also what lets the loop live inside a pod that
holds no Docker socket.
"""

from autoscaler.backends.base import ScaleBackend
from autoscaler.backends.k8s import KubernetesBackend

__all__ = ["KubernetesBackend", "ScaleBackend"]
