"""Envd service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from gateway_common.env import _env_bool, _env_int, _env_json_dict

# Default private-egress denylist applied to the implicit full-egress branch
# (no explicit allowOut/denyOut + internet allowed). Covers RFC1918, loopback,
# link-local / cloud metadata, and ULA. Override with E2B_NETWORK_DENY_CIDRS;
# an explicit empty value disables the protection.
DEFAULT_NETWORK_DENY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "fd00::/8",
)


def _network_deny_cidrs() -> tuple[str, ...]:
    value = os.getenv("E2B_NETWORK_DENY_CIDRS")
    if value is None:
        return DEFAULT_NETWORK_DENY_CIDRS
    if value.strip() == "":
        return ()
    return tuple(p.strip() for p in value.split(",") if p.strip())


@dataclass
class Settings:
    envd_port: int = field(default_factory=lambda: _env_int("E2B_ENVD_PORT", 49983))
    workspace_base: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_WORKSPACE_BASE", "tmp/sandboxes")
        ).resolve()
    )
    executor: str = field(
        default_factory=lambda: os.getenv("E2B_EXECUTOR", "auto").lower()
    )
    base_image: str | None = field(default_factory=lambda: os.getenv("E2B_BASE_IMAGE"))
    template_images: dict[str, str] = field(
        default_factory=lambda: _env_json_dict("E2B_TEMPLATE_IMAGES")
    )
    enable_network: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETWORK", False)
    )
    enable_netns: bool = field(
        default_factory=lambda: _env_bool("E2B_ENABLE_NETNS", False)
    )
    network_deny_cidrs: tuple[str, ...] = field(
        default_factory=_network_deny_cidrs
    )
    sandbox_notify_rate_limit: int = field(
        default_factory=lambda: _env_int("E2B_SANDBOX_NOTIFY_RATE_LIMIT", 5000)
    )
    iam_signing_key: str = field(
        default_factory=lambda: os.getenv(
            "E2B_IAM_SIGNING_KEY", "e2b-sandlock-local-iam-key"
        )
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
    quota_via_agent: bool = field(
        default_factory=lambda: _env_bool("E2B_QUOTA_VIA_AGENT", False)
    )
    default_max_processes: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MAX_PROCESSES", 64)
    )
    default_max_open_files: int = field(
        default_factory=lambda: _env_int("E2B_DEFAULT_MAX_OPEN_FILES", 4096)
    )
    # Per-sandbox command serialization (E2.3): max commands that may run
    # concurrently for one sandbox (default 1 = strictly serial); commands
    # beyond the limit queue.
    max_concurrent_commands_per_sandbox: int = field(
        default_factory=lambda: _env_int(
            "E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX", 1
        )
    )
    # Max commands allowed to wait for a slot; beyond this a new command is
    # rejected with 429. None (default) = max_concurrent_commands_per_sandbox.
    max_queued_commands_per_sandbox: int | None = field(
        default_factory=lambda: (
            None
            if os.getenv("E2B_MAX_QUEUED_COMMANDS_PER_SANDBOX") in (None, "")
            else _env_int("E2B_MAX_QUEUED_COMMANDS_PER_SANDBOX", 0)
        )
    )
    log_level: str = field(default_factory=lambda: os.getenv("E2B_LOG_LEVEL", "INFO"))
    internal_api_key: str = field(
        default_factory=lambda: os.getenv("E2B_INTERNAL_API_KEY", "internal-key")
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
    image_cache_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("E2B_IMAGE_CACHE_DIR", "tmp/sandboxes/_images")
        ).resolve()
    )
