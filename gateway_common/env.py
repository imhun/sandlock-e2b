"""Environment helpers shared by the control plane and the envd service."""

from __future__ import annotations

import json
import os


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    items: list[str] = []
    for part in value.split(","):
        part = part.strip()
        if part:
            items.append(part)
    return tuple(items)


def env_json_dict(name: str) -> dict[str, str]:
    value = os.getenv(name)
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


def env_json(name: str, default=None):
    """Parse an arbitrary JSON env var; ``default`` when unset or empty."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return json.loads(value)


# Backwards-compatible aliases for the historical private names.
_env_bool = env_bool
_env_int = env_int
_env_float = env_float
_env_list = env_list
_env_json_dict = env_json_dict
_env_json = env_json


def registry_host(image_registry: str | None) -> str | None:
    """Host part of an ``E2B_IMAGE_REGISTRY`` value (``host[:port][/ns]``).

    The registry credentials are configured for this host only, and the same
    value also tells the resolver which host they must never be sent to.
    """
    raw = (image_registry or "").strip()
    if not raw:
        return None
    return raw.split("/")[0].strip().lower()
