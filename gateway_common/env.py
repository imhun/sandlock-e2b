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


# Backwards-compatible aliases for the historical private names.
_env_bool = env_bool
_env_int = env_int
_env_list = env_list
_env_json_dict = env_json_dict
