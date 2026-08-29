"""Multi-node scheduling: sandbox runs on the registered remote worker and
SDK traffic is routed through the envd gateway."""

from __future__ import annotations

import pytest

from e2b import Sandbox


def _opts(servers):
    return {
        "api_url": servers["api_url"],
        "sandbox_url": servers["sandbox_url"],
        "api_key": "local-key",
    }


def test_create_routes_to_remote_worker(multinode_servers):
    sandbox = Sandbox.create(**_opts(multinode_servers))
    try:
        assert sandbox.is_running() is True
        result = sandbox.commands.run("echo multinode-ok")
        assert result.stdout == "multinode-ok\n"
        assert result.exit_code == 0

        info = sandbox.get_info()
        assert info.sandbox_id == sandbox.sandbox_id
        assert info.state == "running"
    finally:
        assert sandbox.kill() is True


def test_stdin_through_gateway(multinode_servers):
    sandbox = Sandbox.create(**_opts(multinode_servers))
    try:
        proc = sandbox.commands.run("cat", stdin=True, background=True)
        proc.send_stdin("gateway-stdin\n")
        proc.close_stdin()
        result = proc.wait()
        assert result.stdout == "gateway-stdin\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


def test_files_through_gateway(multinode_servers):
    sandbox = Sandbox.create(**_opts(multinode_servers))
    try:
        sandbox.files.write("workspace/node.txt", "node-data")
        assert sandbox.files.read("workspace/node.txt") == "node-data"
        assert sandbox.files.exists("workspace/node.txt") is True
    finally:
        sandbox.kill()


def test_node_assignment_and_route(multinode_servers, control_client=None):
    sandbox = Sandbox.create(**_opts(multinode_servers))
    try:
        # The sandbox must be recorded on the remote worker node.
        assert any(n.address != "local://" for n in multinode_servers["nodes"].list())
    finally:
        sandbox.kill()


@pytest.mark.asyncio
async def test_async_multinode(multinode_servers):
    from e2b import AsyncSandbox

    sandbox = await AsyncSandbox.create(**_opts(multinode_servers))
    try:
        result = await sandbox.commands.run("echo async-node")
        assert result.stdout == "async-node\n"
        assert await sandbox.is_running() is True
    finally:
        assert await sandbox.kill() is True


def test_remote_snapshot_and_create_from_it(multinode_servers):
    """Snapshots of remote-node sandboxes are captured on the node and can
    cold-boot new sandboxes on the same node."""
    sandbox = Sandbox.create(**_opts(multinode_servers))
    try:
        sandbox.files.write("workspace/remote-snap.txt", "remote-snap-data")
        snapshot = sandbox.create_snapshot(name="remote-snap")
        assert snapshot.snapshot_id.startswith("snap_")

        clone = Sandbox.create(snapshot.snapshot_id, **_opts(multinode_servers))
        try:
            assert clone.files.read("workspace/remote-snap.txt") == "remote-snap-data"
            assert clone.commands.run("echo from-remote-snap").stdout == (
                "from-remote-snap\n"
            )
        finally:
            clone.kill()

        forks = sandbox.fork(count=1)
        assert len(forks) == 1
        assert forks[0].files.read("workspace/remote-snap.txt") == "remote-snap-data"
        forks[0].kill()
    finally:
        sandbox.kill()
