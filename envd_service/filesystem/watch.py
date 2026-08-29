"""Directory watchers: snapshot-diff events + streaming WatchDir."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from gateway_common.errors import invalid_argument, not_found
from gateway_common.ids import watcher_id
from gateway_common.paths import PathTraversalError, resolve_under_root
from envd_service.filesystem.ops import FilesystemOps, _entry


class WatcherRegistry:
    """Old-style watcher API (CreateWatcher / GetWatcherEvents / RemoveWatcher)."""

    def __init__(self, ops: FilesystemOps) -> None:
        self._ops = ops
        self._watchers: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, dict[tuple, dict[str, Any]]] = {}

    @staticmethod
    def _key(path: Path, st: os.stat_result) -> tuple:
        return (st.st_dev, st.st_ino)

    def _snapshot(self, root: Path, target: Path, recursive: bool) -> dict[tuple, dict[str, Any]]:
        snap: dict[tuple, dict[str, Any]] = {}
        targets = [target]
        if recursive:
            targets.extend(p for p in target.rglob("*") if p.is_dir())
        for base in targets:
            try:
                with os.scandir(base) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        path = Path(entry.path)
                        snap[self._key(path, st)] = {
                            "path": path,
                            "name": entry.name,
                            "mtime_ns": st.st_mtime_ns,
                            "size": st.st_size,
                            "mode": st.st_mode,
                            "entry": _entry(root, path, st),
                        }
            except FileNotFoundError:
                continue
        return snap

    def create(self, path: str, recursive: bool, include_entry: bool) -> str:
        target = self._resolve(path)
        if not target.is_dir():
            raise not_found(f"Path {path} not found")
        wid = watcher_id()
        self._watchers[wid] = {
            "path": path,
            "root": self._ops.root,
            "target": target,
            "recursive": recursive,
            "include_entry": include_entry,
        }
        self._snapshots[wid] = self._snapshot(self._ops.root, target, recursive)
        return wid

    def _resolve(self, path: str) -> Path:
        try:
            return resolve_under_root(self._ops.root, path)
        except PathTraversalError as e:
            raise invalid_argument(str(e)) from e

    def _watcher(self, wid: str) -> dict[str, Any]:
        watcher = self._watchers.get(wid)
        if watcher is None:
            raise not_found(f"Watcher {wid} not found")
        return watcher

    def events(self, wid: str) -> dict[str, Any]:
        watcher = self._watcher(wid)
        old = self._snapshots.get(wid, {})
        new = self._snapshot(
            watcher["root"], watcher["target"], watcher["recursive"]
        )
        self._snapshots[wid] = new

        events: list[dict[str, Any]] = []
        new_by_key = dict(new)
        # Removed / renamed-away entries.
        for key, info in old.items():
            if key not in new_by_key:
                events.append(self._event("EVENT_TYPE_REMOVE", info, watcher))
        # New / written / renamed-into / chmod entries.
        for key, info in new.items():
            prior = old.get(key)
            if prior is None:
                events.append(self._event("EVENT_TYPE_CREATE", info, watcher))
            else:
                if prior["mtime_ns"] != info["mtime_ns"] or prior["size"] != info["size"]:
                    events.append(self._event("EVENT_TYPE_WRITE", info, watcher))
                if prior["mode"] != info["mode"]:
                    events.append(self._event("EVENT_TYPE_CHMOD", info, watcher))
        events.sort(key=lambda e: e["name"])
        return {"events": events}

    @staticmethod
    def _event(event_type: str, info: dict[str, Any], watcher: dict[str, Any]) -> dict[str, Any]:
        event: dict[str, Any] = {"name": info["name"], "type": event_type}
        if watcher["include_entry"]:
            event["entry"] = info["entry"]
        else:
            event["entry"] = None
        return event

    def remove(self, wid: str) -> None:
        self._watcher(wid)
        self._watchers.pop(wid, None)
        self._snapshots.pop(wid, None)


class WatchDirStream:
    """Server-streaming WatchDir implementation (polling + event fan-out)."""

    def __init__(self, ops: FilesystemOps, poll_interval: float = 0.2) -> None:
        self._ops = ops
        self._poll_interval = poll_interval

    async def watch(self, path: str, recursive: bool, include_entry: bool) -> list[dict[str, Any]]:
        """Return a generator that yields WatchDirResponse JSON events."""

        try:
            target = resolve_under_root(self._ops.root, path)
        except PathTraversalError as e:
            raise invalid_argument(str(e)) from e
        if not target.is_dir():
            raise not_found(f"Path {path} not found")

        queue: asyncio.Queue = asyncio.Queue()
        watcher = WatcherRegistry(self._ops)
        wid = watcher.create(path, recursive, include_entry)
        await queue.put({"start": {}})

        async def _poll() -> None:
            try:
                while True:
                    await asyncio.sleep(self._poll_interval)
                    result = watcher.events(wid)
                    for event in result["events"]:
                        await queue.put({"filesystem": event})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        poll_task = asyncio.create_task(_poll())

        async def _gen():
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                poll_task.cancel()

        # Attach a keepalive every 15 seconds.
        async def _keepalive() -> None:
            try:
                while True:
                    await asyncio.sleep(15)
                    await queue.put({"keepalive": {}})
            except asyncio.CancelledError:
                pass

        keepalive_task = asyncio.create_task(_keepalive())
        original = _gen()

        async def _combined():
            try:
                async for item in original:
                    yield item
            finally:
                keepalive_task.cancel()

        return _combined()

