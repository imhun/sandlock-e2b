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


def _is_within(child: Path, parent: Path) -> bool:
    """``child`` is equal to or under ``parent`` (both already resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_snapshot_root(path: Path) -> bool:
    """The directory itself is a snapshot root (``_write_record`` always
    emits ``snapshot.json`` there)."""
    return (path / "snapshot.json").is_file()


def _contains_snapshot_root(path: Path, depth: int) -> bool:
    """``path`` is a snapshot root, or a bounded-depth subtree of it holds
    one.  The bound keeps the copytree ignore callback cheap on ordinary
    workspaces (a few levels per visited directory) while still catching a
    registry store that was carried in with its markers intact but nested
    deeper than the direct-child shape (``snap_X/fs/snap_Y/...``)."""
    if _is_snapshot_root(path):
        return True
    if depth <= 0:
        return False
    try:
        for child in path.iterdir():
            if child.is_dir() and _contains_snapshot_root(child, depth - 1):
                return True
    except OSError:
        # Permission/race: copy it as an ordinary directory rather than
        # dropping the whole tree.
        pass
    return False


def _holds_snapshots(path: Path) -> bool:
    """``path`` is a snapshot root, or its subtree (bounded) holds one —
    i.e. a registry store directory was carried into the workspace."""
    return _contains_snapshot_root(path, depth=3)


def _prune_store(directory: str, names: list[str]) -> set[str]:
    """``copytree`` ignore callback pruning embedded snapshot stores.

    Only the outermost store directory is dropped: the walk never descends
    into it, so an embedded ``snap_X/fs/snap_X/fs/...`` chain cannot form.

    Boundary note (G2 review; #14): detection is heuristic -- a directory
    counts as a store when it is itself a snapshot root or its subtree holds
    one within a bounded depth (3 levels; markers intact). A store whose
    marker is missing or renamed, or a permission/race failure inside
    ``_holds_snapshots``, still degrades to copying the directory as ordinary
    content: that alone cannot re-form the exponential chain, but it can
    still carry store bytes into a snapshot. The positive same-name case and
    the nested-store case are pinned in tests/unit/test_snapshot_registry.py;
    the marker-less remainder is deliberate and accepted for now.
    """
    here = Path(directory)
    return {
        name
        for name in names
        if _holds_snapshots(here / name)
    }


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
        """Where one snapshot's record *and* payload live.

        ``<base>/_snapshots/<id>`` -- deliberately the same directory the
        worker's ``/agent/snapshots`` copies the filesystem into, so the
        record and its ``fs/`` are one object in one place. It used to be
        ``<base>/<id>`` here while the worker wrote ``<base>/_snapshots/<id>``,
        i.e. two layouts for the same snapshot; that mattered once the control
        plane started mounting the shared volume read-only (OBS-9), because the
        root-level form has nowhere to be mounted back read-write.
        """
        return self._base / "_snapshots" / snapshot_id

    def _fs_path(self, snapshot_id: str) -> Path:
        return self._snapshot_dir(snapshot_id) / "fs"

    def payload_path(self, snapshot_id: str) -> Path:
        """Where this snapshot's filesystem lives (N29 idempotency checks).

        Public because the create endpoint has to ask "is the payload already
        there?" for a retried idempotency key without reaching for the private
        name: the local shape copies in-process, so the filesystem -- not a
        worker route -- is what answers that question.
        """
        return self._fs_path(snapshot_id)

    def _legacy_snapshot_dir(self, snapshot_id: str) -> Path:
        """The pre-OBS-9 root-level layout, still read (and removed) if present."""
        return self._base / snapshot_id

    def _record_path(self, snapshot_id: str) -> tuple[Path, Path]:
        """``(record_path, fs_path)`` for the layout this snapshot actually uses.

        The two are returned together because a legacy record's payload lives
        in the legacy directory: ``SnapshotRecord.from_dict`` takes the fs path
        as an argument rather than from the file, so pairing them here is what
        keeps snapshots taken before this change readable.
        """
        current = self._snapshot_dir(snapshot_id) / "snapshot.json"
        if current.is_file():
            return current, self._fs_path(snapshot_id)
        legacy = self._legacy_snapshot_dir(snapshot_id) / "snapshot.json"
        if legacy.is_file():
            return legacy, legacy.parent / "fs"
        return current, self._fs_path(snapshot_id)

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
            src = Path(workspace_dir).resolve()
            dst = Path(fs_path).resolve()
            if _is_within(dst, src):
                raise ValueError(
                    f"snapshot destination {dst} is inside its source {src}"
                )
            shutil.copytree(
                src,
                dst,
                symlinks=True,
                dirs_exist_ok=False,
                ignore=_prune_store,
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
        path, fs_path = self._record_path(snapshot_id)
        if not path.is_file():
            raise UnknownSnapshotError(snapshot_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = SnapshotRecord.from_dict(payload, fs_path)
        with self._lock:
            self._snapshots[snapshot_id] = record
        return record

    def delete(self, snapshot_id: str) -> SnapshotRecord:
        record = self.get(snapshot_id)
        with self._lock:
            self._snapshots.pop(snapshot_id, None)
        shutil.rmtree(self._snapshot_dir(snapshot_id), ignore_errors=True)
        # A snapshot written before the layout change still occupies its old
        # directory; deleting only the new one would leave the copy behind.
        shutil.rmtree(self._legacy_snapshot_dir(snapshot_id), ignore_errors=True)
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
        target = Path(dest).resolve()
        source = Path(record.fs_path).resolve()
        if _is_within(target, source):
            raise ValueError(
                f"snapshot destination {target} is inside its source {source}"
            )
        shutil.copytree(
            source, target, symlinks=True, dirs_exist_ok=True, ignore=_prune_store
        )
        return target
