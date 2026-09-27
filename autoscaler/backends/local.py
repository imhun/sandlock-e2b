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
            # Fleet shape (deploy/stack/docker-compose.prod.yml:209-210): each
            # sandbox gets its own loopback-only netns and egress is mediated by
            # the supervisor's connect fd injection. BOTH are required --
            # `create_app` refuses the unpaired shape by name
            # (envd_service/config.py:471-479), which crash-loops a worker
            # rather than silently cutting every sandbox's network.
            #
            # Declared, and gated: these two are read by the *sandlock* executor
            # only, so what turns them on is the pool's own `E2B_EXECUTOR` --
            # `auto` in the compose default since N38 (2026-09-26). Setting it
            # back to `local` runs the worker's shared netns with the pair
            # inert (and then a wildcard `allowOut` needs the low-port window
            # that this backend no longer declares).
            #
            # Kept in the dictionary (the one place the shape is declared, next
            # to the one seam an operator can override) rather than in `cmd`.
            "E2B_ENABLE_NET_ISOLATION": "true",
            "E2B_FD_INJECT_CONNECT": "true",
            # The two other keys the pool was missing next to the pair (N38
            # connected items 1 and 3), values verbatim from the fleet
            # (`deploy/stack/docker-compose.prod.yml` and
            # `deploy/k8s/worker.yaml`): without `E2B_ENABLE_NETWORK` the worker
            # builds an empty network policy (every connect fails, loopback
            # included), and without `E2B_ROUTE_B_TMP_ROOT` a current worker
            # exits 1 at `configure_priv_helpers` before it ever listens (N39).
            # Here for the same reason as the pair: `DockerPoolBackend()` is
            # built by hand too, so it has to be self-sufficient -- with
            # `E2B_AS_WORKER_ENV` still the override (the seam below, last
            # writer wins).
            "E2B_ENABLE_NETWORK": "true",
            "E2B_ROUTE_B_TMP_ROOT": "/var/lib/e2b-sandboxes/.route-b",
            # N45: without this a pooled sandbox shares the worker's pid
            # namespace and `kill(pid, 0)` answers EPERM for the worker's live
            # PID 1 (an existence oracle). Independent of the netns pair above:
            # the fork creates the user namespace for it itself, so there is no
            # pairing guard and no offline failure mode. The pool's compose
            # JSON declares the same value.
            "E2B_PID_NS": "true",
            # The shared image cache and its bound (Z-F7 C1). The fleet and the
            # pool's own compose JSON both name these; the dictionary did not,
            # and then the worker resolved `E2B_IMAGE_CACHE_DIR` to its
            # container-relative default (`tmp/sandboxes/_images`, i.e. off the
            # workspace volume) with eviction disabled (`E2B_IMAGE_CACHE_MAX_BYTES`
            # defaults to 0 = unbounded) -- so a hand-built backend re-extracted
            # the base rootfs into every worker and never bounded that cache.
            # Found by the N45 key-set audit; values are the fleet's.
            "E2B_IMAGE_CACHE_DIR": "/var/lib/e2b-sandboxes/_images",
            "E2B_IMAGE_CACHE_MAX_BYTES": "4294967296",
            "E2B_IMAGE_CACHE_EVICT_MIN_AGE_S": "300",
            "E2B_IMAGE_CACHE_OWNER_UID": "65534",
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
                # (E2B_QUOTA_AGENT_URL); the container-level low-port window is
                # gone too (2026-09-26). The pooled workers now default to the
                # fleet's per-sandbox netns (the pair below, gated by
                # `E2B_EXECUTOR=auto` since N38), where the wildcard-DNS `:53`
                # bind happens inside the sandbox's own netns as root-in-userns
                # -- so no window is needed. An operator who sets
                # `E2B_EXECUTOR=local` gets the shared netns back with the pair
                # inert, and that shape would need the window again.
                "--security-opt",
                "seccomp=unconfined",
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
