"""E6.1 multi-node partition + worker recovery reconciliation.

Scenario under test (security-hardening §8.6): a network partition makes a
worker node unhealthy while its sandboxes keep running. The control plane
must not tear the live sandboxes down (TTL would delete the workspace
under the running processes); it marks the records orphaned instead, and
when the worker recovers both sides reconcile:

* worker local runtimes the control plane no longer knows are torn down;
* control-plane records the worker no longer runs are removed;
* records the worker still runs are restored to ``running``.
"""

from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from envd_service.agent import NodeAgent
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common.timeutil import utcnow


def _c3_resolver() -> StaticAddressResolver:
    """C3 Task 2 (D4/D5): the control plane's expected node endpoints.

    The internal API never takes a node's address from the request and refuses a
    node-scoped claim it cannot resolve, so this lane -- which speaks for
    ``node_a``/``node_b`` over an in-process ASGI client (peer 127.0.0.1) --
    hands the control plane their expected endpoints instead.
    """
    return StaticAddressResolver(
        {
            "node_a": NodeEndpoint("http://127.0.0.1:11111", "127.0.0.1"),
            "node_b": NodeEndpoint("http://127.0.0.1:22222", "127.0.0.1"),
        }
    )


def _register_node(nodes: NodeRegistry, node_id: str, address: str):
    return nodes.register(
        node_id=node_id,
        address=address,
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
        images=["python:3.11-slim"],
    )


def _sandbox_on(registry: SandboxRegistry, node_id: str, sandbox_id: str):
    record = registry.create(
        template_id="base",
        sandbox_id=sandbox_id,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    record.node_id = node_id
    registry.save(record)
    return record


def _make_control(workspace) -> tuple[NodeRegistry, SandboxRegistry, object]:
    nodes = NodeRegistry(heartbeat_timeout=1.0)
    registry = SandboxRegistry(ControlSettings(api_keys=("local-key",)))
    app = create_control_app(
        settings=ControlSettings(api_keys=("local-key",)),
        registry=registry,
        nodes_registry=nodes,
        workspace_base=workspace,
        node_address_resolver=_c3_resolver(),
    )
    return nodes, registry, app


def test_partition_orphans_sandboxes_and_ttl_skips(workspace):
    """Node A loses heartbeats: its sandboxes are orphaned (not deleted),
    TTL leaves them alone, and healthy node B is unaffected."""
    nodes, registry, _app = _make_control(workspace)
    node_a = _register_node(nodes, "node_a", "http://127.0.0.1:11111")
    _register_node(nodes, "node_b", "http://127.0.0.1:22222")
    rec_a1 = _sandbox_on(registry, "node_a", "sbx_part_a1")
    rec_a2 = _sandbox_on(registry, "node_a", "sbx_part_a2")
    rec_b = _sandbox_on(registry, "node_b", "sbx_part_b")

    node_a.heartbeat_at = time.time() - 10
    assert nodes.reap_unhealthy(registry) == ["node_a"]
    assert registry.get("sbx_part_a1").state == "orphaned"
    assert registry.get("sbx_part_a2").state == "orphaned"
    assert registry.get("sbx_part_b").state == "running"

    # TTL must never reap orphaned records: the worker may still be running
    # them (deleting the workspace would orphan the inodes, §8.6).
    for record in (rec_a1, rec_a2, rec_b):
        record.end_at = utcnow() - timedelta(seconds=10)
    assert registry.remove_expired() == [rec_b]
    assert registry.get("sbx_part_a1").state == "orphaned"


def test_partition_leaves_a_paused_sandbox_paused(workspace):
    """A paused sandbox keeps its state through the node-health sweep.

    Orphaning it would protect nothing -- a paused record already gave its
    node/global/tenant reservations back and TTL expiry already skips it --
    while destroying the one fact the resume path needs: ``paused`` is what
    makes ``Sandbox.connect``'s auto-resume (the SDK's only public resume
    surface) push the thaw to the worker that holds the frozen child. Left
    ``paused`` (or flipped to ``running`` by ``recover_node``) the worker's
    SIGSTOPped process tree can never be thawed again.
    """
    nodes, registry, _app = _make_control(workspace)
    _register_node(nodes, "node_a", "http://127.0.0.1:11111")
    paused = _sandbox_on(registry, "node_a", "sbx_part_paused")
    running = _sandbox_on(registry, "node_a", "sbx_part_running")
    registry.pause(paused)

    nodes.get("node_a").heartbeat_at = time.time() - 10
    assert nodes.reap_unhealthy(registry) == ["node_a"]
    assert registry.get("sbx_part_paused").state == "paused"
    assert registry.get("sbx_part_running").state == "orphaned"

    # TTL already skips both states; recovery reconciliation must not turn the
    # paused sandbox into a ``running`` one whose worker was never thawed.
    for record in (paused, running):
        record.end_at = utcnow() - timedelta(seconds=10)
    assert registry.remove_expired() == []
    recovered = registry.recover_node(
        "node_a",
        {"sbx_part_paused", "sbx_part_running"},
        {"sbx_part_paused", "sbx_part_running"},
    )
    assert recovered["recovered"] == ["sbx_part_running"]
    assert registry.get("sbx_part_paused").state == "paused"
    assert registry.get("sbx_part_running").state == "running"


@pytest.mark.asyncio
async def test_recovery_reconcile_endpoints(workspace):
    """The worker's recovery report restores records it still runs and
    removes records it no longer has; other nodes are untouched."""
    nodes, registry, app = _make_control(workspace)
    _register_node(nodes, "node_a", "http://127.0.0.1:11111")
    _register_node(nodes, "node_b", "http://127.0.0.1:22222")
    rec_keep = _sandbox_on(registry, "node_a", "sbx_rec_keep")
    rec_gone = _sandbox_on(registry, "node_a", "sbx_rec_gone")
    rec_b = _sandbox_on(registry, "node_b", "sbx_rec_b")
    nodes.get("node_a").heartbeat_at = time.time() - 10
    nodes.reap_unhealthy(registry)

    headers = {"X-Internal-Key": ControlSettings(api_keys=("local-key",)).internal_api_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        listed = await client.get(
            "/internal/nodes/node_a/sandboxes", headers=headers
        )
        assert listed.status_code == 200
        snapshot = listed.json()
        assert snapshot["sandboxIDs"] == ["sbx_rec_gone", "sbx_rec_keep"]

        resp = await client.post(
            "/internal/nodes/node_a/reconcile",
            headers=headers,
            json={
                "sandboxIDs": ["sbx_rec_keep"],
                "snapshotIDs": snapshot["sandboxIDs"],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "recovered": ["sbx_rec_keep"],
            "removed": ["sbx_rec_gone"],
            "kept": [],
        }

        assert registry.get("sbx_rec_keep").state == "running"
        with pytest.raises(KeyError):
            registry.get("sbx_rec_gone")
        assert registry.get("sbx_rec_b").state == "running"
        assert registry.get("sbx_rec_b").node_id == "node_b"

        # Malformed payloads are rejected with a precise 400.
        bad = await client.post(
            "/internal/nodes/node_a/reconcile",
            headers=headers,
            json={"sandboxIDs": ["../escape"]},
        )
        assert bad.status_code == 400
        bad_missing_key = await client.post(
            "/internal/nodes/node_a/reconcile",
            headers=headers,
            json={},
        )
        assert bad_missing_key.status_code == 400
        bad_missing_snapshot = await client.post(
            "/internal/nodes/node_a/reconcile",
            headers=headers,
            json={"sandboxIDs": ["sbx_rec_keep"]},
        )
        assert bad_missing_snapshot.status_code == 400
        unauthorized = await client.get(
            "/internal/nodes/node_a/sandboxes",
            headers={"X-Internal-Key": "wrong"},
        )
        assert unauthorized.status_code == 401


@pytest.mark.asyncio
async def test_reconcile_post_snapshot_create_not_deleted(workspace):
    """E6.1 race (control-plane side): a sandbox record created after the
    worker's snapshot must survive reconcile even when the worker's report
    (computed before the create landed) omits it."""
    nodes, registry, app = _make_control(workspace)
    _register_node(nodes, "node_a", "http://127.0.0.1:11111")
    rec_keep = _sandbox_on(registry, "node_a", "sbx_race_keep")
    nodes.get("node_a").heartbeat_at = time.time() - 10
    nodes.reap_unhealthy(registry)

    headers = {"X-Internal-Key": ControlSettings(api_keys=("local-key",)).internal_api_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        listed = await client.get(
            "/internal/nodes/node_a/sandboxes", headers=headers
        )
        snapshot = listed.json()
        assert snapshot["sandboxIDs"] == ["sbx_race_keep"]

        # Concurrent create: the record lands on node_a after the snapshot.
        rec_new = _sandbox_on(registry, "node_a", "sbx_race_new")
        assert rec_new.state == "running"

        # The worker's diff was computed before the create, so its report
        # only lists the snapshot sandbox.
        resp = await client.post(
            "/internal/nodes/node_a/reconcile",
            headers=headers,
            json={
                "sandboxIDs": ["sbx_race_keep"],
                "snapshotIDs": snapshot["sandboxIDs"],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "recovered": ["sbx_race_keep"],
            "removed": [],
            "kept": ["sbx_race_new"],
        }
        assert registry.get("sbx_race_keep").state == "running"
        # The live concurrent create was NOT deleted by recovery.
        survivor = registry.get("sbx_race_new")
        assert survivor.state == "running"
        assert survivor.node_id == "node_a"


class _RacingCreateClient:
    """Wraps the ASGI client to simulate a create landing on the worker
    between the snapshot GET and the reconcile diff: the control-plane
    record and the local runtime appear while the snapshot response is
    already in flight, so the GET result still lacks the new sandbox."""

    def __init__(self, inner, on_snapshot):
        self._inner = inner
        self._on_snapshot = on_snapshot
        self.last_post: dict | None = None

    async def get(self, url, headers):
        resp = await self._inner.get(url, headers=headers)
        self._on_snapshot()
        return resp

    async def post(self, url, json=None, headers=None):
        self.last_post = json
        return await self._inner.post(url, json=json, headers=headers)


@pytest.mark.asyncio
async def test_worker_reconcile_concurrent_create_not_killed(workspace):
    """E6.1 race (worker side): a runtime registered while reconciliation is
    in flight is a concurrent create — the worker must not tear it down, and
    must report it back so the control plane keeps its record."""
    control_nodes = NodeRegistry(heartbeat_timeout=1.0)
    _register_node(control_nodes, "node_a", "http://127.0.0.1:11111")
    registry = SandboxRegistry(ControlSettings(api_keys=("local-key",)))
    control_app = create_control_app(
        settings=ControlSettings(api_keys=("local-key",)),
        registry=registry,
        nodes_registry=control_nodes,
        workspace_base=workspace,
        node_address_resolver=_c3_resolver(),
    )
    rec_keep = _sandbox_on(registry, "node_a", "sbx_race_keep")
    registry.mark_orphaned("node_a")

    runtime_registry = RuntimeRegistry(workspace)
    envd_settings = EnvdSettings(executor="local")
    envd_app = create_envd_app(
        settings=envd_settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    keep_dir = workspace / "sbx_race_keep"
    keep_dir.mkdir(parents=True)
    (keep_dir / "workspace").mkdir()
    runtime_registry.register(
        sandbox_id="sbx_race_keep",
        access_token="tok",
        workspace_dir=str(keep_dir),
    )
    shutdowns: list[str] = []
    envd_app.state.runtimes["sbx_race_keep"] = SimpleNamespace(
        shutdown=lambda: shutdowns.append("sbx_race_keep")
    )

    def create_between_snapshot_and_diff():
        # The control plane schedules a new sandbox on this node and the
        # worker registers its runtime right after the snapshot response
        # was built (but before the agent computes local - known).
        rec_new = registry.create(
            template_id="base",
            sandbox_id="sbx_race_new",
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
        )
        rec_new.node_id = "node_a"
        registry.save(rec_new)
        new_dir = workspace / "sbx_race_new"
        new_dir.mkdir(parents=True)
        (new_dir / "workspace").mkdir()
        runtime_registry.register(
            sandbox_id="sbx_race_new",
            access_token="tok",
            workspace_dir=str(new_dir),
        )
        envd_app.state.runtimes["sbx_race_new"] = SimpleNamespace(
            shutdown=lambda: shutdowns.append("sbx_race_new")
        )

    agent = NodeAgent(
        settings=envd_settings,
        runtime_registry=runtime_registry,
        control_plane_url="http://control",
        node_address="http://127.0.0.1:11111",
    )
    agent._node_id = "node_a"
    headers = {
        "X-Internal-Key": ControlSettings(api_keys=("local-key",)).internal_api_key
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app), base_url="http://control"
    ) as client:
        racing = _RacingCreateClient(client, create_between_snapshot_and_diff)
        await agent._reconcile_with_control_plane(racing, headers)

    # The concurrent create was reported back with the snapshot ids.
    assert racing.last_post == {
        "sandboxIDs": ["sbx_race_keep", "sbx_race_new"],
        "snapshotIDs": ["sbx_race_keep"],
    }
    # The live sandbox was not torn down locally...
    assert runtime_registry.get("sbx_race_new") is not None
    assert shutdowns == []
    assert (workspace / "sbx_race_new").exists()
    # ...and its control-plane record survived.
    assert registry.get("sbx_race_new").state == "running"
    # The pre-existing orphan was restored as usual.
    assert registry.get("sbx_race_keep").state == "running"
    assert runtime_registry.get("sbx_race_keep") is not None
    assert rec_keep.sandbox_id in racing.last_post["sandboxIDs"]


@pytest.mark.asyncio
async def test_worker_reconcile_tears_down_orphan_runtime(workspace):
    """The worker side of recovery: a local runtime the control plane no
    longer knows (record deleted during the partition) is unregistered, its
    process tree shut down and its workspace removed."""
    control_nodes = NodeRegistry(heartbeat_timeout=1.0)
    _register_node(control_nodes, "node_a", "http://127.0.0.1:11111")
    registry = SandboxRegistry(ControlSettings(api_keys=("local-key",)))
    control_app = create_control_app(
        settings=ControlSettings(api_keys=("local-key",)),
        registry=registry,
        nodes_registry=control_nodes,
        workspace_base=workspace,
        node_address_resolver=_c3_resolver(),
    )

    runtime_registry = RuntimeRegistry(workspace)
    # The worker's settings must agree with the registry's base: the record
    # this test registers lives at ``<workspace_base>/<id>``, and the
    # orphan-tree GC anchors its teardown targets there (M4) instead of
    # trusting the record's own ``workspace_dir``.
    envd_settings = EnvdSettings(workspace_base=workspace, executor="local")
    envd_app = create_envd_app(
        settings=envd_settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    sandbox_id = "sbx_worker_orphan"
    sandbox_dir = workspace / sandbox_id
    sandbox_dir.mkdir(parents=True)
    (sandbox_dir / "workspace").mkdir()
    runtime_registry.register(
        sandbox_id=sandbox_id,
        access_token="tok",
        workspace_dir=str(sandbox_dir),
    )
    shutdowns: list[str] = []
    envd_app.state.runtimes[sandbox_id] = SimpleNamespace(
        shutdown=lambda: shutdowns.append(sandbox_id)
    )

    agent = NodeAgent(
        settings=envd_settings,
        runtime_registry=runtime_registry,
        control_plane_url="http://control",
        node_address="http://127.0.0.1:11111",
    )
    agent._node_id = "node_a"  # simulate a successful registration
    headers = {
        "X-Internal-Key": ControlSettings(api_keys=("local-key",)).internal_api_key
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app), base_url="http://control"
    ) as client:
        await agent._reconcile_with_control_plane(client, headers)

    assert runtime_registry.get(sandbox_id) is None
    assert shutdowns == [sandbox_id]
    assert not sandbox_dir.exists()
    # The control plane had no record for this sandbox, so nothing changed.
    assert registry.list() == []
