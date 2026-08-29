"""Fork and snapshot via the official e2b SDK (filesystem-level, cold boot)."""

from __future__ import annotations

import pytest

from e2b import Sandbox


def test_snapshot_roundtrip_and_create_from_snapshot(sandbox):
    sandbox.files.write("workspace/snap.txt", "snap-data")
    snapshot = sandbox.create_snapshot(name="snap1")
    assert snapshot.snapshot_id.startswith("snap_")
    assert snapshot.names == ["snap1"]

    paginator = Sandbox.list_snapshots()
    ids = []
    while paginator.has_next:
        ids.extend(s.snapshot_id for s in paginator.next_items())
    assert snapshot.snapshot_id in ids

    clone = Sandbox.create(snapshot.snapshot_id)
    try:
        assert clone.files.read("workspace/snap.txt") == "snap-data"
        result = clone.commands.run("echo from-snapshot")
        assert result.stdout == "from-snapshot\n"
    finally:
        clone.kill()

    assert sandbox.delete_snapshot(snapshot.snapshot_id) is True
    assert sandbox.delete_snapshot(snapshot.snapshot_id) is False


def test_fork_independent_sandboxes(sandbox):
    sandbox.files.write("workspace/fork.txt", "fork-data")
    forks = sandbox.fork(count=2)
    assert len(forks) == 2
    for fork in forks:
        assert isinstance(fork, Sandbox)
        assert fork.sandbox_id != sandbox.sandbox_id
        assert fork.files.read("workspace/fork.txt") == "fork-data"
        assert fork.commands.run("echo fork-ok").stdout == "fork-ok\n"
        fork.kill()
    # The source sandbox keeps running.
    assert sandbox.is_running() is True
    assert sandbox.files.read("workspace/fork.txt") == "fork-data"


def test_fork_missing_sandbox_raises(live_servers):
    from e2b.exceptions import SandboxNotFoundException

    with pytest.raises(SandboxNotFoundException):
        Sandbox.fork("sbx_missing", count=1)


@pytest.mark.asyncio
async def test_async_snapshot_and_fork(async_sandbox):
    from e2b import AsyncSandbox

    await async_sandbox.files.write("workspace/async-snap.txt", "async-data")
    snapshot = await async_sandbox.create_snapshot(name="async-snap")
    assert snapshot.snapshot_id.startswith("snap_")

    clone = await AsyncSandbox.create(snapshot.snapshot_id)
    try:
        assert await clone.files.read("workspace/async-snap.txt") == "async-data"
    finally:
        await clone.kill()

    forks = await async_sandbox.fork(count=1)
    assert len(forks) == 1
    assert forks[0].sandbox_id != async_sandbox.sandbox_id
    await forks[0].kill()
    assert await async_sandbox.delete_snapshot(snapshot.snapshot_id) is True
