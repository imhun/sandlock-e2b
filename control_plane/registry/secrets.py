"""Secret registry: write-only values with versioning."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from gateway_common.ids import sandbox_id
from gateway_common.timeutil import to_iso_z, utcnow


class UnknownSecretError(KeyError):
    pass


@dataclass
class SecretRecord:
    secret_id: str
    name: str
    value: str
    version: int = 1
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def as_model(self) -> dict:
        return {
            "secretID": self.secret_id,
            "name": self.name,
            "currentVersion": self.version,
            "metadata": self.metadata,
            "createdAt": to_iso_z(self.created_at),
            "updatedAt": to_iso_z(self.updated_at),
        }


class SecretRegistry:
    def __init__(self) -> None:
        self._secrets: dict[str, SecretRecord] = {}
        self._by_name: dict[str, str] = {}
        self._lock = threading.Lock()

    def create(self, name: str, value: str, metadata: dict[str, str] | None = None) -> SecretRecord:
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if value is None:
            raise ValueError("value is required")
        with self._lock:
            if name in self._by_name:
                raise ValueError(f"Secret {name} already exists")
            secret_id = sandbox_id().replace("sbx_", "sec_")
            record = SecretRecord(
                secret_id=secret_id,
                name=name,
                value=str(value),
                metadata=dict(metadata or {}),
            )
            self._secrets[secret_id] = record
            self._by_name[name] = secret_id
            return record

    def get(self, secret_id: str) -> SecretRecord:
        with self._lock:
            record = self._secrets.get(secret_id)
        if record is None:
            raise UnknownSecretError(secret_id)
        return record

    def get_by_name(self, name: str) -> SecretRecord:
        with self._lock:
            secret_id = self._by_name.get(name)
        if secret_id is None:
            raise UnknownSecretError(name)
        return self.get(secret_id)

    def update(
        self, secret_id: str, value: str | None, metadata: dict[str, str] | None = None
    ) -> SecretRecord:
        record = self.get(secret_id)
        with self._lock:
            if value is not None:
                record.value = str(value)
                record.version += 1
            if metadata is not None:
                record.metadata = dict(metadata)
            record.updated_at = utcnow()
        return record

    def delete(self, secret_id: str) -> SecretRecord:
        record = self.get(secret_id)
        with self._lock:
            self._secrets.pop(secret_id, None)
            self._by_name.pop(record.name, None)
        return record

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[SecretRecord]:
        records = sorted(
            self._secrets.values(), key=lambda r: r.created_at, reverse=True
        )
        if limit is not None:
            records = records[offset : offset + limit]
        return records

    def resolve_env_refs(self, env_vars: dict[str, str]) -> dict[str, str]:
        """Resolve ``${secret_name}`` references in env var values."""
        resolved: dict[str, str] = {}
        for key, value in env_vars.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                name = value[2:-1]
                try:
                    resolved[key] = self.get_by_name(name).value
                    continue
                except UnknownSecretError:
                    pass
            resolved[key] = value
        return resolved

