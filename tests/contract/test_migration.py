"""Filesystem-level migration between worker nodes (multi-node)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx
import pytest

from e2b import Sandbox
from tests._memory_budget import per_sandbox_memory_mb


class _TreeRemovingAgentClient:
    """The CP→agent removal channel, as this harness's stand-in for the agent.

    Migration of a **non-shared** tree has to take the source's workspace off
    the disk *after* the record already names the target, and the only channel
    that may do so is the CP→agent hop: the worker's own DELETE asks the control
    plane for a ``remove-workspace`` file op, which the control plane refuses
    because the record has moved (403) -- that leak is the whole reason
    ``_release_source_after_migration`` exists
    (``docs/create-local-first-design.md`` §8.3.1).

    This harness (``multinode_two_workers``) runs the control plane, two envd
    worker apps and the gateway *in one process*: there are no per-node agent
    services, so a real ``C3AgentClient`` cannot address one and every release
    is refused by name -- the tree then stays, which is what this case measured
    (2026-10-11) before the stand-in existed. Installing one keeps the
    contract's own subject under test: the CP derives the path, instructs
    **that node**, and the tree really goes away.

    Two things are modelled, both from the deployed shapes rather than invented:

    * the agent acts on the tree under **its own** workspace base -- that is
      ``e2b-maint``'s root whitelist (``maint_env`` hands the binary
      ``E2B_WORKSPACE_BASE`` and it refuses a path outside it). Every deployed
      shape pins one absolute tree root for the whole fleet
      (``/var/lib/e2b/workspaces`` in ``deploy/k8s/worker.yaml``,
      ``/var/lib/e2b-sandboxes`` in the compose stack), so the CP's derivation
      and the node's own base are the same directory there. This harness gives
      each *in-process* node a directory of its own
      (``…/multinode-two/worker-N``), which is the one place they differ; the
      stand-in resolves against the node's base, and records the sent path so
      the difference stays visible instead of being assumed away;
    * a removal is only ever of a sandbox tree, so anything whose basename is
      not a sandbox id is refused here rather than deleted.

    The agent *service* itself (face B's HTTP route, the kernel-checked worker
    identity, ``e2b-maint``'s argv/refusals) is exercised against the real agent
    image by ``tests/contract/test_c3_slot_identity_grant.py`` and by
    ``tests/unit/test_c3_agent_fileops.py`` -- and this harness getting a real
    per-node agent is recorded in ``docs/open-issues.md`` (N96).
    """

    def __init__(self, node_roots: dict[str, "os.PathLike[str]"]) -> None:
        self._node_roots = {node: Path(root) for node, root in node_roots.items()}
        self.removals: list[tuple[str, str]] = []
        #: Sent paths that are *not* under the addressed node's own base (see
        #: the class docstring): this harness's per-node directories, never a
        #: deployed shape.
        self.cross_base: list[str] = []

    async def rm(
        self,
        *,
        node_id: str,
        sandbox_id: str,
        path: str,
        worker_uid: int | None = None,
        worker_gid: int | None = None,
        worker_container_id: str | None = None,
        target: object | None = None,
    ) -> dict:
        node_root = self._node_roots[node_id]
        sent = Path(path)
        if not sent.is_relative_to(node_root):
            self.cross_base.append(path)
        if not sent.name.startswith("sbx_"):
            raise AssertionError(f"the CP asked to remove a non-sandbox path: {path}")
        tree = node_root / sent.name
        self.removals.append((node_id, str(tree)))
        if tree.is_symlink() or tree.is_file():
            tree.unlink()
        elif tree.exists():
            shutil.rmtree(tree)
        return {"removed": str(tree)}


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
    # The CP→agent channel this harness has no service for: see
    # ``_TreeRemovingAgentClient``. Installed for the whole case so the
    # sandbox's own teardown releases the target's tree the same way.
    state = harness["control_app"].state
    stub_agent = _TreeRemovingAgentClient(
        {
            f"worker-{index + 1}": Path(app.state.settings.workspace_base)
            for index, app in enumerate(harness["worker_apps"])
        }
    )
    real_agent = state.c3_agent_client
    state.c3_agent_client = stub_agent
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

        # The source node no longer holds the workspace -- and it is the CP's
        # own instruction (the stand-in above, on the CP-derived path) that took
        # it away, not the worker's DELETE, which the CP would refuse now that
        # the record names the target.
        assert [node for node, _path in stub_agent.removals] == [before["nodeID"]]
        async with httpx.AsyncClient(
            base_url=source_address
        ) as client:
            exported = await client.get(
                f"/agent/sandboxes/{sandbox_id}/export",
                headers={"X-Internal-Key": "internal-key"},
            )
            assert exported.status_code == 404
    finally:
        state.c3_agent_client = real_agent
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
        # The moved sandbox reserves its own ceiling on the target node.
        assert target.reserved_memory_mb == per_sandbox_memory_mb()

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
