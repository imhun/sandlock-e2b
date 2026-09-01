"""Filesystem-level sandbox snapshots (cold-boot semantics).

A snapshot captures the sandbox's filesystem and creation metadata but not
running processes or memory — the official ``keep_memory=false`` cold-boot
semantics. Snapshots are independent of sandbox lifetime and can be used to
create new sandboxes or fork existing ones.
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway_common.ids import sandbox_id
from gateway_common.paths import validate_sandbox_id
from gateway_common.timeutil import to_iso_z, utcnow


class UnknownSnapshotError(KeyError):
    pass


@dataclass
class SnapshotRecord:
    snapshot_id: str
    names: list[str]
    created_at: datetime = field(default_factory=utcnow)
    template_id: str = "base"
    env_vars: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    volume_mounts: list[dict[str, str]] = field(default_factory=list)
    base_image: str | None = None
    allow_internet_access: bool = False
    node_id: str = "local"
    fs_path: Path | None = None
    tenant_id: str | None = None

    def as_snapshot_info(self) -> dict[str, Any]:
        return {
            "snapshotID": self.snapshot_id,
            "names": list(self.names),
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = to_iso_z(self.created_at)
        data.pop("fs_path", None)
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any], fs_path: Path) -> "SnapshotRecord":
        from datetime import datetime as _dt

        created = payload.get("created_at")
        try:
            created_dt = _dt.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            created_dt = utcnow()
        return cls(
            snapshot_id=payload["snapshot_id"],
            names=list(payload.get("names", [])),
            created_at=created_dt,
            template_id=payload.get("template_id", "base"),
            env_vars=dict(payload.get("env_vars", {})),
            metadata=dict(payload.get("metadata", {})),
            volume_mounts=list(payload.get("volume_mounts", [])),
            base_image=payload.get("base_image"),
            allow_internet_access=bool(payload.get("allow_internet_access", False)),
            node_id=payload.get("node_id", "local"),
            fs_path=fs_path,
            tenant_id=payload.get("tenant_id"),
        )


class SnapshotRegistry:
    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir).resolve()
        self._snapshots: dict[str, SnapshotRecord] = {}
        self._lock = threading.Lock()
        self._base.mkdir(parents=True, exist_ok=True)

    def _snapshot_dir(self, snapshot_id: str) -> Path:
        return self._base / snapshot_id

    def _fs_path(self, snapshot_id: str) -> Path:
        return self._snapshot_dir(snapshot_id) / "fs"

    def create_from_sandbox(
        self,
        *,
        workspace_dir: str | Path,
        template_id: str,
        env_vars: dict[str, str],
        metadata: dict[str, str],
        volume_mounts: list[dict[str, str]],
        base_image: str | None,
        allow_internet_access: bool,
        node_id: str = "local",
        name: str | None = None,
        snapshot_id: str | None = None,
        copy_fs: bool = True,
        tenant_id: str | None = None,
    ) -> SnapshotRecord:
        snapshot_id = snapshot_id or sandbox_id().replace("sbx_", "snap_")
        fs_path = self._fs_path(snapshot_id)
        if copy_fs:
            shutil.copytree(
                Path(workspace_dir), fs_path, symlinks=True, dirs_exist_ok=False
            )
        record = SnapshotRecord(
            snapshot_id=snapshot_id,
            names=[name] if name else [],
            template_id=template_id,
            env_vars=dict(env_vars),
            metadata=dict(metadata),
            volume_mounts=[dict(m) for m in volume_mounts],
            base_image=base_image,
            allow_internet_access=bool(allow_internet_access),
            node_id=node_id,
            fs_path=fs_path,
            tenant_id=tenant_id,
        )
        self._write_record(record)
        with self._lock:
            self._snapshots[snapshot_id] = record
        return record

    def _write_record(self, record: SnapshotRecord) -> None:
        path = self._snapshot_dir(record.snapshot_id) / "snapshot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record.to_dict(), separators=(",", ":")), encoding="utf-8"
        )

    def get(self, snapshot_id: str) -> SnapshotRecord:
        if not validate_sandbox_id(snapshot_id):
            raise UnknownSnapshotError(snapshot_id)
        with self._lock:
            record = self._snapshots.get(snapshot_id)
            if record is not None:
                return record
        path = self._snapshot_dir(snapshot_id) / "snapshot.json"
        if not path.is_file():
            raise UnknownSnapshotError(snapshot_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = SnapshotRecord.from_dict(payload, self._fs_path(snapshot_id))
        with self._lock:
            self._snapshots[snapshot_id] = record
        return record

    def delete(self, snapshot_id: str) -> SnapshotRecord:
        record = self.get(snapshot_id)
        with self._lock:
            self._snapshots.pop(snapshot_id, None)
        shutil.rmtree(self._snapshot_dir(snapshot_id), ignore_errors=True)
        return record

    def list(
        self,
        *,
        sandbox_id_filter: str | None = None,
        name: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        tenant_id: str | None = None,
    ) -> list[SnapshotRecord]:
        records = sorted(
            self._snapshots.values(), key=lambda r: r.created_at, reverse=True
        )
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        if name:
            records = [r for r in records if name in r.names]
        if limit is not None:
            records = records[offset : offset + limit]
        return records

    def expand_to(self, record: SnapshotRecord, dest: str | Path) -> Path:
        """Copy a snapshot's filesystem into a new sandbox workspace."""
        target = Path(dest)
        shutil.copytree(
            record.fs_path, target, symlinks=True, dirs_exist_ok=True
        )
        return target
