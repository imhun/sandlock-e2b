"""N62: a snapshot record is visible -- and invisible -- on *every* replica.

The defect: the records are files on the shared volume
(``<base>/_snapshots/<id>/snapshot.json``) but ``list()`` only read this
process's in-memory dict and ``get()`` served finished records from that dict
without looking at the file. Deleting a record on one replica therefore left
it listed on the other forever -- measured live as one replica listing 15
records and the other 12, with different members.

Two ``SnapshotRegistry`` instances over the same ``base_dir`` are the honest
shape of the two control-plane replicas: they share the volume and nothing
else, so every assertion here is about what the shared record says.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from control_plane.registry.snapshots import SnapshotRegistry, UnknownSnapshotError
from gateway_common.paths import write_json_atomically


def _write_record(
    base: Path,
    snapshot_id: str,
    *,
    created_at: str,
    names: list[str] | None = None,
    tenant_id: str | None = None,
) -> Path:
    """The on-disk shape ``SnapshotRegistry._write_record`` produces.

    A record is ``<base>/_snapshots/<id>/snapshot.json`` plus an ``fs/``
    payload directory; writing it directly is how a test can publish a record
    "from another replica" without that replica's process being here.
    """
    entry = base / "_snapshots" / snapshot_id
    (entry / "fs").mkdir(parents=True, exist_ok=True)
    path = entry / "snapshot.json"
    write_json_atomically(
        path,
        {
            "snapshot_id": snapshot_id,
            "names": list(names or []),
            "created_at": created_at,
            "tenant_id": tenant_id,
            "status": "completed",
        },
    )
    return path


def _ids(records) -> list[str]:
    return [record.snapshot_id for record in records]


def test_a_record_deleted_by_one_replica_disappears_from_the_other(tmp_path):
    base = tmp_path / "control"
    replica_a = SnapshotRegistry(base)
    replica_b = SnapshotRegistry(base)
    _write_record(base, "snap_0000000000000a01", created_at="2026-10-02T00:00:01Z")

    assert _ids(replica_a.list()) == ["snap_0000000000000a01"]
    assert _ids(replica_b.list()) == ["snap_0000000000000a01"]

    replica_b.delete("snap_0000000000000a01")

    assert replica_a.list() == []
    with pytest.raises(UnknownSnapshotError):
        replica_a.get("snap_0000000000000a01")


def test_a_record_written_by_another_replica_is_visible_to_a_warm_cache(tmp_path):
    base = tmp_path / "control"
    replica_a = SnapshotRegistry(base)
    _write_record(base, "snap_0000000000000a02", created_at="2026-10-02T00:00:01Z")
    # Warm replica A's cache from a listing before B publishes anything.
    assert _ids(replica_a.list()) == ["snap_0000000000000a02"]

    _write_record(base, "snap_0000000000000b02", created_at="2026-10-02T00:00:02Z")

    assert _ids(replica_a.list()) == [
        "snap_0000000000000b02",
        "snap_0000000000000a02",
    ]


def test_a_record_whose_payload_vanished_is_not_served_from_cache(tmp_path):
    base = tmp_path / "control"
    replica_a = SnapshotRegistry(base)
    _write_record(base, "snap_0000000000000a03", created_at="2026-10-02T00:00:01Z")
    # Cache it: a finished record, served from memory before the fix.
    assert replica_a.get("snap_0000000000000a03").snapshot_id == "snap_0000000000000a03"

    # The other replica's delete(): record file and payload gone.
    shutil.rmtree(base / "_snapshots" / "snap_0000000000000a03")

    with pytest.raises(UnknownSnapshotError):
        replica_a.get("snap_0000000000000a03")


def test_listing_keeps_the_existing_filters(tmp_path):
    base = tmp_path / "control"
    registry = SnapshotRegistry(base)
    _write_record(
        base,
        "snap_0000000000000c01",
        created_at="2026-10-02T00:00:01Z",
        names=["alpha"],
        tenant_id="t1",
    )
    _write_record(
        base,
        "snap_0000000000000c02",
        created_at="2026-10-02T00:00:02Z",
        names=["alpha", "beta"],
        tenant_id="t1",
    )
    _write_record(
        base,
        "snap_0000000000000c03",
        created_at="2026-10-02T00:00:03Z",
        names=["beta"],
        tenant_id="t2",
    )

    assert _ids(registry.list()) == [
        "snap_0000000000000c03",
        "snap_0000000000000c02",
        "snap_0000000000000c01",
    ]
    assert _ids(registry.list(tenant_id="t1")) == [
        "snap_0000000000000c02",
        "snap_0000000000000c01",
    ]
    assert _ids(registry.list(name="alpha")) == [
        "snap_0000000000000c02",
        "snap_0000000000000c01",
    ]
    assert _ids(registry.list(limit=2)) == [
        "snap_0000000000000c03",
        "snap_0000000000000c02",
    ]
    assert _ids(registry.list(limit=1, offset=1)) == ["snap_0000000000000c02"]
