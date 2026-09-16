"""Local Docker pool manager: grows workers via ``docker run`` and retires
drained containers with ``docker rm``. The autoscaler runs on the Docker
host and uses the docker CLI (the workers themselves need no socket)."""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

_WORKER_LABEL = "e2b.role=worker"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


class DockerPoolBackend:
    def __init__(
        self,
        *,
        image: str,
        network: str,
        workspace_volume: str,
        control_plane_url: str,
        internal_api_key: str,
        base_image: str | None = None,
        node_memory_mb: str = "2048",
        node_cpu_percent: str = "200",
        node_disk_mb: str = "4096",
        node_processes: str = "256",
        worker_env: dict[str, str] | None = None,
    ) -> None:
        self._image = image
        self._network = network
        self._volume = workspace_volume
        self._control = control_plane_url
        self._key = internal_api_key
        self._base_image = base_image
        self._env = {
            "E2B_NODE_MEMORY_MB": node_memory_mb,
            "E2B_NODE_CPU_PERCENT": node_cpu_percent,
            "E2B_NODE_DISK_MB": node_disk_mb,
            "E2B_NODE_PROCESSES": node_processes,
            **dict(worker_env or {}),
        }

    def current(self) -> int:
        result = _run("ps", "-q", "-f", f"label={_WORKER_LABEL}")
        return len([line for line in result.stdout.splitlines() if line.strip()])

    def has_node(self, node_id: str) -> bool:
        return _run("inspect", node_id).returncode == 0

    def scale_to(self, replicas: int) -> None:
        needed = replicas - self.current()
        if needed <= 0:
            return
        for _ in range(needed):
            name = f"e2b-worker-{int(time.time())}-{_rand_suffix()}"
            cmd = [
                "run",
                "-d",
                "--name",
                name,
                "--label",
                _WORKER_LABEL,
                "--network",
                self._network,
                "-v",
                f"{self._volume}:/var/lib/e2b-sandboxes",
                # A6: no --cap-add SYS_ADMIN. The shared-volume bind was
                # deleted in A4 and quota goes through quota-agent
                # (E2B_QUOTA_AGENT_URL); the low-port window below is a
                # container-spec declaration, not a runtime sysctl write.
                "--security-opt",
                "seccomp=unconfined",
                "--sysctl",
                "net.ipv4.ip_unprivileged_port_start=0",
                # The local backend keeps `seccomp=unconfined` (above): the
                # worker's startup self-check must not refuse that shape.
                "-e",
                "E2B_REQUIRE_SECCOMP_FILTER=0",
                "-e",
                f"E2B_NODE_ID={name}",
                "-e",
                f"E2B_NODE_ADDRESS=http://{name}:49983",
                "-e",
                f"E2B_CONTROL_PLANE_URL={self._control}",
                "-e",
                f"E2B_INTERNAL_API_KEY={self._key}",
                "-e",
                "E2B_WORKSPACE_BASE=/var/lib/e2b-sandboxes",
            ]
            if self._base_image:
                cmd += ["-e", f"E2B_BASE_IMAGE={self._base_image}"]
            for key, value in self._env.items():
                cmd += ["-e", f"{key}={value}"]
            cmd.append(self._image)
            result = _run(*cmd)
            if result.returncode != 0:
                logger.error("docker run %s failed: %s", name, result.stderr.strip())
                raise RuntimeError(
                    f"docker run failed: {result.stderr.strip()[:300]}"
                )
            logger.info("started worker %s", name)

    def remove_node(self, node_id: str) -> None:
        result = _run("rm", "-f", node_id)
        if result.returncode != 0:
            logger.error("docker rm %s failed: %s", node_id, result.stderr.strip())
            raise RuntimeError(
                f"docker rm failed: {result.stderr.strip()[:300]}"
            )
        logger.info("removed worker %s", node_id)


def _rand_suffix() -> str:
    import secrets

    return secrets.token_hex(3)
