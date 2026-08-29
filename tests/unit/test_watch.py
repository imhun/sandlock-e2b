"""WatchDir streaming generator and watcher events."""

from __future__ import annotations

import asyncio

import pytest

from envd_service.filesystem.ops import FilesystemOps
from envd_service.filesystem.watch import WatcherRegistry, WatchDirStream


def test_watcher_create_events_remove(workspace):
    root = workspace / "watch-root"
    root.mkdir()
    ops = FilesystemOps(root)
    watchers = WatcherRegistry(ops)
    wid = watchers.create("", recursive=False, include_entry=False)
    (root / "evt.txt").write_text("x")
    result = watchers.events(wid)
    types = [(e["type"], e["name"]) for e in result["events"]]
    assert ("EVENT_TYPE_CREATE", "evt.txt") in types
    watchers.remove(wid)
    with pytest.raises(Exception):
        watchers.events(wid)


@pytest.mark.asyncio
async def test_watch_dir_stream_emits_start_then_events(workspace):
    root = workspace / "watch-stream-root"
    root.mkdir()
    ops = FilesystemOps(root)
    stream = WatchDirStream(ops, poll_interval=0.05)
    gen = await stream.watch("", recursive=False, include_entry=False)

    async def consume():
        events = []
        async for item in gen:
            events.append(item)
            if "filesystem" in item:
                return events

    (root / "created.txt").write_text("x")
    events = await asyncio.wait_for(consume(), timeout=5)
    assert events[0] == {"start": {}}
    assert events[-1]["filesystem"]["type"] == "EVENT_TYPE_CREATE"
    assert events[-1]["filesystem"]["name"] == "created.txt"

