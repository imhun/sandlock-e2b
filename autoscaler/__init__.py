"""Autoscaler for the Sandlock E2B worker pool.

Polls the control plane's fleet metrics, computes a desired replica count
from capacity utilization / 503 failures, and reconciles either a local
Docker container pool or a Kubernetes Deployment. Scale-down only drains and
removes idle nodes while fleet utilization is below the scale-down guard.
"""

from autoscaler.config import Settings
from autoscaler.loop import AutoscalerLoop

__all__ = ["AutoscalerLoop", "Settings"]
