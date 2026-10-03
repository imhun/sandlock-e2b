"""N64 / minor: the ``GET /snapshots`` endpoint's cost and failure handling.

Two defects, both in ``control_plane/api/snapshots.py``:

* the listing handler asked the registry for the page (``limit``/``offset``)
  and then asked it again -- with no limit -- just to count, so a single
  request paid ~2N stats and a ``json.loads`` per uncached record, all of it
  synchronously on the event loop;
* ``_run_reserved_capture`` answers a failed copy by calling ``mark_failed``,
  and when a peer had already ``rmtree``-ed the record that call raises
  ``UnknownSnapshotError`` out of an asyncio task whose only callback is
  ``discard`` -- the task owns the exception and nobody ever sees it.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from control_plane.api import snapshots as snapshots_api
from control_plane.app import create_app
from control_plane.config import Settings
from control_plane.registry.snapshots import (
    SnapshotRecord,
    SnapshotRegistry,
    UnknownSnapshotError,
)

API_KEY = "local-key"


class _CountingSnapshots:
    """A registry stand-in that records *how* the endpoint called ``list()``.

    The stand-in is the defect's witness: the red test sees two entries here
    (the page and the un-limited count) and the thread of the caller, which is
    the event-loop thread before the fix.
    """

    def __init__(self, records: list[SnapshotRecord]) -> None:
        self._records = list(records)
        self.calls: list[tuple[dict, int]] = []

    def list(self, **kwargs):
        self.calls.append((dict(kwargs), threading.get_ident()))
        records = list(self._records)
        offset = kwargs.get("offset") or 0
        limit = kwargs.get("limit")
        if limit is not None:
            records = records[offset : offset + limit]
        return records


def _record(snapshot_id: str, position: int) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=snapshot_id,
        names=[],
        created_at=datetime(2026, 10, 2, 0, 0, position, tzinfo=timezone.utc),
    )


def _app(tmp_path: Path, snapshots_registry) -> object:
    trees = tmp_path / "trees"
    trees.mkdir(exist_ok=True)
    return create_app(
        settings=Settings(
            api_keys=(API_KEY,),
            create_queue_timeout_s=0,
            workspace_base=trees,
        ),
        workspace_base=trees,
        snapshots_registry=snapshots_registry,
    )


async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as client:
        return await client.get(path, headers={"X-API-Key": API_KEY})


async def test_list_endpoint_calls_the_registry_once_off_the_event_loop(tmp_path):
    """One request, one ``list()``, and it runs in a worker thread (N64).

    The event-loop thread is captured here, on the coroutine that drives the
    request; ``asyncio.to_thread`` hands ``list()`` to a thread of its own, so
    the ids must differ. A handler that awaited ``list()`` directly would run
    it on the loop and fail this assertion.
    """
    records = [
        _record("snap_0000000000000301", 1),
        _record("snap_0000000000000302", 2),
        _record("snap_0000000000000303", 3),
    ]
    registry = _CountingSnapshots(records)
    app = _app(tmp_path, registry)

    event_loop_thread = threading.get_ident()
    response = await _get(app, "/snapshots?limit=2")

    assert response.status_code == 200
    assert response.json() == [
        {"snapshotID": "snap_0000000000000301", "names": []},
        {"snapshotID": "snap_0000000000000302", "names": []},
    ]
    assert response.headers["X-Next-Token"] == "2"
    # The call runs off the loop: a handler that awaited ``list()`` inline
    # would run it on the very thread this coroutine is on.
    kwargs, call_thread = registry.calls[0]
    assert call_thread != event_loop_thread
    assert kwargs == {"sandbox_id_filter": None, "name": None, "tenant_id": None}
    # Exactly one call per request, and it is the full listing (no limit).
    assert len(registry.calls) == 1


async def test_list_endpoint_pages_and_passes_the_sandbox_filter(tmp_path):
    """The outward JSON is unchanged, and ``sandboxID`` reaches the registry."""
    records = [
        _record("snap_0000000000000401", 1),
        _record("snap_0000000000000402", 2),
        _record("snap_0000000000000403", 3),
    ]
    registry = _CountingSnapshots(records)
    app = _app(tmp_path, registry)

    second_page = await _get(app, "/snapshots?limit=2&nextToken=2")
    assert second_page.status_code == 200
    assert second_page.json() == [{"snapshotID": "snap_0000000000000403", "names": []}]
    # The last page is not truncated, so there is no next-token header.
    assert "X-Next-Token" not in second_page.headers
    assert len(registry.calls) == 1

    registry.calls.clear()
    filtered = await _get(app, "/snapshots?sandboxID=sbx_0000000000000042")
    assert filtered.status_code == 200
    assert len(registry.calls) == 1
    assert registry.calls[0][0]["sandbox_id_filter"] == "sbx_0000000000000042"


async def test_run_reserved_capture_tolerates_a_peer_deleting_the_record(
    tmp_path, caplog, monkeypatch
):
    """``mark_failed`` on a vanished record is a warning, not a lost task.

    The peer ``rmtree``-ing the record mid-copy is the shape under test: the
    capture fails, ``mark_failed`` looks the record up, and the lookup raises
    because the file -- which *was* the truth -- is gone. Nothing is left to
    write the failure to, so the only useful thing the handler can do is name
    the id and the reason in the log.
    """
    base = tmp_path / "control"
    registry = SnapshotRegistry(base)
    snapshot_id = "snap_0000000000000f09"
    sandbox_id = "sbx_0000000000000001"
    registry.reserve_from_sandbox(
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        source_sandbox_id=sandbox_id,
        snapshot_id=snapshot_id,
    )
    app = _app(tmp_path, registry)

    def _capture_and_lose_the_record(_state, sid, _sandbox_id, _name):
        # A peer's delete() lands between the failed copy and mark_failed.
        registry.delete(sid)
        raise RuntimeError("the copy failed after the record was gone")

    monkeypatch.setattr(
        snapshots_api, "_capture_reserved", _capture_and_lose_the_record
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="control_plane.api.snapshots"):
        await snapshots_api._run_reserved_capture(
            app, snapshot_id, sandbox_id, None, "lease-token"
        )

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "control_plane.api.snapshots"
        and record.levelno == logging.WARNING
    ]
    assert warnings == [
        f"async snapshot {snapshot_id} failed: "
        "the copy failed after the record was gone",
        f"snapshot {snapshot_id}: the record was deleted by another replica "
        "before the failure could be recorded",
    ]
    with pytest.raises(UnknownSnapshotError):
        registry.get(snapshot_id)
