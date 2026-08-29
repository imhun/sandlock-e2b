"""Random ID generation for sandboxes, clients and tokens."""

from __future__ import annotations

import secrets


def _token(prefix: str, length: int = 16) -> str:
    return f"{prefix}_{secrets.token_hex(length // 2 + 1)[:length]}"


def sandbox_id() -> str:
    return _token("sbx", 16)


def client_id() -> str:
    return _token("cli", 12)


def access_token() -> str:
    return _token("tok", 24)


def watcher_id() -> str:
    return _token("watch", 12)

