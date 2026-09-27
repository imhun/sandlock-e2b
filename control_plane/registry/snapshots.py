"""Filesystem-level sandbox snapshots (cold-boot semantics).

A snapshot captures the sandbox's filesystem and creation metadata but not
running processes or memory — the official ``keep_memory=false`` cold-boot
semantics. Snapshots are independent of sandbox lifetime and can be used to
create new sandboxes or fork existing ones.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway_common.ids import sandbox_id
from gateway_common.paths import validate_sandbox_id, write_json_atomically
from gateway_common.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)


#: How long the *named* copy claim lives (F11 step 3). It has no refresher
#: behind it, so it has to outlive one whole synchronous copy on its own: the
#: worker call times out at 120 s and N32 measured 76 s for a 2000-file tree.
COPY_CLAIM_TTL_S = 600

#: The copy *lease* (N46): the TTL a claim lives without a refresh, and how
#: often its owner refreshes it. Much shorter than the named claim above and
#: refreshed on purpose: the point of the lease is that it *expires*, so an
#: owner that died mid-copy stops looking like a live one within seconds
#: instead of holding the id for ten minutes. The TTL is several refresh
#: intervals long, so a copy keeps its lease across an ordinary stall of the
#: control-plane loop rather than losing it to one missed tick.
COPY_LEASE_TTL_S = 30
COPY_LEASE_REFRESH_S = 10


def _text(value) -> str | None:
    """A store value as text (``bytes`` when the client is not decoding)."""
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


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
    #: The sandbox this was captured from. Kept so an interrupted *async*
    #: capture (N29) can be resumed or failed after a restart without asking
    #: the caller again.
    sandbox_id: str | None = None
    #: ``completed`` | ``creating`` | ``failed``. Records are born completed;
    #: only the async shape reserves an id before any bytes move.
    status: str = "completed"
    #: Why a capture failed (async shape only). Short, user-facing text.
    error: str | None = None

    def as_snapshot_info(self) -> dict[str, Any]:
        return {
            "snapshotID": self.snapshot_id,
            "names": list(self.names),
        }

    def as_snapshot_status(self) -> dict[str, Any]:
        """`as_snapshot_info` plus where the capture got to (N29 async).

        The sync shape keeps the two-field body it always had; this one is what
        a 202 answer and the poll endpoint return, so a client can tell
        "still copying" from "done" without inferring it from a missing field.
        """
        info = self.as_snapshot_info()
        info["status"] = self.status
        if self.error:
            info["error"] = self.error
        return info

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
            sandbox_id=payload.get("sandbox_id"),
            status=payload.get("status", "completed"),
            error=payload.get("error"),
        )


class SnapshotRegistry:
    def __init__(
        self,
        base_dir: str | Path,
        *,
        redis_client=None,
        namespace: str = "e2b",
    ) -> None:
        self._base = Path(base_dir).resolve()
        #: Where the snapshots themselves live: ``<base>/_snapshots``.
        #: Resolved once here, from the root ``create_app`` hands us -- the
        #: *shared export root* (N27), never the tree root: once the tree root
        #: sinks (``<export>/workspaces``) that directory holds sandbox trees,
        #: and re-deriving this path from it at a read site is how a snapshot
        #: lookup would start reading somebody's sandbox. ``_snapshots`` is
        #: deliberately not part of the state base (N27 Task 3).
        self._snapshots_root = self._base / "_snapshots"
        self._snapshots: dict[str, SnapshotRecord] = {}
        self._lock = threading.Lock()
        self._redis = redis_client
        self._ns = namespace
        self._base.mkdir(parents=True, exist_ok=True)

    # -- the copy claim (F11 step 3) ---------------------------------------

    def _copy_key(self, snapshot_id: str) -> str:
        return f"{self._ns}:snapshot:copy:{snapshot_id}"

    def try_acquire_copy(
        self,
        snapshot_id: str,
        *,
        ttl_s: int = COPY_CLAIM_TTL_S,
        token: str | None = None,
    ) -> bool:
        """Claim the *copy* for one snapshot id across replicas.

        The record's ``creating`` status is the durable half of this ("somebody
        is copying id X"): it lives on the shared volume, so every replica can
        read it. What a record cannot do is close the window between "no record
        yet" and "record written" -- two replicas can both look, both miss, and
        both copy the same tree into the same directory. That window is what
        this key closes.

        Two shapes of caller, and the difference is who the claim belongs to
        (N46):

        * ``token=None`` -- the original claim. The *named* request path takes
          it before the copy starts (and releases it when the record carries
          the answer), and the reconcile pass takes it to ask "is somebody
          copying this id?" before re-driving a record. The TTL is
          ``COPY_CLAIM_TTL_S``: no refresher is behind either caller, so it has
          to outlive one copy on its own.
        * ``token=<id>`` -- the copy *lease*. The replica running a copy takes
          the id under a token it can refresh (:meth:`refresh_copy`), so an
          in-flight copy stays claimed for as long as it runs and an owner that
          dies stops being claimed after ``COPY_LEASE_TTL_S``. Answering ``True``
          for an id already held with *this* token is the "adopt the claim I
          just took" case; a rival's token is refused.

        Without Redis there is one process, so the in-process lock is the whole
        answer and this returns ``True``.
        """
        if self._redis is None:
            return True
        key = self._copy_key(snapshot_id)
        try:
            if token is None:
                return bool(self._redis.set(key, "1", nx=True, ex=ttl_s))
            if self._redis.set(key, token, nx=True, ex=max(1, int(ttl_s))):
                return True
            # Already claimed: ours to refresh, or somebody else's to respect.
            return _text(self._redis.get(key)) == token
        except Exception:  # pragma: no cover - defensive
            logger.warning("snapshot copy claim failed; proceeding", exc_info=True)
            return True

    def refresh_copy(
        self, snapshot_id: str, token: str, *, ttl_s: int = COPY_LEASE_TTL_S
    ) -> bool:
        """Extend *this replica's* copy lease; ``False`` when it is not ours.

        The owner of an in-flight copy has to keep saying so (N46): a copy can
        run for minutes, and a lease that outlived its owner would be the
        opposite mistake from the one this whole mechanism fixes. The value is
        the token rather than a bare ``1`` precisely so this can be a
        *conditional* refresh -- an unconditional "extend this key" would also
        extend a peer's lease, and hiding a peer's expiry is what would make a
        settled record look live again.

        A store that cannot be reached answers ``False``: the caller keeps
        copying (the bytes are the user's work) and says so in the log, because
        what a lost lease changes is *who a peer may settle*, not what this
        replica is doing.
        """
        if self._redis is None:
            return True
        key = self._copy_key(snapshot_id)
        try:
            if _text(self._redis.get(key)) != token:
                return False
            self._redis.expire(key, max(1, int(ttl_s)))
            return True
        except Exception:  # pragma: no cover - defensive
            logger.warning("snapshot copy lease refresh failed", exc_info=True)
            return False

    def release_copy(self, snapshot_id: str, *, token: str | None = None) -> None:
        """Drop the claim for one snapshot id.

        ``token=None`` releases unconditionally (the legacy shape, used by
        callers that know the claim is theirs); with a token the claim is only
        dropped when it is still *that* lease, so a release that arrives after
        the lease expired and a peer took it cannot delete the peer's claim.
        """
        if self._redis is None:
            return
        key = self._copy_key(snapshot_id)
        try:
            if token is None:
                self._redis.delete(key)
            elif _text(self._redis.get(key)) == token:
                self._redis.delete(key)
        except Exception:  # pragma: no cover - defensive
            logger.warning("snapshot copy claim release failed", exc_info=True)

    def try_acquire_reconcile(self, *, ttl_s: float) -> bool:
        """Claim one round of the reconcile pass (N46).

        Same shape as ``NodeRegistry.try_acquire_sweep`` and the TTL sweeper's
        ``try_claim``: the pass settles records every replica can see, so a
        second replica running the same round is duplicated work -- and a
        duplicated ``mark_failed``/re-drive -- rather than extra coverage. A
        replica that dies mid-round costs the fleet one round. Without Redis
        there is one process, which is the sweeper by definition.
        """
        if self._redis is None:
            return True
        try:
            return bool(
                self._redis.set(
                    f"{self._ns}:snapshot:reconcile",
                    "1",
                    nx=True,
                    ex=max(1, int(ttl_s)),
                )
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "snapshot reconcile claim failed; sweeping anyway", exc_info=True
            )
            return True

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
        return self._snapshots_root / snapshot_id

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
        """The pre-OBS-9 root-level layout, still read (and removed) if present.

        Root-level on purpose and unchanged by N27: it describes where a
        snapshot *was*, and the read paths above are the ones that had to stop
        inferring the platform's directory from whatever base they were handed.
        """
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
        source_sandbox_id: str | None = None,
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
            sandbox_id=source_sandbox_id,
            status="completed",
        )
        self._write_record(record)
        with self._lock:
            self._snapshots[snapshot_id] = record
        return record

    def reserve_from_sandbox(
        self,
        *,
        template_id: str,
        env_vars: dict[str, str],
        metadata: dict[str, str],
        volume_mounts: list[dict[str, str]],
        base_image: str | None,
        allow_internet_access: bool,
        source_sandbox_id: str | None,
        node_id: str = "local",
        name: str | None = None,
        snapshot_id: str | None = None,
        tenant_id: str | None = None,
    ) -> SnapshotRecord:
        """Claim an id for a copy that has not run yet (N29, async shape).

        The record exists from the moment the caller is answered with 202 --
        that is what makes the id pollable and the retry idempotent -- and the
        copy later fills ``fs/`` and flips ``status`` through
        :meth:`mark_completed` / :meth:`mark_failed`. Nothing here touches the
        workspace, so it is cheap and safe to call inside the request.
        """
        snapshot_id = snapshot_id or sandbox_id().replace("sbx_", "snap_")
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
            fs_path=self._fs_path(snapshot_id),
            tenant_id=tenant_id,
            sandbox_id=source_sandbox_id,
            status="creating",
        )
        self._write_record(record)
        with self._lock:
            self._snapshots[snapshot_id] = record
        return record

    def mark_completed(self, snapshot_id: str) -> SnapshotRecord:
        """The copy for `snapshot_id` finished; publish the record as usable."""
        record = self.get(snapshot_id)
        record.status = "completed"
        record.error = None
        self._write_record(record)
        return record

    def mark_failed(self, snapshot_id: str, error: str) -> SnapshotRecord:
        """The copy for `snapshot_id` did not finish.

        The record stays (the id is claimed, and a retry with the same key has
        to be able to answer with *why* rather than 404), but it is never
        handed out as a usable snapshot: every reader checks the status.
        """
        record = self.get(snapshot_id)
        record.status = "failed"
        record.error = error[:500]
        self._write_record(record)
        return record

    def in_progress(self) -> list[SnapshotRecord]:
        """Every record whose copy has not finished (startup reconciliation)."""
        for path in sorted(self._snapshots_root.glob("*/snapshot.json")):
            snapshot_id = path.parent.name
            try:
                record = self.get(snapshot_id)
            except UnknownSnapshotError:
                continue
            if record.status == "creating":
                yield record

    def _write_record(self, record: SnapshotRecord) -> None:
        # Atomic because the reader is another *process* by design: ``get()``
        # re-reads a ``creating`` record from this file precisely because the
        # replica that owns the copy is the one flipping it (F11 step 3).
        write_json_atomically(
            self._snapshot_dir(record.snapshot_id) / "snapshot.json",
            record.to_dict(),
        )

    def get(self, snapshot_id: str) -> SnapshotRecord:
        if not validate_sandbox_id(snapshot_id):
            raise UnknownSnapshotError(snapshot_id)
        with self._lock:
            record = self._snapshots.get(snapshot_id)
            # A ``creating`` record is the one state another *replica* can
            # change under us (it owns the copy and flips it when the bytes are
            # in), so that one is re-read from the shared record instead of
            # served from this process's cache -- otherwise a poll that lands
            # on the "other" replica waits forever on a copy that finished
            # (F11 step 3). Finished records never change, and stay cached.
            if record is not None and record.status != "creating":
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
