"""Quota-agent server configuration (E2.6)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from gateway_common.env import _env_int


def _parse_path_map(value: str | None) -> tuple[tuple[str, str], ...]:
    """Parse ``E2B_QUOTA_AGENT_PATH_MAP``: comma-separated ``client=server`` pairs.

    The NFS client and the server may see the same tree under different
    paths; each pair rewrites a client-side prefix to the server-side path
    before any ``xfs_quota`` call. Empty value = no rewriting (worker and
    server use the same path layout).
    """
    if not value:
        return ()
    pairs: list[tuple[str, str]] = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(
                f"E2B_QUOTA_AGENT_PATH_MAP entry {entry!r} must be client=server"
            )
        client, server = (part.strip() for part in entry.split("=", 1))
        if not client or not server:
            raise ValueError(
                f"E2B_QUOTA_AGENT_PATH_MAP entry {entry!r} must be client=server"
            )
        pairs.append((client, server))
    return tuple(pairs)


@dataclass
class Settings:
    token: str = field(
        default_factory=lambda: os.getenv("E2B_QUOTA_AGENT_TOKEN", "")
    )
    host: str = field(
        default_factory=lambda: os.getenv("E2B_QUOTA_AGENT_HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: _env_int("E2B_QUOTA_AGENT_PORT", 49984)
    )
    path_map: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: _parse_path_map(
            os.getenv("E2B_QUOTA_AGENT_PATH_MAP")
        )
    )

    def __post_init__(self) -> None:
        # Longest client prefix first so a nested export wins over its parent.
        self.path_map = tuple(
            sorted(self.path_map, key=lambda pair: len(pair[0]), reverse=True)
        )
