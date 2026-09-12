"""Option A contract: the worker's orphan-tree GC ("记录没了、树还在").

The probe report (``.superpowers/sdd/task-quotaleakprobe-report.md``) split the
"kill leaks quota entries" statement into its two halves. The leak is the
reverse ordering — the control-plane record is gone while the workspace tree,
its ``sandbox.json`` and its XFS quota row all survive — and the only reclaim
path (``xfs_quota.reconcile``) is fail-safe: it never touches a tree whose
``sandbox.json`` still claims the project id. These contracts pin the worker
side fix: after a (re)registration the agent reconciles the in-memory registry
**and** the trees still on disk, tears down the ones no control-plane record
references anywhere in the fleet, and reclaims their quota rows.

Every case where the two halves disagree asserts exact values: what the round
deleted, what it refused to delete, and what the control plane was told. A GC
that deletes one tree too many destroys a live sandbox, so "nothing was
deleted" is asserted as precisely as "this was deleted".
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import pytest

import envd_service.agent as agent_mod
import envd_service.xfs_quota as xfs_quota
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from envd_service.agent import NodeAgent
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry

INTERNAL_KEY = "internal-key"
API_KEY = "local-key"
STRANDED_PROJID = 1677253610
STRANDED_VOLUME_PROJID = 987654321


def _control_settings(**overrides) -> ControlSettings:
    return ControlSettings(
        api_keys=(API_KEY,),
        internal_api_key=INTERNAL_KEY,
        **overrides,
    )


def _envd_settings(workspace: Path, **overrides) -> EnvdSettings:
    return EnvdSettings(
        executor="local",
        workspace_base=workspace,
        internal_api_key=INTERNAL_KEY,
        # The quota-agent form is the deployment's supported source, and the
        # fake ops installed by these tests are exactly what the worker talks
        # to, so the contracts never need an XFS mount of their own.
        quota_via_agent=True,
        **overrides,
    )


class _QuotaFake:
    """Fake quota-agent ops over an explicit project table.

    ``reconcile`` reproduces the production semantics with the real
    ``_recorded_projids`` scan: a projid whose ``sandbox.json`` is still there
    keeps its row, a projid whose tree is gone becomes reclaimable.
    ``defer_rounds`` emulates XFS's deferred dquot accounting (the row still
    reports used blocks with the tree already removed), which is what makes
    the worker's bounded retry observable.
    """

    def __init__(self, rows: dict[int, int], *, defer_rounds: int = 0) -> None:
        self.rows = dict(rows)
        self.defer_rounds = defer_rounds
        self.reconcile_calls: list[dict] = []
        self.released: list[tuple[str, int]] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(
            xfs_quota,
            "agent_ops",
            {
                "reconcile": self.reconcile,
                "release": self.release,
                "provision": self.provision,
            },
        )

    def reconcile(self, *, workspace_base, mount_point) -> dict:
        recorded = xfs_quota._recorded_projids(workspace_base)
        orphans = sorted(projid for projid in self.rows if projid not in recorded)
        if self.defer_rounds > 0:
            self.defer_rounds -= 1
            result = {
                "cleaned": [],
                "skipped": [
                    {"projid": projid, "reason": "deferred accounting"}
                    for projid in orphans
                ],
            }
        else:
            for projid in orphans:
                self.rows.pop(projid)
            result = {"cleaned": orphans, "skipped": []}
        self.reconcile_calls.append(result)
        return result

    def release(self, *, project_dir, mount_point, projid) -> None:
        self.released.append((str(project_dir), int(projid)))

    def provision(self, **kwargs):  # pragma: no cover - never used here
        raise AssertionError("provisioning must not run in orphan-tree GC")


class _RecordingClient:
    """ASGI client that records every reconcile POST body."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.posts: list[dict] = []

    async def get(self, url, headers=None):
        return await self._inner.get(url, headers=headers)

    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return await self._inner.post(url, json=json, headers=headers)


def _headers() -> dict[str, str]:
    return {"X-Internal-Key": INTERNAL_KEY}


def _register_node(nodes: NodeRegistry, node_id: str, address: str) -> None:
    nodes.register(
        node_id=node_id,
        address=address,
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
        images=["python:3.11-slim"],
    )


def _control_record(registry: SandboxRegistry, node_id: str, sandbox_id: str):
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


def _tree(
    workspace: Path,
    sandbox_id: str,
    *,
    project_id: int | None = None,
    volume_projids: tuple[int, ...] = (),
    created_at: float = 1_600_000_000.0,
    record_text: str | None = None,
) -> Path:
    """Write a sandbox tree (``<base>/<id>/sandbox.json``) on disk."""
    sandbox_dir = workspace / sandbox_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "workspace").mkdir(exist_ok=True)
    if record_text is not None:
        (sandbox_dir / "sandbox.json").write_text(record_text, encoding="utf-8")
        return sandbox_dir
    payload: dict = {
        "sandbox_id": sandbox_id,
        "access_token": "tok",
        "workspace_dir": str(sandbox_dir),
    }
    if project_id is not None:
        payload["project_id"] = project_id
    if volume_projids:
        payload["volume_projects"] = [
            {
                "volume_id": f"vol_id_{projid}",
                "sandbox_id": sandbox_id,
                "mount_path": f"/mnt/vol_{projid}",
                # The slice directory is always named after its sandbox (the
                # guard in ``cleanup_volume_projects``).
                "sandbox_dir": str(
                    sandbox_dir / "volumes" / f"vol_id_{projid}" / sandbox_id
                ),
                "projid": projid,
            }
            for projid in volume_projids
        ]
    payload["created_at"] = created_at
    (sandbox_dir / "sandbox.json").write_text(json.dumps(payload), encoding="utf-8")
    return sandbox_dir


def _stack(workspace: Path, *, nodes: tuple[str, ...] = ("node_a",)):
    """Control plane over one workspace, with every node address unreachable."""
    control_nodes = NodeRegistry(heartbeat_timeout=600.0)
    for node_id in nodes:
        _register_node(control_nodes, node_id, "http://127.0.0.1:1")
    registry = SandboxRegistry(_control_settings())
    control_app = create_control_app(
        settings=_control_settings(),
        registry=registry,
        nodes_registry=control_nodes,
        workspace_base=workspace,
    )
    # Force scheduling onto the registered remote worker, exactly like the
    # E6.1 contracts next door.
    control_app.state.nodes.remove("local")
    return control_nodes, registry, control_app


def _agent(workspace: Path) -> NodeAgent:
    """A freshly started worker (empty in-memory registry) on ``workspace``."""
    agent = NodeAgent(
        settings=_envd_settings(workspace),
        runtime_registry=RuntimeRegistry(workspace),
        control_plane_url="http://control",
        node_address="http://127.0.0.1:1",
    )
    agent._node_id = "node_a"
    return agent


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    )


@pytest.mark.asyncio
async def test_restart_reports_the_live_tree_instead_of_stranding_it(
    workspace, monkeypatch
):
    """A worker restart must not cost a live sandbox its record.

    The worker comes back with an empty in-memory registry; before the fix it
    reported nothing, so ``recover_node`` deleted the record of a sandbox that
    was still fully present on disk (``-13``/``-15`` in the probe). The disk
    side of the reconcile reports it instead, so the record is restored.
    """
    _nodes, registry, control_app = _stack(workspace)
    sandbox_id = "sbx_live_after_restart"
    _control_record(registry, "node_a", sandbox_id)
    registry.mark_orphaned("node_a")
    assert registry.get(sandbox_id).state == "orphaned"
    sandbox_dir = _tree(workspace, sandbox_id, project_id=STRANDED_PROJID)
    quota = _QuotaFake({STRANDED_PROJID: 8})
    quota.install(monkeypatch)

    agent = _agent(workspace)
    assert agent._runtime_registry.list() == []
    async with _client(control_app) as raw:
        client = _RecordingClient(raw)
        summary = await agent._reconcile_with_control_plane(client, _headers())

    # The live sandbox keeps its record, its tree and its quota row.
    assert registry.get(sandbox_id).state == "running"
    assert sandbox_dir.exists()
    assert (sandbox_dir / "sandbox.json").is_file()
    assert quota.rows == {STRANDED_PROJID: 8}
    assert quota.reconcile_calls == []
    assert summary == {
        "deleted": [],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert client.posts == [
        {"sandboxIDs": [sandbox_id], "snapshotIDs": [sandbox_id]}
    ]


@pytest.mark.asyncio
async def test_restart_reclaims_an_unowned_tree_and_its_quota_row(
    workspace, monkeypatch, caplog
):
    """The trees behind the target machine's 12 pinned quota rows.

    No control-plane record anywhere, tree + ``sandbox.json`` + quota row on
    disk: one reconcile round removes the tree, releases the workspace and
    volume project ids through the record materialised from disk, and reclaims
    both rows — retrying once because XFS can still report the released
    accounting as in use (probe ``-05`` attempt 1).
    """
    _nodes, registry, control_app = _stack(workspace)
    assert registry.list() == []
    sandbox_id = "sbx_stranded"
    sandbox_dir = _tree(
        workspace,
        sandbox_id,
        project_id=STRANDED_PROJID,
        volume_projids=(STRANDED_VOLUME_PROJID,),
    )
    quota = _QuotaFake(
        {STRANDED_PROJID: 8, STRANDED_VOLUME_PROJID: 4}, defer_rounds=1
    )
    quota.install(monkeypatch)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        client = _RecordingClient(raw)
        summary = await agent._reconcile_with_control_plane(client, _headers())

    assert sandbox_dir.exists() is False
    assert quota.rows == {}
    assert summary == {
        "deleted": [sandbox_id],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "concurrent_creates": [],
        "quota_cleaned": [STRANDED_VOLUME_PROJID, STRANDED_PROJID],
        "quota_unreclaimed": [],
    }
    assert client.posts == [{"sandboxIDs": [], "snapshotIDs": []}]
    # The tree was gone before the second pass: the first pass skipped the
    # rows (deferred accounting), the retry reclaimed them, and the retry
    # stopped there instead of looping.
    assert quota.reconcile_calls == [
        {
            "cleaned": [],
            "skipped": [
                {"projid": STRANDED_VOLUME_PROJID, "reason": "deferred accounting"},
                {"projid": STRANDED_PROJID, "reason": "deferred accounting"},
            ],
        },
        {
            "cleaned": [STRANDED_VOLUME_PROJID, STRANDED_PROJID],
            "skipped": [],
        },
    ]
    # The disk-materialised record supplied the exact teardown targets.
    volume_slice = sandbox_dir / "volumes" / f"vol_id_{STRANDED_VOLUME_PROJID}"
    assert sorted(quota.released) == [
        (str(sandbox_dir), STRANDED_PROJID),
        (str(volume_slice / sandbox_id), STRANDED_VOLUME_PROJID),
    ]
    assert [record.message for record in caplog.records] == [
        f"reconcile: removing orphan runtime {sandbox_id} (not in control plane)",
    ]


@pytest.mark.asyncio
async def test_quota_reclaim_is_bounded_and_reported_when_accounting_never_settles(
    workspace, monkeypatch, caplog
):
    """A row that never settles is reported, not retried forever.

    The tree removal is already correct in this case; the round must stay
    quiet about it apart from a precise warning, and a later reconcile (the
    next startup) still reclaims the row.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_never_settles"
    sandbox_dir = _tree(workspace, sandbox_id, project_id=STRANDED_PROJID)
    quota = _QuotaFake({STRANDED_PROJID: 8}, defer_rounds=99)
    quota.install(monkeypatch)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert sandbox_dir.exists() is False
    assert len(quota.reconcile_calls) == agent_mod._QUOTA_RECLAIM_ATTEMPTS
    assert summary["deleted"] == [sandbox_id]
    assert summary["quota_cleaned"] == []
    assert summary["quota_unreclaimed"] == [STRANDED_PROJID]
    assert (
        f"reconcile: 1 quota row(s) not reclaimed after "
        f"{agent_mod._QUOTA_RECLAIM_ATTEMPTS} attempt(s): [{STRANDED_PROJID}]"
    ) in [record.message for record in caplog.records]

    quota.defer_rounds = 0
    async with _client(control_app) as raw:
        second = await agent._reconcile_with_control_plane(raw, _headers())
    # Nothing to delete the second time (the tree is gone) and no projids to
    # reclaim from this round; the row is reclaimed by the quota reconcile the
    # next worker start schedules, exactly as before this change.
    assert second["deleted"] == []
    assert quota.rows == {STRANDED_PROJID: 8}
    assert xfs_quota.reconcile_orphan_projects(
        workspace_base=workspace, mount_point=workspace, via_agent=True
    ) == {"cleaned": [STRANDED_PROJID], "skipped": []}
    assert quota.rows == {}


@pytest.mark.asyncio
async def test_kill_while_the_hosting_worker_is_down_is_reclaimed_on_the_next_start(
    workspace, monkeypatch
):
    """Probe ``-14``: kill succeeds (204) while the hosting worker is stopped.

    The record goes, the tree and its quota row stay. The next worker start
    must reclaim both — the option-A branch of the report's case 2.
    """
    _nodes, registry, control_app = _stack(workspace)
    sandbox_id = "sbx_killed_while_down"
    _control_record(registry, "node_a", sandbox_id)
    sandbox_dir = _tree(workspace, sandbox_id, project_id=STRANDED_PROJID)
    quota = _QuotaFake({STRANDED_PROJID: 8})
    quota.install(monkeypatch)

    async with _client(control_app) as client:
        killed = await client.delete(
            f"/sandboxes/{sandbox_id}", headers={"X-API-Key": API_KEY}
        )
    assert killed.status_code == 204
    with pytest.raises(KeyError):
        registry.get(sandbox_id)
    # The remote teardown could not reach the worker, so nothing was removed.
    assert sandbox_dir.exists()
    assert (sandbox_dir / "sandbox.json").is_file()
    assert quota.rows == {STRANDED_PROJID: 8}

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())
    assert sandbox_dir.exists() is False
    assert quota.rows == {}
    assert summary["deleted"] == [sandbox_id]
    assert summary["quota_cleaned"] == [STRANDED_PROJID]
    assert summary["quota_unreclaimed"] == []


@pytest.mark.asyncio
async def test_gc_protects_paused_migrating_and_reserved_entries(
    workspace, monkeypatch, caplog
):
    """The three ways the GC could destroy live data (report §5.4 case 3).

    paused: the record is still in the control plane, so the tree is not an
    orphan. Migration source (``keepFiles=true``): the record is still there,
    on this node or another one of the shared workspace. Reserved roots and
    symlinks: not sandbox trees at all, so they are never even considered.
    """
    _nodes, registry, control_app = _stack(workspace, nodes=("node_a", "node_b"))
    paused_id = "sbx_paused_keep"
    _control_record(registry, "node_a", paused_id)
    registry.pause(registry.get(paused_id))
    assert registry.get(paused_id).state == "paused"
    paused_dir = _tree(workspace, paused_id, project_id=1001)

    migrating_id = "sbx_migrating_keep"
    _control_record(registry, "node_b", migrating_id)
    migrating_dir = _tree(workspace, migrating_id, project_id=1002)
    # The source stop that migration performs (`?keepFiles=true`): the record
    # stays and the tree must stay with it.
    envd_settings = _envd_settings(workspace)
    runtime_registry = RuntimeRegistry(workspace)
    runtime_registry.register(
        sandbox_id=migrating_id,
        access_token="tok",
        workspace_dir=str(migrating_dir),
        project_id=1002,
    )
    envd_app = create_envd_app(
        settings=envd_settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    async with _client(envd_app) as worker:
        stopped = await worker.delete(
            f"/agent/sandboxes/{migrating_id}?keepFiles=true", headers=_headers()
        )
    assert stopped.status_code == 204
    assert migrating_dir.exists()
    assert (migrating_dir / "sandbox.json").is_file()

    # Reserved infrastructure, a symlink and a plain file are not sandboxes.
    snapshot_marker = workspace / "_snapshots" / "snap_a" / "fs" / "marker.txt"
    snapshot_marker.parent.mkdir(parents=True, exist_ok=True)
    snapshot_marker.write_text("snapshot data", encoding="utf-8")
    for name in ("_migrate", "_volumes", "_templates", "_cow", "_secrets"):
        (workspace / name).mkdir(exist_ok=True)
    symlink = workspace / "sbx_symlinked"
    symlink.symlink_to(workspace / "_snapshots")
    plain_file = workspace / "sbx_not_a_dir"
    plain_file.write_text("not a tree", encoding="utf-8")

    quota = _QuotaFake({1001: 8, 1002: 8})
    quota.install(monkeypatch)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert paused_dir.exists()
    assert (paused_dir / "sandbox.json").is_file()
    assert registry.get(paused_id).state == "paused"
    assert migrating_dir.exists()
    assert (migrating_dir / "sandbox.json").is_file()
    assert registry.get(migrating_id).state == "running"
    assert snapshot_marker.read_text(encoding="utf-8") == "snapshot data"
    assert symlink.is_symlink()
    assert plain_file.read_text(encoding="utf-8") == "not a tree"
    assert quota.rows == {1001: 8, 1002: 8}
    assert summary == {
        "deleted": [],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [migrating_id],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert [record.message for record in caplog.records] == []


@pytest.mark.asyncio
async def test_trees_without_a_readable_record_are_reported_never_deleted(
    workspace, monkeypatch, caplog
):
    """Fail-safe on an unusable ``sandbox.json`` (report requirement 3).

    Without a record there is no project id to release, so deleting the tree
    would be all risk and no reclaim. The round must count and name them so
    operators can decide whether to schedule a cleanup task.
    """
    _nodes, _registry, control_app = _stack(workspace)
    missing = workspace / "sbx_no_record"
    missing.mkdir()
    corrupt = _tree(workspace, "sbx_corrupt_record", record_text="{not json")
    empty = _tree(workspace, "sbx_empty_record", record_text="{}")
    quota = _QuotaFake({})
    quota.install(monkeypatch)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert missing.exists()
    assert corrupt.exists()
    assert empty.exists()
    assert summary == {
        "deleted": [],
        "delete_failures": [],
        "unmaterialised": [
            "sbx_corrupt_record",
            "sbx_empty_record",
            "sbx_no_record",
        ],
        "protected_elsewhere": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert [record.message for record in caplog.records] == [
        f"reconcile: cannot read the sandbox record of {empty}",
        "reconcile: 3 sandbox tree(s) on disk have no readable sandbox.json "
        "and were left alone: sbx_corrupt_record,sbx_empty_record,sbx_no_record",
    ]


@pytest.mark.asyncio
async def test_one_failing_tree_does_not_abort_the_round(
    workspace, monkeypatch, caplog
):
    """A single unrecoverable tree keeps the round alive (requirement 5).

    The failing tree is reported, the other tree is still torn down, its
    quota row is still reclaimed, and the control-plane reconcile POST still
    happens.
    """
    _nodes, _registry, control_app = _stack(workspace)
    failing_id = "sbx_teardown_fails"
    surviving_id = "sbx_teardown_ok"
    failing_dir = _tree(workspace, failing_id, project_id=2001)
    surviving_dir = _tree(workspace, surviving_id, project_id=2002)
    quota = _QuotaFake({2001: 8, 2002: 8})
    quota.install(monkeypatch)

    from envd_service import priv_helpers

    real_remove_tree = priv_helpers.remove_tree

    def flaky_remove_tree(path, **kwargs):
        if Path(path) == failing_dir:
            raise OSError("simulated permission wall")
        return real_remove_tree(path, **kwargs)

    monkeypatch.setattr(priv_helpers, "remove_tree", flaky_remove_tree)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        client = _RecordingClient(raw)
        summary = await agent._reconcile_with_control_plane(client, _headers())

    assert failing_dir.exists()
    assert surviving_dir.exists() is False
    # The project state of the failing tree was released before its rmtree
    # failed; its quota row stays pinned by fail-safe (the tree and its
    # sandbox.json are still there).
    assert sorted(quota.released) == [
        (str(failing_dir), 2001),
        (str(surviving_dir), 2002),
    ]
    assert summary == {
        "deleted": [surviving_id],
        "delete_failures": [failing_id],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "concurrent_creates": [],
        "quota_cleaned": [2002],
        "quota_unreclaimed": [],
    }
    assert client.posts == [{"sandboxIDs": [], "snapshotIDs": []}]
    assert [record.message for record in caplog.records] == [
        f"reconcile: removing orphan runtime {failing_id} (not in control plane)",
        f"reconcile: orphan runtime {failing_id} teardown failed; continuing",
        f"reconcile: removing orphan runtime {surviving_id} (not in control plane)",
    ]


@pytest.mark.asyncio
async def test_shared_workspace_keeps_another_nodes_live_tree(workspace, monkeypatch):
    """Production topology: every worker mounts the same workspace volume.

    ``deploy/stack/docker-compose.prod.yml`` mounts ``sandbox-shared`` into
    every worker, so this worker sees the other node's live trees. A node-local
    snapshot cannot tell them from a true orphan: the fleet-wide record set
    can, and only a tree no record references is reclaimed.
    """
    _nodes, registry, control_app = _stack(workspace, nodes=("node_a", "node_b"))
    other_id = "sbx_other_node"
    _control_record(registry, "node_b", other_id)
    other_dir = _tree(workspace, other_id, project_id=3001)
    orphan_id = "sbx_unowned_shared"
    orphan_dir = _tree(workspace, orphan_id, project_id=3002)
    quota = _QuotaFake({3001: 8, 3002: 8})
    quota.install(monkeypatch)

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        client = _RecordingClient(raw)
        summary = await agent._reconcile_with_control_plane(client, _headers())

    assert other_dir.exists()
    assert registry.get(other_id).state == "running"
    assert registry.get(other_id).node_id == "node_b"
    assert orphan_dir.exists() is False
    assert quota.rows == {3001: 8}
    # Scanning must not have cached the other node's record in this worker:
    # an in-memory record is treated as one this worker owns and may tear
    # down, so a second round would then destroy the same live tree.
    assert agent._runtime_registry.list() == []
    assert summary == {
        "deleted": [orphan_id],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [other_id],
        "concurrent_creates": [],
        "quota_cleaned": [3002],
        "quota_unreclaimed": [],
    }
    assert client.posts == [{"sandboxIDs": [], "snapshotIDs": []}]
    async with _client(control_app) as raw:
        second = await agent._reconcile_with_control_plane(raw, _headers())
    assert second["deleted"] == []
    assert second["protected_elsewhere"] == [other_id]
    assert other_dir.exists()
    assert agent._runtime_registry.list() == []


@pytest.mark.asyncio
async def test_runtime_whose_record_moved_away_is_not_torn_down(
    workspace, monkeypatch
):
    """A migration that already moved the record must not cost the tree.

    The source worker can still hold the runtime in memory (it crashed half
    way through the source stop) while the record now belongs to the target
    node. In a shared workspace that tree is exactly what the target serves,
    so fleet-wide ownership protects it even on the in-memory path.
    """
    _nodes, registry, control_app = _stack(workspace, nodes=("node_a", "node_b"))
    moved_id = "sbx_moved_away"
    _control_record(registry, "node_b", moved_id)
    moved_dir = _tree(workspace, moved_id, project_id=6001)
    quota = _QuotaFake({6001: 8})
    quota.install(monkeypatch)

    agent = _agent(workspace)
    agent._runtime_registry.register(
        sandbox_id=moved_id,
        access_token="tok",
        workspace_dir=str(moved_dir),
        project_id=6001,
    )
    async with _client(control_app) as raw:
        client = _RecordingClient(raw)
        summary = await agent._reconcile_with_control_plane(client, _headers())

    assert moved_dir.exists()
    assert registry.get(moved_id).node_id == "node_b"
    assert registry.get(moved_id).state == "running"
    assert quota.rows == {6001: 8}
    assert summary["deleted"] == []
    assert summary["delete_failures"] == []
    assert summary["protected_elsewhere"] == [moved_id]
    assert client.posts == [{"sandboxIDs": [], "snapshotIDs": []}]


@pytest.mark.asyncio
async def test_incomplete_fleet_enumeration_aborts_the_disk_sweep(
    workspace, monkeypatch, caplog
):
    """Delete nothing when the fleet's records cannot all be accounted for.

    A record whose node is not in the node registry (right after a
    control-plane restart, before that worker re-registers) makes the
    enumeration short of ``/internal/fleet/metrics``'s record count. The sweep
    must then leave every disk tree alone instead of guessing.
    """
    control_nodes, registry, control_app = _stack(workspace)
    ghost_id = "sbx_ghost_node"
    _control_record(registry, "node_ghost", ghost_id)
    assert [node.node_id for node in control_nodes.list()] == ["node_a"]
    orphan_dir = _tree(workspace, "sbx_unowned", project_id=4001)
    quota = _QuotaFake({4001: 8})
    quota.install(monkeypatch)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert orphan_dir.exists()
    assert quota.rows == {4001: 8}
    assert summary == {
        "deleted": [],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert [record.message for record in caplog.records] == [
        "reconcile: fleet sandbox enumeration is incomplete "
        "(0 of 1 records accounted for)",
        "reconcile: leaving 1 orphan tree(s) on disk alone this round "
        "(fleet record enumeration unavailable): sbx_unowned",
    ]


@pytest.mark.asyncio
async def test_tree_created_during_the_reconcile_window_is_a_concurrent_create(
    workspace, monkeypatch
):
    """The snapshot-time boundary protects a create racing the reconcile.

    ``created_at`` (read from the tree's own ``sandbox.json``) after
    ``reconcile_started_at`` means the create raced the snapshot: the tree is
    kept and reported back so the control plane never deletes its record.
    """
    _nodes, _registry, control_app = _stack(workspace)
    racing_id = "sbx_racing_create"
    racing_dir = _tree(
        workspace, racing_id, project_id=5001, created_at=time.time() + 60
    )
    quota = _QuotaFake({5001: 8})
    quota.install(monkeypatch)

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        client = _RecordingClient(raw)
        summary = await agent._reconcile_with_control_plane(client, _headers())

    assert racing_dir.exists()
    assert quota.rows == {5001: 8}
    assert summary["deleted"] == []
    assert summary["concurrent_creates"] == [racing_id]
    assert summary["quota_unreclaimed"] == []
    assert client.posts == [{"sandboxIDs": [racing_id], "snapshotIDs": []}]
