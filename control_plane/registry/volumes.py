"""Volume registry: persistent storage independent of sandbox lifetime."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway_common.ids import access_token, sandbox_id
from gateway_common.paths import validate_sandbox_id
from gateway_common.timeutil import to_iso_z, utcnow


class UnknownVolumeError(KeyError):
    pass


@dataclass
class VolumeRecord:
    volume_id: str
    name: str
    token: str
    node_id: str = "local"
    created_at: datetime = field(default_factory=utcnow)
    path: Path | None = None
    #: Per-sandbox disk quota (MB) applied to every sandbox mounting this
    #: volume, set uniformly at creation (E2.5). 0 = no per-sandbox limit
    #: (backward-compatible: sandboxes mount the volume root as before).
    per_sandbox_quota_mb: int = 0
    tenant_id: str | None = None

    def as_volume(self) -> dict:
        return {
            "volumeID": self.volume_id,
            "name": self.name,
            "createdAt": to_iso_z(self.created_at),
            "perSandboxQuotaMb": self.per_sandbox_quota_mb,
        }

    def as_volume_and_token(self) -> dict:
        payload = self.as_volume()
        payload["token"] = self.token
        return payload

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "volume_id": self.volume_id,
            "name": self.name,
            "token": self.token,
            "node_id": self.node_id,
            "created_at": to_iso_z(self.created_at),
            "per_sandbox_quota_mb": self.per_sandbox_quota_mb,
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_storage_dict(
        cls, data: dict[str, Any], path: Path
    ) -> "VolumeRecord":
        from datetime import datetime as _dt

        created = data.get("created_at")
        try:
            created_dt = _dt.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            created_dt = utcnow()
        return cls(
            volume_id=data["volume_id"],
            name=data["name"],
            token=data["token"],
            node_id=data.get("node_id", "local"),
            created_at=created_dt,
            path=path,
            per_sandbox_quota_mb=int(data.get("per_sandbox_quota_mb", 0)),
            tenant_id=data.get("tenant_id"),
        )


class VolumeRegistry:
    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)
        self._volumes: dict[str, VolumeRecord] = {}
        self._lock = threading.Lock()

    def create(
        self,
        name: str,
        node_id: str = "local",
        per_sandbox_quota_mb: int = 0,
        tenant_id: str | None = None,
    ) -> VolumeRecord:
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if (
            not isinstance(per_sandbox_quota_mb, int)
            or isinstance(per_sandbox_quota_mb, bool)
            or per_sandbox_quota_mb < 0
        ):
            raise ValueError("per_sandbox_quota_mb must be a non-negative integer")
        with self._lock:
            volume_id = sandbox_id().replace("sbx_", "vol_")
            while volume_id in self._volumes:
                volume_id = sandbox_id().replace("sbx_", "vol_")
            record = VolumeRecord(
                volume_id=volume_id,
                name=name,
                token=access_token(),
                node_id=node_id,
                path=self._base / volume_id,
                per_sandbox_quota_mb=per_sandbox_quota_mb,
                tenant_id=tenant_id,
            )
            record.path.mkdir(parents=True, exist_ok=True)
            # E3.2 volume permission model: the volume root is shared across
            # sandboxes with distinct host uids, so it must be world
            # rwx (single-entry userns has no supplementary groups) with the
            # sticky bit preventing cross-uid deletion. Best-effort: a
            # filesystem that refuses the chmod keeps the platform default.
            try:
                os.chmod(record.path, 0o1777)
            except OSError:
                pass
            self._write_record(record)
            self._volumes[volume_id] = record
            return record

    def _record_path(self, volume_id: str) -> Path:
        # Records live outside the volume data directory so they are never
        # exposed through the volume content API.
        return self._base / "_meta" / f"{volume_id}.json"

    def _write_record(self, record: VolumeRecord) -> None:
        path = self._record_path(record.volume_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record.to_storage_dict(), separators=(",", ":")),
            encoding="utf-8",
        )

    def _load_record(self, volume_id: str) -> VolumeRecord:
        path = self._record_path(volume_id)
        if not path.is_file():
            raise UnknownVolumeError(volume_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return VolumeRecord.from_storage_dict(payload, self._base / volume_id)

    def get(self, volume_id: str) -> VolumeRecord:
        if not validate_sandbox_id(volume_id):
            raise UnknownVolumeError(volume_id)
        with self._lock:
            record = self._volumes.get(volume_id)
            if record is None:
                try:
                    record = self._load_record(volume_id)
                    self._volumes[volume_id] = record
                except UnknownVolumeError:
                    record = None
        if record is None:
            raise UnknownVolumeError(volume_id)
        return record

    def delete(self, volume_id: str) -> VolumeRecord:
        record = self.get(volume_id)
        with self._lock:
            self._volumes.pop(volume_id, None)
        self._record_path(volume_id).unlink(missing_ok=True)
        if record.path is not None:
            import shutil

            shutil.rmtree(record.path, ignore_errors=True)
        return record

    def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        tenant_id: str | None = None,
    ) -> list[VolumeRecord]:
        with self._lock:
            meta_dir = self._base / "_meta"
            if meta_dir.is_dir():
                for path in sorted(meta_dir.glob("*.json")):
                    volume_id = path.stem
                    if volume_id in self._volumes:
                        continue
                    try:
                        self._volumes[volume_id] = self._load_record(volume_id)
                    except (OSError, ValueError, KeyError):
                        continue
            records = list(self._volumes.values())
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        records = sorted(
            records, key=lambda r: r.created_at, reverse=True
        )
        if limit is not None:
            records = records[offset : offset + limit]
        return records

    def verify_token(self, volume_id: str, token: str) -> VolumeRecord:
        record = self.get(volume_id)
        if record.token != token:
            raise UnknownVolumeError(volume_id)
        return record
