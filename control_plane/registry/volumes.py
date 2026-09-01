"""Volume registry: persistent storage independent of sandbox lifetime."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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
            )
            record.path.mkdir(parents=True, exist_ok=True)
            self._volumes[volume_id] = record
            return record

    def get(self, volume_id: str) -> VolumeRecord:
        if not validate_sandbox_id(volume_id):
            raise UnknownVolumeError(volume_id)
        with self._lock:
            record = self._volumes.get(volume_id)
        if record is None:
            raise UnknownVolumeError(volume_id)
        return record

    def delete(self, volume_id: str) -> VolumeRecord:
        record = self.get(volume_id)
        with self._lock:
            self._volumes.pop(volume_id, None)
        if record.path is not None:
            import shutil

            shutil.rmtree(record.path, ignore_errors=True)
        return record

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[VolumeRecord]:
        records = sorted(
            self._volumes.values(), key=lambda r: r.created_at, reverse=True
        )
        if limit is not None:
            records = records[offset : offset + limit]
        return records

    def verify_token(self, volume_id: str, token: str) -> VolumeRecord:
        record = self.get(volume_id)
        if record.token != token:
            raise UnknownVolumeError(volume_id)
        return record
