"""Secret registry: write-only values with versioning."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway_common.ids import sandbox_id
from gateway_common.timeutil import to_iso_z, utcnow


class UnknownSecretError(KeyError):
    pass


class SecretTenantMismatchError(ValueError):
    """Raised when a sandbox references a secret owned by another tenant."""


@dataclass
class SecretRecord:
    secret_id: str
    name: str
    value: str
    version: int = 1
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    tenant_id: str | None = None

    def as_model(self) -> dict:
        return {
            "secretID": self.secret_id,
            "name": self.name,
            "currentVersion": self.version,
            "metadata": self.metadata,
            "createdAt": to_iso_z(self.created_at),
            "updatedAt": to_iso_z(self.updated_at),
        }

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "secret_id": self.secret_id,
            "name": self.name,
            "value": self.value,
            "version": self.version,
            "metadata": dict(self.metadata),
            "created_at": to_iso_z(self.created_at),
            "updated_at": to_iso_z(self.updated_at),
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "SecretRecord":
        from datetime import datetime as _dt

        def _parse(value: str) -> datetime:
            try:
                return _dt.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return utcnow()

        return cls(
            secret_id=data["secret_id"],
            name=data["name"],
            value=data["value"],
            version=int(data.get("version", 1)),
            metadata=dict(data.get("metadata", {})),
            created_at=_parse(data.get("created_at")),
            updated_at=_parse(data.get("updated_at")),
            tenant_id=data.get("tenant_id"),
        )


class SecretRegistry:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._secrets: dict[str, SecretRecord] = {}
        self._by_name: dict[str, str] = {}
        self._lock = threading.Lock()
        self._base = Path(base_dir).resolve() if base_dir is not None else None
        if self._base is not None:
            self._base.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        value: str,
        metadata: dict[str, str] | None = None,
        tenant_id: str | None = None,
    ) -> SecretRecord:
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
                tenant_id=tenant_id,
            )
            self._secrets[secret_id] = record
            self._by_name[name] = secret_id
            self._write_record(record)
            return record

    def _record_path(self, secret_id: str) -> Path | None:
        if self._base is None:
            return None
        return self._base / secret_id / "secret.json"

    def _write_record(self, record: SecretRecord) -> None:
        path = self._record_path(record.secret_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record.to_storage_dict(), separators=(",", ":")),
            encoding="utf-8",
        )

    def _scan_disk(self) -> None:
        if self._base is None or not self._base.is_dir():
            return
        for entry in sorted(self._base.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / "secret.json"
            if not path.is_file():
                continue
            secret_id = entry.name
            if secret_id in self._secrets:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = SecretRecord.from_storage_dict(payload)
                self._secrets[record.secret_id] = record
                self._by_name[record.name] = record.secret_id
            except (OSError, ValueError, KeyError):
                continue

    def get(self, secret_id: str) -> SecretRecord:
        with self._lock:
            record = self._secrets.get(secret_id)
            if record is None:
                self._scan_disk()
                record = self._secrets.get(secret_id)
        if record is None:
            raise UnknownSecretError(secret_id)
        return record

    def get_by_name(self, name: str) -> SecretRecord:
        with self._lock:
            self._scan_disk()
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
            self._write_record(record)
        return record

    def delete(self, secret_id: str) -> SecretRecord:
        record = self.get(secret_id)
        with self._lock:
            self._secrets.pop(secret_id, None)
            self._by_name.pop(record.name, None)
        path = self._record_path(secret_id)
        if path is not None:
            import shutil

            shutil.rmtree(path.parent, ignore_errors=True)
        return record

    def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        tenant_id: str | None = None,
    ) -> list[SecretRecord]:
        with self._lock:
            self._scan_disk()
            values = list(self._secrets.values())
        records = sorted(values, key=lambda r: r.created_at, reverse=True)
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        if limit is not None:
            records = records[offset : offset + limit]
        return records

    def resolve_env_refs(
        self,
        env_vars: dict[str, str],
        tenant_id: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, str]:
        """Resolve ``${secret_name}`` references in env var values.

        When tenant isolation is active (``tenant_id`` set and caller not an
        admin), referencing a secret owned by another tenant raises
        :class:`SecretTenantMismatchError` (the API maps it to 403).
        """
        resolved: dict[str, str] = {}
        for key, value in env_vars.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                name = value[2:-1]
                try:
                    secret = self.get_by_name(name)
                    if (
                        tenant_id is not None
                        and not is_admin
                        and secret.tenant_id != tenant_id
                    ):
                        raise SecretTenantMismatchError(
                            f"Secret {name} does not belong to this tenant"
                        )
                    resolved[key] = secret.value
                    continue
                except UnknownSecretError:
                    pass
            resolved[key] = value
        return resolved
