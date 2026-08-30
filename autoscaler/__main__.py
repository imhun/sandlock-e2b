"""Run the autoscaler: ``python -m autoscaler``."""

from __future__ import annotations

import asyncio
import logging

from autoscaler.config import Settings
from autoscaler.control import ControlPlaneClient
from autoscaler.loop import AutoscalerLoop
from autoscaler.policy import PolicyConfig


def _build_backend(settings: Settings):
    if settings.backend == "k8s":
        from autoscaler.backends.k8s import KubernetesBackend

        return KubernetesBackend(
            namespace=settings.k8s_namespace,
            deployment=settings.k8s_deployment,
        )
    from autoscaler.backends.local import DockerPoolBackend

    return DockerPoolBackend(
        image=settings.docker_image,
        network=settings.docker_network,
        workspace_volume=settings.workspace_volume,
        control_plane_url=settings.control_plane_url,
        internal_api_key=settings.internal_api_key,
        base_image=settings.worker_env.get("E2B_BASE_IMAGE"),
        worker_env=settings.worker_env,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    control = ControlPlaneClient(
        base_url=settings.control_plane_url,
        internal_api_key=settings.internal_api_key,
    )
    policy = PolicyConfig(
        min_replicas=settings.min_replicas,
        max_replicas=settings.max_replicas,
        util_threshold=settings.util_threshold,
        scale_up_cooldown_s=settings.scale_up_cooldown_s,
        scale_down_cooldown_s=settings.scale_down_cooldown_s,
        scale_down_util=settings.scale_down_util,
        node_scale_down_util=settings.node_scale_down_util,
        warmup_buffer=settings.warmup_buffer,
    )
    loop = AutoscalerLoop(
        control=control,
        backend=_build_backend(settings),
        policy=policy,
        poll_s=settings.poll_s,
    )
    asyncio.run(loop.run_forever())


if __name__ == "__main__":
    main()
