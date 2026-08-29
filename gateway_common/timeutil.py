"""RFC3339/ISO-8601 UTC helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    """Serialize a datetime as ``2026-08-28T10:00:00.000Z`` (UTC, ms)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

