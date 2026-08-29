"""Filesystem-level migration between worker nodes (multi-node)."""

from __future__ import annotations

import httpx
import pytest

from e2b import Sandbox


def _opts(harness):
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


async def _route(harness, sandbox_id) -> dict:
    async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
        resp = await client.get(
            f"/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert resp.status_code == 200
    return resp.json()


async def _migrate(harness, sandbox_id, body=None) -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=harness["api_url"], timeout=60
    ) as client:
        resp = await client.post(
            f"/sandboxes/{sandbox_id}/migrate",
            headers={"X-API-Key": "local-key"},
            json=body or {},
        )
    return resp


async def test_migrate_conflict_while_lock_held(multinode_two_workers):
    """A held migration lock makes a second migrate answer 409, and the lock
    release unblocks it (in-process SETNX equivalent)."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(**_opts(harness))
    sandbox_id = sandbox.sandbox_id
    try:
        registry = harness["control_app"].state.registry
        token = registry.try_acquire_migration(sandbox_id)
        assert token is not None
        try:
            conflicted = await _migrate(harness, sandbox_id)
            assert conflicted.status_code == 409
        finally:
            registry.release_migration(sandbox_id, token)
        migrated = await _migrate(harness, sandbox_id)
        assert migrated.status_code == 200
        assert migrated.json()["sandboxID"] == sandbox_id
    finally:
        sandbox.kill()


async def test_migrate_failure_restores_source_runtime(multinode_two_workers):
    """When the target cannot be provisioned the migration aborts and the
    source runtime is re-provisioned: commands keep working on the source
    node (no dual-active, no lost sandbox)."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(**_opts(harness))
    sandbox_id = sandbox.sandbox_id
    try:
        before = await _route(harness, sandbox_id)
        # A node that accepts no agent API: pointing at the control plane
        # itself makes the target import fail fast and deterministically
        # (404), instead of depending on connect timeouts.
        harness["nodes"].register(
            node_id="node_dead",
            address=harness["api_url"],
            total_memory_mb=4096,
            total_cpu_percent=400,
            total_disk_mb=8192,
            total_processes=256,
        )
        try:
            failed = await _migrate(
                harness, sandbox_id, body={"nodeID": "node_dead"}
            )
            assert failed.status_code >= 500

            # The sandbox still routes to the source and commands still
            # execute after the failed migration re-provisioned its runtime.
            after = await _route(harness, sandbox_id)
            assert after["address"] == before["address"]
            assert (
                sandbox.commands.run("echo still-alive > alive.txt").exit_code == 0
            )
            assert sandbox.commands.run("cat alive.txt").stdout == "still-alive\n"
        finally:
            # Do not leak the fake node into the session-scoped harness.
            harness["nodes"].remove("node_dead")
    finally:
        sandbox.kill()


async def test_migrate_sandbox_files_between_workers(multinode_two_workers):
    harness = multinode_two_workers
    sandbox = Sandbox.create(**_opts(harness))
    sandbox_id = sandbox.sandbox_id
    try:
        before = await _route(harness, sandbox_id)
        source_address = before["address"]
        assert source_address in harness["worker_urls"]
        target_address = next(
            url for url in harness["worker_urls"] if url != source_address
        )

        # Seed state through the gateway (populates the route cache).
        sandbox.files.write("workspace/marker.txt", "before-migration")
        assert sandbox.commands.run("echo from-a > cmd-marker.txt").exit_code == 0

        migrated = await _migrate(harness, sandbox_id)
        assert migrated.status_code == 200
        assert migrated.json()["sandboxID"] == sandbox_id

        after = await _route(harness, sandbox_id)
        assert after["address"] == target_address
        assert after["nodeID"] != before["nodeID"]

        # The gateway route was invalidated; traffic now reaches the target.
        assert sandbox.commands.run("cat cmd-marker.txt").stdout == "from-a\n"
        assert sandbox.files.read("workspace/marker.txt") == "before-migration"
        assert sandbox.commands.run("echo on-b > post-marker.txt").exit_code == 0
        assert sandbox.commands.run("cat post-marker.txt").stdout == "on-b\n"

        # The source node no longer holds the workspace.
        async with httpx.AsyncClient(
            base_url=source_address
        ) as client:
            exported = await client.get(
                f"/agent/sandboxes/{sandbox_id}/export",
                headers={"X-Internal-Key": "internal-key"},
            )
            assert exported.status_code == 404
    finally:
        sandbox.kill()


async def test_migrate_with_shared_volume(multinode_two_workers):
    harness = multinode_two_workers
    async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
        created = await client.post(
            "/volumes",
            headers={"X-API-Key": "local-key"},
            json={"name": "migration-vol"},
        )
        assert created.status_code == 201
        volume_id = created.json()["volumeID"]
    try:
        sandbox = Sandbox.create(
            volume_mounts={"/data": volume_id}, **_opts(harness)
        )
        sandbox_id = sandbox.sandbox_id
        try:
            # The mounted path is a symlink outside the workspace root, so the
            # shell (inside the sandbox) writes it, not the files HTTP API.
            assert sandbox.commands.run(
                "echo volume-data > data/shared.txt"
            ).exit_code == 0
            migrated = await _migrate(harness, sandbox_id)
            assert migrated.status_code == 200
            assert (
                sandbox.commands.run("cat data/shared.txt").stdout
                == "volume-data\n"
            )
        finally:
            sandbox.kill()
    finally:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            await client.delete(
                f"/volumes/{volume_id}", headers={"X-API-Key": "local-key"}
            )


async def test_migrate_explicit_target_and_quota_move(multinode_two_workers):
    harness = multinode_two_workers
    sandbox = Sandbox.create(**_opts(harness))
    sandbox_id = sandbox.sandbox_id
    try:
        before = await _route(harness, sandbox_id)
        target_node = next(
            n for n in harness["nodes"].list() if n.node_id != before["nodeID"]
        )

        migrated = await _migrate(
            harness, sandbox_id, body={"nodeID": target_node.node_id}
        )
        assert migrated.status_code == 200
        assert migrated.json()["nodeID"] == target_node.node_id

        # Quota moved: source released, target reserved.
        source = next(n for n in harness["nodes"].list() if n.node_id == before["nodeID"])
        target = next(
            n for n in harness["nodes"].list() if n.node_id == target_node.node_id
        )
        assert source.reserved_memory_mb == 0
        assert target.reserved_memory_mb == 512

        # Migrating back to the current node is rejected.
        same = await _migrate(harness, sandbox_id, body={"nodeID": target_node.node_id})
        assert same.status_code == 400
    finally:
        sandbox.kill()


async def test_migrate_unknown_sandbox_404(multinode_two_workers):
    harness = multinode_two_workers
    migrated = await _migrate(harness, "sbx_does_not_exist")
    assert migrated.status_code == 404


async def test_migrate_shared_workspace_skips_transfer(multinode_shared_workspace):
    """With E2B_SHARED_WORKSPACE_ROOT migration only switches the route:
    no archive transfer, and the (shared) source directory is kept."""
    harness = multinode_shared_workspace
    sandbox = Sandbox.create(**_opts(harness))
    sandbox_id = sandbox.sandbox_id
    try:
        before = await _route(harness, sandbox_id)
        source_address = before["address"]
        assert source_address in harness["worker_urls"]
        target_address = next(
            url for url in harness["worker_urls"] if url != source_address
        )

        sandbox.files.write("workspace/marker.txt", "shared-workspace-data")
        assert sandbox.commands.run("echo on-source > cmd-marker.txt").exit_code == 0

        migrated = await _migrate(harness, sandbox_id)
        assert migrated.status_code == 200
        assert migrated.json()["nodeID"] != before["nodeID"]

        after = await _route(harness, sandbox_id)
        assert after["address"] == target_address

        # Files survive without any archive transfer (same shared storage).
        assert sandbox.commands.run("cat cmd-marker.txt").stdout == "on-source\n"
        assert sandbox.files.read("workspace/marker.txt") == "shared-workspace-data"

        # The source directory is kept, and it is the very same directory the
        # target node now serves (export succeeds from both nodes).
        for address in (source_address, target_address):
            async with httpx.AsyncClient(base_url=address) as client:
                exported = await client.get(
                    f"/agent/sandboxes/{sandbox_id}/export",
                    headers={"X-Internal-Key": "internal-key"},
                )
                assert exported.status_code == 200
    finally:
        sandbox.kill()
