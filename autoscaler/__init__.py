"""The worker fleet's autoscaler: decisions, and the marks they are made on.

Reads the fleet, computes a desired replica count from capacity utilization /
503 failures, and reconciles the cluster's worker workload. Scale-down only
drains and removes idle nodes while fleet utilization is below the scale-down
guard.

There is no entry point here any more. Since 2026-09-30 the loop runs inside
the control plane -- ``control_plane.autoscaler_service`` builds it, the app's
lifespan runs it -- and the only backend is Kubernetes: the Docker pool and the
standalone process went with the local compose autoscaler (they were the last
thing in the repo that wanted a Docker socket at runtime). What is left is a
library whose seams are ``control``, ``backend`` and ``state``, each of which
the control plane provides.
"""

from autoscaler.loop import AutoscalerLoop

__all__ = ["AutoscalerLoop"]
