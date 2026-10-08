"""Volume registry: persistent storage independent of sandbox lifetime."""

from __future__ import annotations

import errno
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from gateway_common.ids import access_token, sandbox_id
from gateway_common.paths import (
    validate_sandbox_id,
    write_json_atomically,
    write_text_atomically,
)
from gateway_common.timeutil import to_iso_z, utcnow


#: Type tag written into every volume record. Volume and sandbox records share
#: the ``e2b:record:<id>`` key space (both registries are constructed with the
#: same namespace), so records are self-describing: the sandbox read paths use
#: it to skip foreign payloads instead of trying to parse them
#: (``control_plane.registry.manager._is_sandbox_record_payload``). Legacy
#: records have no tag and are told apart by ``volume_id``/``sandbox_id``.
RECORD_KIND_VOLUME = "volume"


def _widen_ancestors_for_tenant_uids(volume_root: Path) -> None:
    """Give tenant uids a way *through* every ancestor of a volume root.

    The volume root is created here, in the control-plane process, but the
    sandbox opens that host path *as its own uid* (E3.2 / own-identity slot), so
    DAC needs ``o+x`` on every level between ``/`` and the volume view -- the
    ``1777`` on the root alone is not enough (A5,
    ``docs/production-deployment-requirements.md`` §2.4.2). A stack
    deployment gets that from the worker's mount path
    (``envd_service.volumes._ensure_shared_volume_root``); a combined
    ("合体") node creates the root here and never runs that half for its own
    volume roots, so creation has to apply the same rule to the same path.

    Reuses the worker's helper rather than a second copy, so the two sides of
    one deployment cannot drift; it only ever *adds* the x bits (best-effort,
    never fails provisioning). A separated control plane has no envd service
    and no tenant uids, and the import failure is then the no-op.
    """
    try:
        from envd_service.volumes import _ensure_traversable
    except ImportError:  # pragma: no cover - separated control-plane image
        return
    _ensure_traversable(volume_root)


class UnknownVolumeError(KeyError):
    pass


class VolumeRootNotOwnedError(RuntimeError):
    """The volume store is not writable by this control plane's own uid.

    C3 Task 5 (ruling **D24**): the control plane runs as **65534** and creates
    every volume as ``<store>/<volume_id>`` — an ordinary owner operation, not
    a privileged one — plus the record at ``<store>/_meta/<volume_id>.json``.
    The store's ownership is therefore a *deployment pre-condition*: it is
    handed over to 65534 once, **non-recursively** (the directories below it
    are sandbox volume data owned by pooled sandbox uids), by the agent's
    ``storage-init`` (``deploy/k8s/c3-agent.yaml``).

    Before D24 this showed up as a bare ``PermissionError`` from ``Path.mkdir``
    inside the create handler — the operator saw an errno at the first volume
    create and nothing told them which one-time command fixes it. Raising a
    named error that carries the exact command is what makes the pre-condition
    audible, and it is deliberately *not* a fallback: no privileged path is
    tried, because the plan's whole point is that the CP's A-class is empty.

    The text is **shape-neutral on purpose** (review round 1, minor 3): this
    module also runs in the separated compose stacks and in the ``local://``
    lane, so it names *this process's* uid (whatever it is) and the one-time,
    non-recursive hand-over rather than the k8s agent's node-level job — that
    script's own log is where the k8s remedy belongs, and it says the same
    thing in its own terms (``deploy/k8s/c3-agent.yaml``).
    """


def _volume_store_refusal(*, store: Path, path: Path) -> VolumeRootNotOwnedError:
    """The one wording for "this uid cannot write the store".

    Two call sites raise it — the store's own creation at startup
    (:meth:`VolumeRegistry.__init__`, which the compose and ``local://`` lanes
    do go through) and a volume's directory in :meth:`VolumeRegistry.create`
    (the A3 D24 recon narrowed to owner operations) — and they must not drift.
    """
    uid = os.geteuid()
    return VolumeRootNotOwnedError(
        f"cannot create {path}: the volume store {store} is not writable by "
        f"this control plane (uid {uid}). The store has to belong to that uid "
        "before a volume can be created in it or a record written beside it: "
        "hand it over once, non-recursively (never `-R`, since the directories "
        "below it are sandbox volume data owned by pooled sandbox uids): chown "
        f'{uid}:{uid} "{store}" "{store}/_meta"'
    )


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
    #: E3.3: token expiry (UTC; ``None`` = never expires, legacy semantics)
    #: and revocation flag. Revocation invalidates the token immediately;
    #: an expired token behaves identically (401 on the content API).
    token_expires_at: datetime | None = None
    token_revoked: bool = False

    def is_token_valid(self, now: datetime | None = None) -> bool:
        """True while the token is neither revoked nor past its TTL."""
        if self.token_revoked:
            return False
        if self.token_expires_at is None:
            return True
        return (now or utcnow()) < self.token_expires_at

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
        if self.token_expires_at is not None:
            payload["tokenExpiresAt"] = to_iso_z(self.token_expires_at)
        return payload

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            # Shared-store type tag (see RECORD_KIND_VOLUME).
            "kind": RECORD_KIND_VOLUME,
            "volume_id": self.volume_id,
            "name": self.name,
            "token": self.token,
            "node_id": self.node_id,
            "created_at": to_iso_z(self.created_at),
            "per_sandbox_quota_mb": self.per_sandbox_quota_mb,
            "tenant_id": self.tenant_id,
            "token_expires_at": (
                to_iso_z(self.token_expires_at)
                if self.token_expires_at is not None
                else None
            ),
            "token_revoked": self.token_revoked,
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
        expires = data.get("token_expires_at")
        if expires:
            try:
                expires_dt = _dt.fromisoformat(expires.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                expires_dt = None
        else:
            expires_dt = None
        return cls(
            volume_id=data["volume_id"],
            name=data["name"],
            token=data["token"],
            node_id=data.get("node_id", "local"),
            created_at=created_dt,
            path=path,
            per_sandbox_quota_mb=int(data.get("per_sandbox_quota_mb", 0)),
            tenant_id=data.get("tenant_id"),
            token_expires_at=expires_dt,
            token_revoked=bool(data.get("token_revoked", False)),
        )


class VolumeRegistry:
    def __init__(
        self,
        base_dir: str | Path,
        redis_client=None,
        token_ttl_seconds: int = 0,
        namespace: str = "e2b",
    ) -> None:
        self._base = Path(base_dir).resolve()
        try:
            self._base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Review round 1 (minor 2): the *same* store creation, one layer
            # earlier -- and the one the compose and `local://` lanes actually
            # take, because only the k8s pod mounts `<store>` as a subPath (so
            # there it always exists by the time this runs). Without the wrap a
            # store this uid cannot write fails at *startup* with a bare
            # `PermissionError`, which is the same un-named failure the create
            # path was fixed for.
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise _volume_store_refusal(
                    store=self._base, path=self._base
                ) from exc
            raise
        self._volumes: dict[str, VolumeRecord] = {}
        self._lock = threading.Lock()
        self._token_ttl_seconds = token_ttl_seconds
        self._record_store = None
        #: True once legacy disk-only records have been mirrored into the
        #: shared store (E3.3 review I1); the scan runs at most once per
        #: process so per-request lookups stay cheap.
        self._backfilled = False
        if redis_client is not None:
            from control_plane.registry.redis_backend import RedisRecordStore

            self._record_store = RedisRecordStore(redis_client, namespace)

    def create(
        self,
        name: str,
        node_id: str = "local",
        per_sandbox_quota_mb: int = 0,
        tenant_id: str | None = None,
        token_ttl_seconds: int | None = None,
    ) -> VolumeRecord:
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if (
            not isinstance(per_sandbox_quota_mb, int)
            or isinstance(per_sandbox_quota_mb, bool)
            or per_sandbox_quota_mb < 0
        ):
            raise ValueError("per_sandbox_quota_mb must be a non-negative integer")
        ttl = (
            self._token_ttl_seconds
            if token_ttl_seconds is None
            else token_ttl_seconds
        )
        if (
            not isinstance(ttl, int)
            or isinstance(ttl, bool)
            or ttl < 0
        ):
            raise ValueError("token_ttl_seconds must be a non-negative integer")
        with self._lock:
            volume_id = sandbox_id().replace("sbx_", "vol_")
            while volume_id in self._volumes:
                volume_id = sandbox_id().replace("sbx_", "vol_")
            tombstone = self._tombstone_path(volume_id)
            if tombstone.exists():
                tombstone.unlink()
            token_expires_at = (
                utcnow() + timedelta(seconds=ttl) if ttl > 0 else None
            )
            record = VolumeRecord(
                volume_id=volume_id,
                name=name,
                token=access_token(),
                node_id=node_id,
                path=self._base / volume_id,
                per_sandbox_quota_mb=per_sandbox_quota_mb,
                tenant_id=tenant_id,
                token_expires_at=token_expires_at,
            )
            try:
                record.path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # C3 Task 5 (D24): the CP is 65534, so this needs the store to
                # be *its* directory and nothing else. Name the pre-condition
                # instead of letting a bare EACCES escape (see the class).
                if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                    raise _volume_store_refusal(
                        store=self._base, path=record.path
                    ) from exc
                raise
            # E3.2 volume permission model: the volume root is shared across
            # sandboxes with distinct host uids, so it must be world
            # rwx (single-entry userns has no supplementary groups) with the
            # sticky bit preventing cross-uid deletion. Best-effort: a
            # filesystem that refuses the chmod keeps the platform default.
            # The chain above the root needs its own o+x pass (W4): the
            # sandbox reaches this path as the tenant uid, and a 0700
            # ancestor turns even an absolute volume path into EACCES.
            _widen_ancestors_for_tenant_uids(record.path)
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

    def _tombstone_path(self, volume_id: str) -> Path:
        # Disk tombstone marker: written by delete() before the record is
        # removed so a stale copy of the record (this replica's own disk,
        # a restored backup, ...) can never be backfilled after a restart.
        return self._base / "_meta" / f"{volume_id}.deleted"

    def _is_tombstoned_on_disk(self, volume_id: str) -> bool:
        return self._tombstone_path(volume_id).is_file()

    @staticmethod
    def _is_legacy_disk_payload(payload: dict[str, Any]) -> bool:
        """True only for records written before E3.3 (no token fields).

        E3.3+ ``_write_record`` always persists ``token_expires_at`` and
        ``token_revoked``, so a disk copy carrying either key is a stale
        cache of a Redis-mode replica. The shared store is authoritative
        for those records; mirroring them back on a Redis miss would
        resurrect volumes that another replica already deleted.
        """
        if not isinstance(payload, dict):
            return False
        return (
            "token_expires_at" not in payload
            and "token_revoked" not in payload
        )

    def _write_record(self, record: VolumeRecord) -> None:
        path = self._record_path(record.volume_id)
        # Atomic: this file is read by a peer with no record store, by the
        # worker side mounting the volume, and by ``_ensure_backfilled`` at
        # startup -- all of them while this replica may be rewriting it.
        write_json_atomically(path, record.to_storage_dict())
        if self._record_store is not None:
            self._record_store.put(
                record.volume_id, record.to_storage_dict(), ttl=None
            )

    def _load_record(self, volume_id: str) -> VolumeRecord:
        if self._record_store is not None:
            payload = self._record_store.get(volume_id)
            if payload is None:
                raise UnknownVolumeError(volume_id)
            return VolumeRecord.from_storage_dict(
                payload, self._base / volume_id
            )
        path = self._record_path(volume_id)
        if self._is_tombstoned_on_disk(volume_id) or not path.is_file():
            raise UnknownVolumeError(volume_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return VolumeRecord.from_storage_dict(payload, self._base / volume_id)

    def _ensure_backfilled(self) -> None:
        """Mirror legacy disk-only volume records into Redis once.

        Pre-Redis deployments keep volume records only in ``_meta/*.json``
        on disk. Enabling ``E2B_REDIS_URL`` without a migration would make
        every existing volume 404/401 and drop it from ``list()``. The
        first access writes any disk record Redis does not know yet into
        the shared store; records already present are never overwritten,
        so a concurrent replica's revocation/expiry stays authoritative.

        Two guards keep deleted volumes dead:
        * only pre-E3.3 records (no ``token_expires_at``/``token_revoked``
          keys) qualify — disk copies written by Redis-mode replicas are
          stale caches, not a source of truth;
        * tombstoned records (Redis marker or local ``.deleted`` file) are
          skipped, so a deletion by any replica survives restarts.
        """
        if self._record_store is None:
            return
        with self._lock:
            if self._backfilled:
                return
            self._backfilled = True
            meta_dir = self._base / "_meta"
            if not meta_dir.is_dir():
                return
            for path in sorted(meta_dir.glob("*.json")):
                volume_id = path.stem
                if self._is_tombstoned_on_disk(volume_id):
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if not self._is_legacy_disk_payload(payload):
                    continue
                if self._record_store.is_tombstoned(volume_id):
                    continue
                if self._record_store.get(volume_id) is not None:
                    continue
                try:
                    record = VolumeRecord.from_storage_dict(
                        payload, self._base / volume_id
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                self._record_store.put(volume_id, payload, ttl=None)
                self._volumes[volume_id] = record

    def save(self, record: VolumeRecord) -> VolumeRecord:
        """Persist a mutated record (revocation / expiry bookkeeping)."""
        with self._lock:
            self._volumes[record.volume_id] = record
        self._write_record(record)
        return record

    def revoke_token(self, volume_id: str) -> VolumeRecord:
        """Invalidate the volume's access token immediately (idempotent)."""
        record = self.get(volume_id)
        if not record.token_revoked:
            record.token_revoked = True
            self.save(record)
        return record

    def get(self, volume_id: str) -> VolumeRecord:
        if not validate_sandbox_id(volume_id):
            raise UnknownVolumeError(volume_id)
        if self._record_store is not None:
            # Redis-backed registries always read the shared store so a
            # mutation (token revocation, expiry) by another replica is
            # visible immediately.
            self._ensure_backfilled()
            payload = self._record_store.get(volume_id)
            if payload is None:
                raise UnknownVolumeError(volume_id)
            record = VolumeRecord.from_storage_dict(
                payload, self._base / volume_id
            )
            with self._lock:
                self._volumes[volume_id] = record
            return record
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
        # Tombstone first: the marker (shared store + this replica's disk)
        # stops any stale disk copy — this replica's or another one's —
        # from being backfilled into the shared store after a restart.
        tombstone = self._tombstone_path(volume_id)
        # Only its existence is read, but it is the one thing standing between
        # a deleted volume and a backfill, so it is published whole too.
        write_text_atomically(tombstone, "deleted\n")
        if self._record_store is not None:
            self._record_store.tombstone(volume_id)
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
        self._ensure_backfilled()
        if self._record_store is not None:
            # The shared store decides what exists: re-validate every
            # candidate (local disk glob + in-process cache) against Redis
            # so a volume deleted by another replica drops out of the
            # listing immediately, even without a restart.
            candidates = set(self._volumes)
            meta_dir = self._base / "_meta"
            if meta_dir.is_dir():
                with self._lock:
                    for path in meta_dir.glob("*.json"):
                        candidates.add(path.stem)
            records = []
            for volume_id in candidates:
                try:
                    records.append(self.get(volume_id))
                except UnknownVolumeError:
                    with self._lock:
                        self._volumes.pop(volume_id, None)
        else:
            with self._lock:
                meta_dir = self._base / "_meta"
                if meta_dir.is_dir():
                    for path in sorted(meta_dir.glob("*.json")):
                        volume_id = path.stem
                        if volume_id in self._volumes:
                            continue
                        try:
                            self._volumes[volume_id] = self._load_record(
                                volume_id
                            )
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
        if not record.is_token_valid() or record.token != token:
            raise UnknownVolumeError(volume_id)
        return record
