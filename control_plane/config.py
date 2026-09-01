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
    _env_json,
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
    create_rate_limit_per_min: int = field(
        default_factory=lambda: _env_int("E2B_CREATE_RATE_LIMIT_PER_MIN", 120)
    )
    enable_network: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETWORK", False)
    )
    log_level: str = field(default_factory=lambda: os.getenv("E2B_LOG_LEVEL", "INFO"))
    internal_api_key: str = field(
        default_factory=lambda: os.getenv("E2B_INTERNAL_API_KEY", "internal-key")
    )
    tls_cert_file: str | None = field(
        default_factory=lambda: os.getenv("E2B_TLS_CERT")
    )
    tls_key_file: str | None = field(
        default_factory=lambda: os.getenv("E2B_TLS_KEY")
    )
    enable_local_node: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_LOCAL_NODE", True)
    )
    redis_url: str | None = field(default_factory=lambda: os.getenv("E2B_REDIS_URL"))
    gateway_url: str | None = field(default_factory=lambda: os.getenv("E2B_GATEWAY_URL"))
    buildkit_addr: str = field(
        default_factory=lambda: os.getenv(
            "E2B_BUILDKIT_ADDR", "unix:///run/buildkit/buildkitd.sock"
        )
    )
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
    # Tenant isolation (E3.1). When E2B_TENANTS is unset the control plane
    # runs in single-tenant compatible mode: all keys share every resource
    # and resources are created with tenant_id=None.
    tenant_map: dict[str, list[str]] = field(
        default_factory=lambda: _env_json("E2B_TENANTS", {}) or {}
    )
    admin_api_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_ADMIN_API_KEYS", ())
    )
    tenant_limits: dict[str, dict[str, int]] = field(
        default_factory=lambda: _env_json("E2B_TENANT_LIMITS", {}) or {}
    )
    tenant_rate_limits: dict[str, int] = field(
        default_factory=lambda: _env_json("E2B_TENANT_RATE_LIMITS", {}) or {}
    )

    def __post_init__(self) -> None:
        normalized: dict[str, list[str]] = {}
        for tenant, keys in (self.tenant_map or {}).items():
            if not isinstance(keys, list):
                raise ValueError(
                    f"E2B_TENANTS[{tenant!r}] must be a list of API keys"
                )
            normalized[str(tenant)] = [str(k) for k in keys]
        self.tenant_map = normalized
        self.tenant_limits = {
            str(t): {str(k): int(v) for k, v in limits.items()}
            for t, limits in (self.tenant_limits or {}).items()
        }
        self.tenant_rate_limits = {
            str(t): int(v) for t, v in (self.tenant_rate_limits or {}).items()
        }

    @property
    def all_api_keys(self) -> tuple[str, ...]:
        keys = list(self.api_keys)
        if self.api_key:
            keys.append(self.api_key)
        keys.extend(self.admin_api_keys)
        for tenant_keys in self.tenant_map.values():
            keys.extend(tenant_keys)
        return tuple(dict.fromkeys(keys))

    @property
    def tenants_enabled(self) -> bool:
        """True when tenant isolation is configured (E2B_TENANTS non-empty)."""
        return bool(self.tenant_map)

    def tenant_of_key(self, key: str) -> str | None:
        """Map an API key to its tenant, ``None`` when unassigned."""
        for tenant, keys in self.tenant_map.items():
            if key in keys:
                return tenant
        return None

    @property
    def tls_enabled(self) -> bool:
        """HTTPS is on only when both cert and key are configured."""
        return bool(self.tls_cert_file and self.tls_key_file)

    def resolve_template_image(self, template_id: str) -> str | None:
        """Resolve ``templateID`` to a base image, ``None`` for pure Sandlock."""
        if template_id in self.template_images:
            return self.template_images[template_id]
        if template_id == "mcp-gateway":
            return self.base_image
        if template_id == "base":
            return self.base_image
        return None


def uvicorn_ssl_kwargs(settings: Settings) -> dict[str, str]:
    """uvicorn.run kwargs enabling HTTPS; empty dict keeps plain HTTP.

    Both ``E2B_TLS_CERT`` and ``E2B_TLS_KEY`` must be set together; a
    half-configured pair is a deployment error, not a silent HTTP fallback.
    """
    cert_file = settings.tls_cert_file
    key_file = settings.tls_key_file
    if (cert_file is None) != (key_file is None):
        raise ValueError("E2B_TLS_CERT and E2B_TLS_KEY must be set together")
    if cert_file is None:
        return {}
    return {"ssl_certfile": cert_file, "ssl_keyfile": key_file}
