"""Control plane configuration from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from gateway_common.env import (
    _env_bool,
    _env_int,
    _env_json_dict,
    _env_list,
)

@dataclass
class Settings:
    """All tunables of the control plane.

    Defaults match spec section 7.2. A total limit of ``0`` disables that
    dimension.
    """

    api_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_API_KEYS", ("local-key",))
    )
    api_key: str | None = field(default_factory=lambda: os.getenv("E2B_API_KEY"))
    control_plane_port: int = field(
        default_factory=lambda: _env_int("E2B_CONTROL_PLANE_PORT", 3000)
    )
    envd_port: int = field(default_factory=lambda: _env_int("E2B_ENVD_PORT", 49983))
    warm_timeout_s: int = field(
        default_factory=lambda: _env_int("E2B_WARM_TIMEOUT_S", 180)
    )
    executor: str = field(
        default_factory=lambda: os.getenv("E2B_EXECUTOR", "auto").lower()
    )
    workspace_base: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_WORKSPACE_BASE", "tmp/sandboxes")
        ).resolve()
    )
    image_cache_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_IMAGE_CACHE_DIR", "tmp/sandboxes/_images")
        ).resolve()
    )
    max_sandboxes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_SANDBOXES", 100)
    )
    default_timeout: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_TIMEOUT", 300)
    )
    max_command_timeout: int = field(
        default_factory=lambda: _env_int("E2B_MAX_COMMAND_TIMEOUT", 3600)
    )
    default_memory_mb: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MEMORY_MB", 512)
    )
    default_cpu_percent: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_CPU_PERCENT", 100)
    )
    default_disk_mb: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_DISK_MB", 1024)
    )
    default_max_processes: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MAX_PROCESSES", 64)
    )
    base_image: str | None = field(default_factory=lambda: os.getenv("E2B_BASE_IMAGE"))
    template_images: dict[str, str] = field(
        default_factory=lambda: _env_json_dict("E2B_TEMPLATE_IMAGES")
    )
    max_total_memory_mb: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_MEMORY_MB", 8192)
    )
    max_total_cpu_percent: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_CPU_PERCENT", 400)
    )
    max_total_disk_mb: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_DISK_MB", 10240)
    )
    max_total_processes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_TOTAL_PROCESSES", 2048)
    )
    enable_network: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETWORK", False)
    )
    log_level: str = field(default_factory=lambda: os.getenv("E2B_LOG_LEVEL", "INFO"))
    internal_api_key: str = field(
        default_factory=lambda: os.getenv("E2B_INTERNAL_API_KEY", "internal-key")
    )
    enable_local_node: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_LOCAL_NODE", True)
    )
    redis_url: str | None = field(default_factory=lambda: os.getenv("E2B_REDIS_URL"))
    gateway_url: str | None = field(default_factory=lambda: os.getenv("E2B_GATEWAY_URL"))
    shared_workspace_root: str | None = field(
        default_factory=lambda: os.getenv("E2B_SHARED_WORKSPACE_ROOT")
    )
    image_registry: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY")
    )
    image_registry_username: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY_USERNAME")
    )
    image_registry_password: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY_PASSWORD")
    )
    shared_volume_root: str | None = field(
        default_factory=lambda: os.getenv("E2B_SHARED_VOLUME_ROOT")
    )

    @property
    def all_api_keys(self) -> tuple[str, ...]:
        keys = list(self.api_keys)
        if self.api_key:
            keys.append(self.api_key)
        return tuple(dict.fromkeys(keys))

    def resolve_template_image(self, template_id: str) -> str | None:
        """Resolve ``templateID`` to a base image, ``None`` for pure Sandlock."""
        if template_id in self.template_images:
            return self.template_images[template_id]
        if template_id == "mcp-gateway":
            return self.base_image
        if template_id == "base":
            return self.base_image
        return None
