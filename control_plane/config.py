"""Control plane configuration from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from gateway_common.env import registry_host
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
        default_factory=lambda: _env_int("E2B_DEFAULT_MEMORY_MB", 1024)
    )
    default_cpu_percent: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_CPU_PERCENT", 100)
    )
    default_disk_mb: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_DISK_MB", 1024)
    )
    default_max_processes: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MAX_PROCESSES", 256)
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
    # E9.1: idle detection (resource-contention.md §3.1). A running sandbox
    # whose last observed activity is older than this becomes an eviction
    # candidate once the fleet is out of capacity. 0 disables idleness.
    sandbox_idle_threshold_s: int = field(
        default_factory=lambda: _env_int("E2B_SANDBOX_IDLE_THRESHOLD_S", 300)
    )
    # E9.1: activity timestamps are high-churn while the idle threshold they
    # feed is minutes wide, so the registry keeps them in memory and writes
    # through to the shared store at most this often. 0 = write every update.
    activity_persist_interval_s: int = field(
        default_factory=lambda: _env_int("E2B_ACTIVITY_PERSIST_INTERVAL_S", 30)
    )
    # E9.3: resource-driven eviction (resource-contention.md §5). Default ON
    # is a user decision (2026-09-01) that overrides the earlier design doc's
    # "default off" draft: when the fleet is full, idle low-priority sandboxes
    # are evicted to make room for a new create.
    eviction_enabled: bool = field(
        default_factory=lambda: _env_bool("E2B_EVICTION_ENABLED", True)
    )
    # E9.3: prefer pausing an idle victim (state preserved, reservation
    # returned) over killing it. The caller still kills a paused victim when
    # pausing alone did not make room.
    eviction_prefer_pause: bool = field(
        default_factory=lambda: _env_bool("E2B_EVICTION_PREFER_PAUSE", False)
    )
    # E9.3: how many idle sandboxes a single create request may evict at most
    # (storm bound; see E2B_EVICTION_MIN_INTERVAL_S for the temporal bound).
    eviction_max_per_create: int = field(
        default_factory=lambda: _env_int("E2B_EVICTION_MAX_PER_CREATE", 3)
    )
    # E9.3: minimum wall-clock gap between eviction rounds, per control-plane
    # process. Replicas do NOT share this throttle (known limitation, see
    # docs/resource-contention.md §8).
    eviction_min_interval_s: int = field(
        default_factory=lambda: _env_int("E2B_EVICTION_MIN_INTERVAL_S", 1)
    )
    # E9.3: how long a kill-eviction notice stays queryable so GET on an
    # evicted sandbox can explain the 404. Redis keys get this as a real TTL;
    # the in-memory fallback expires lazily and is capacity-capped.
    eviction_notice_ttl_s: int = field(
        default_factory=lambda: _env_int("E2B_EVICTION_NOTICE_TTL_S", 3600)
    )
    # E9.3: cross-tenant eviction is OFF by default (security decision): a
    # tenant key may only evict idle sandboxes of its own tenant, otherwise
    # "create a sandbox" would be a weapon to evict other tenants' sandboxes.
    # Admin keys and this switch may cross tenants.
    eviction_cross_tenant: bool = field(
        default_factory=lambda: _env_bool("E2B_EVICTION_CROSS_TENANT", False)
    )
    # E9.4: create queue (resource-contention.md §3.5/§5). When admission
    # still fails after an eviction round, wait up to this long for capacity
    # to be released before answering the original 503. 0 disables queueing
    # (today's direct-503 behavior after eviction).
    create_queue_timeout_s: float = field(
        default_factory=lambda: _env_int("E2B_CREATE_QUEUE_TIMEOUT_S", 30)
    )
    # E9.4: max number of create requests waiting for capacity at once. A
    # full queue answers 429 immediately (no ordering/fairness guarantee;
    # queue state is per replica, see docs/resource-contention.md §8).
    create_queue_max: int = field(
        default_factory=lambda: _env_int("E2B_CREATE_QUEUE_MAX", 100)
    )
    volume_token_ttl_s: int = field(
        default_factory=lambda: _env_int("E2B_VOLUME_TOKEN_TTL_S", 0)
    )
    template_build_concurrency: int = field(
        default_factory=lambda: _env_int("E2B_TEMPLATE_BUILD_CONCURRENCY", 2)
    )
    template_build_rate_limit_per_min: int = field(
        default_factory=lambda: _env_int(
            "E2B_TEMPLATE_BUILD_RATE_LIMIT_PER_MIN", 0
        )
    )
    # E4.2: max bytes accepted by file-write endpoints (volumecontent PUT,
    # template archive upload). 0 disables the limit (repo convention).
    max_file_write_mb: int = field(
        default_factory=lambda: _env_int("E2B_MAX_FILE_WRITE_MB", 512)
    )
    # E5.3: sandbox metadata/envVars size caps (serialized JSON bytes) so
    # sandbox.json cannot be inflated by hostile create bodies. 0 disables
    # the limit (repo convention).
    max_metadata_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_METADATA_BYTES", 64 * 1024)
    )
    max_envvars_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_ENVVARS_BYTES", 64 * 1024)
    )
    # E5.3: cap on JSON request bodies for create/build/fork endpoints
    # (bounded read, 413 beyond the limit).
    max_json_body_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_JSON_BODY_BYTES", 256 * 1024)
    )
    # E5.3: max UTF-8 bytes for user-supplied names (template/snapshot).
    max_name_bytes: int = field(
        default_factory=lambda: _env_int("E2B_MAX_NAME_BYTES", 1024)
    )
    enable_network: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETWORK", False)
    )
    log_level: str = field(default_factory=lambda: os.getenv("E2B_LOG_LEVEL", "INFO"))
    internal_api_key: str = field(
        default_factory=lambda: os.getenv("E2B_INTERNAL_API_KEY", "internal-key")
    )
    # E3.6: rotation window. When E2B_INTERNAL_API_KEYS is set, every listed
    # key authenticates X-Internal-Key (old + new valid during rotation);
    # once the old key is removed from the list it stops working.
    internal_api_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_INTERNAL_API_KEYS", ())
    )
    # E5.4: secret-at-rest encryption. When E2B_SECRET_MASTER_KEY is unset
    # the secret registry degrades to the previous in-memory + plaintext
    # disk behavior with a startup warning and is never persisted to Redis.
    # During rotation keep the previous key in E2B_SECRET_MASTER_KEYS so
    # records encrypted with it still decrypt until every replica has
    # re-encrypted them with the primary key.
    secret_master_key: str | None = field(
        default_factory=lambda: os.getenv("E2B_SECRET_MASTER_KEY") or None
    )
    secret_master_keys: tuple[str, ...] = field(
        default_factory=lambda: _env_list("E2B_SECRET_MASTER_KEYS", ())
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
    # Pull credentials belong to one registry host; public images are pulled
    # anonymously (see ``oci_registry.RegistryClient``). The worker needs the
    # host too, not just the user/password pair.
    image_registry: str | None = field(
        default_factory=lambda: os.getenv("E2B_IMAGE_REGISTRY")
    )

    @property
    def image_registry_host(self) -> str | None:
        """Host the ``image_registry_*`` credentials are meant for."""
        return registry_host(self.image_registry)
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
    def all_internal_api_keys(self) -> tuple[str, ...]:
        """Active X-Internal-Key credentials (list first, single fallback)."""
        keys = list(self.internal_api_keys)
        if self.internal_api_key:
            keys.append(self.internal_api_key)
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
