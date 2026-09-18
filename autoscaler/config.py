"""Autoscaler settings from environment variables (E2B_AS_*)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    control_plane_url: str = field(
        default_factory=lambda: os.getenv("E2B_AS_CONTROL_PLANE_URL", "http://127.0.0.1:3000")
    )
    internal_api_key: str = field(
        default_factory=lambda: os.getenv("E2B_AS_INTERNAL_API_KEY", "internal-key")
    )
    poll_s: int = field(default_factory=lambda: _env_int("E2B_AS_POLL_S", 5))
    min_replicas: int = field(default_factory=lambda: _env_int("E2B_AS_MIN_REPLICAS", 1))
    max_replicas: int = field(default_factory=lambda: _env_int("E2B_AS_MAX_REPLICAS", 16))
    util_threshold: float = field(
        default_factory=lambda: _env_float("E2B_AS_UTIL_THRESHOLD", 0.70)
    )
    scale_up_cooldown_s: int = field(
        default_factory=lambda: _env_int("E2B_AS_SCALE_UP_COOLDOWN_S", 60)
    )
    scale_down_cooldown_s: int = field(
        default_factory=lambda: _env_int("E2B_AS_SCALE_DOWN_COOLDOWN_S", 600)
    )
    scale_down_util: float = field(
        default_factory=lambda: _env_float("E2B_AS_SCALE_DOWN_UTIL", 0.40)
    )
    node_scale_down_util: float = field(
        default_factory=lambda: _env_float("E2B_AS_NODE_SCALE_DOWN_UTIL", 0.0)
    )
    warmup_buffer: int = field(
        default_factory=lambda: _env_int("E2B_AS_WARMUP_BUFFER", 1)
    )
    backend: str = field(
        default_factory=lambda: os.getenv("E2B_AS_BACKEND", "local").lower()
    )
    # Local (Docker pool) backend.
    docker_image: str = field(
        default_factory=lambda: os.getenv(
            "E2B_AS_DOCKER_IMAGE",
            "registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:0.1.0",
        )
    )
    docker_network: str = field(
        default_factory=lambda: os.getenv("E2B_AS_DOCKER_NETWORK", "sandlock_default")
    )
    workspace_volume: str = field(
        default_factory=lambda: os.getenv("E2B_AS_WORKSPACE_VOLUME", "sandbox-shared")
    )
    worker_env: dict[str, str] = field(
        default_factory=lambda: _env_json("E2B_AS_WORKER_ENV", {})
    )
    # Kubernetes backend.
    k8s_namespace: str = field(
        default_factory=lambda: os.getenv("E2B_AS_K8S_NAMESPACE", "default")
    )
    k8s_deployment: str = field(
        default_factory=lambda: os.getenv("E2B_AS_K8S_DEPLOYMENT", "e2b-worker")
    )
    #: ``deployment`` or ``statefulset``. The baseline runs the worker as a
    #: StatefulSet so its node ids are stable across restarts (N20); the name of
    #: the object still comes from ``E2B_AS_K8S_DEPLOYMENT``.
    k8s_kind: str = field(
        default_factory=lambda: os.getenv("E2B_AS_K8S_KIND", "deployment")
    )


def _env_json(name: str, default: dict) -> dict:
    raw = os.getenv(name)
    if not raw:
        return dict(default)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return dict(default)
    return value if isinstance(value, dict) else dict(default)
