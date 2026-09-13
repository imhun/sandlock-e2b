"""W1 contract: the explicit delete endpoint acts only on verified targets.

Review round 1 (M4) brought the *orphan-tree GC* under the rule that a
``sandbox.json`` living inside a sandbox-owned tree is input the sandbox can
rewrite: the sweep targets ``<workspace_base>/<id>`` by convention, reads the
project ids off the disk, and refuses a record that contradicts them. The
explicit delete endpoint (``DELETE /agent/sandboxes/{id}`` -- owner kill, TTL
expiry, eviction, migration stop) kept resolving its targets from the record,
so a sandbox that replaced its ``sandbox.json`` (``unlink`` + recreate: the
tree is ``0770`` and owned by the sandbox's host uid) with
``workspace_dir = <someone else's tree>`` had *that* tree rmtree'd and *that*
tenant's quota row released when it was deleted.

``tmp/w1-01-reachability.py`` measures the shape end to end against HEAD
(``tmp/w1-01-reachability-red.log``: 204, the victim's tree and quota row
gone) and against this change (``-green.log``: 409, nothing touched). These
contracts pin the four rules, the unchanged normal semantics, and the two
teardown races review round 1 registered (loop-owned callbacks, a ``get()``
inside the teardown window).

Every assertion is an exact value: a teardown that removes one tree too many
destroys a live sandbox, so "the victim survived" is asserted as precisely as
"the tree was removed".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import threading
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
#: The project ids of an unrelated, live tenant the rewritten record aims at
#: (the same pair the M4 contracts use for the GC's version of this shape).
VICTIM_PROJID = 1807253611
VICTIM_VOLUME_PROJID = 1876543211
#: The legitimate owner of the tree that was rewritten.
LIAR_PROJID = 7001
LIAR_VOLUME_PROJID = 7002


def _envd_settings(workspace: Path, **overrides) -> EnvdSettings:
    return EnvdSettings(
        executor="local",
        workspace_base=workspace,
        internal_api_key=INTERNAL_KEY,
        # The quota-agent form is the deployment's supported source, so the
        # fakes these contracts install are exactly what the worker talks to.
        quota_via_agent=True,
        **overrides,
    )


class _QuotaFake:
    """Quota-agent ops over an explicit table, recording what was released."""

    def __init__(self, rows: dict[int, int] | None = None) -> None:
        self.rows = dict(rows or {})
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
        return {"cleaned": [], "skipped": []}

    def release(self, *, project_dir, mount_point, projid) -> None:
        self.released.append((str(project_dir), int(projid)))

    def provision(self, **kwargs):  # pragma: no cover - never used here
        raise AssertionError("provisioning must not run in these contracts")


def _install_disk_projids(
    monkeypatch, mapping: dict[Path, int]
) -> list[list[str]]:
    """Answer the project-id read from an explicit disk table.

    ``_verified_project_id`` reads the truth from the filesystem (the record
    inside the sandbox-owned tree is input the sandbox can rewrite), so these
    contracts supply the one read this host cannot do itself. What is faked is
    ``lsattr -p -d``'s stdout: the real parser and the real wiring still run.
    A directory absent from ``mapping`` reports no project id.
    """
    calls: list[list[str]] = []
    real_run = subprocess.run
    monkeypatch.setattr(xfs_quota, "_use_quotactl_read", lambda mount_point: False)

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "lsattr":
            calls.append(list(argv))
            projid = mapping.get(Path(argv[-1]))
            stdout = (
                ""
                if projid is None
                else f"{projid:>8} ---------------- {argv[-1]}\n"
            )
            return subprocess.CompletedProcess(list(argv), 0, stdout, "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(xfs_quota.subprocess, "run", fake_run)
    return calls


def _write_record(base: Path, sandbox_id: str, **payload) -> Path:
    """Write ``<base>/<id>/sandbox.json`` (plus a payload file)."""
    tree = base / sandbox_id
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "workspace").mkdir(exist_ok=True)
    (tree / "payload.bin").write_bytes(f"payload-of-{sandbox_id}".encode())
    record = {
        "sandbox_id": sandbox_id,
        "access_token": "tok",
        "workspace_dir": str(tree),
    }
    record.update(payload)
    (tree / "sandbox.json").write_text(json.dumps(record), encoding="utf-8")
    return tree


def _worker_app(workspace: Path, **settings_overrides) -> object:
    return create_envd_app(
        settings=_envd_settings(workspace, **settings_overrides),
    )


async def _delete(app, sandbox_id: str, params: str = "") -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.delete(
            f"/agent/sandboxes/{sandbox_id}{params}",
            headers={"X-Internal-Key": INTERNAL_KEY},
        )


def _agent_log_records(caplog) -> list[str]:
    return [
        record.message
        for record in caplog.records
        if record.name == "envd_service.agent"
    ]


# ---------------------------------------------------------------------------
# The four verification rules, on the delete endpoint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_record_pointing_at_another_tree_is_refused(
    workspace, monkeypatch, caplog
):
    """The rewritten ``workspace_dir`` may not aim the teardown anywhere.

    The victim is a live, unrelated tenant: its tree, its record, its payload
    and its quota row all have to be exactly as they were, and the sandbox
    whose record was rewritten is not torn down either (the record is the only
    thing on this host that describes it, and it has been shown to lie).
    """
    victim_dir = _write_record(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    liar_dir = _write_record(
        workspace,
        "sbx_liar",
        project_id=VICTIM_PROJID,
        workspace_dir=str(victim_dir),
    )
    quota = _QuotaFake({VICTIM_PROJID: 8, LIAR_PROJID: 8})
    app = _worker_app(workspace)
    quota.install(monkeypatch)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete(app, "sbx_liar")

    assert response.status_code == 409
    assert response.text == (
        f"refusing to tear down sbx_liar: its sandbox.json points at {victim_dir}"
    )
    assert _agent_log_records(caplog) == [
        f"delete: refusing to tear down sbx_liar: "
        f"its sandbox.json points at {victim_dir}"
    ]
    # Nothing was touched: the victim's tree, payload, record and row are all
    # intact, and the rewritten tree is left standing for an operator to see.
    assert victim_dir.is_dir()
    assert (victim_dir / "payload.bin").read_bytes() == b"payload-of-sbx_victim"
    assert json.loads((victim_dir / "sandbox.json").read_text())["project_id"] == (
        VICTIM_PROJID
    )
    assert liar_dir.is_dir()
    assert quota.released == []
    assert quota.rows == {VICTIM_PROJID: 8, LIAR_PROJID: 8}


@pytest.mark.asyncio
async def test_a_project_id_that_contradicts_the_disk_is_refused(
    workspace, monkeypatch, caplog
):
    """A cached record is checked against the disk as well.

    The record's ``workspace_dir`` is honest here -- only ``project_id`` was
    rewritten, to the victim's -- and the registry has it cached in memory
    (any request that called ``get()`` does that). The disk says the tree
    belongs to ``LIAR_PROJID``, so releasing the claimed id would clear the
    victim's quota row: refused.
    """
    _write_record(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    liar_dir = _write_record(workspace, "sbx_liar", project_id=VICTIM_PROJID)
    quota = _QuotaFake({VICTIM_PROJID: 8, LIAR_PROJID: 8})
    app = _worker_app(workspace)
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {liar_dir: LIAR_PROJID})
    # The sandbox-writable record reaches the registry the way it would in a
    # live worker: read back through ``get()``.
    assert app.state.runtime_registry.get("sbx_liar").project_id == VICTIM_PROJID
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete(app, "sbx_liar")

    assert response.status_code == 409
    assert response.text == (
        "refusing to tear down sbx_liar: its sandbox.json claims project id "
        f"{VICTIM_PROJID} but the disk says {LIAR_PROJID}"
    )
    assert _agent_log_records(caplog) == [
        "delete: refusing to tear down sbx_liar: its sandbox.json claims "
        f"project id {VICTIM_PROJID} but the disk says {LIAR_PROJID}"
    ]
    assert liar_dir.is_dir()
    assert quota.released == []
    assert quota.rows == {VICTIM_PROJID: 8, LIAR_PROJID: 8}


@pytest.mark.asyncio
async def test_a_volume_entry_naming_another_slice_is_refused(
    workspace, monkeypatch, caplog
):
    """Only a slice named after this sandbox, inside the volume root, is acted on.

    The tree itself is verifiable (its name is this sandbox's, the disk
    confirms its project id), so it is reclaimed -- but the two rewritten
    volume entries are refused one by one: one names the victim's slice, one
    sits outside the configured shared volume root. Neither the victim's slice
    nor the victim's quota row is touched.
    """
    volume_root = workspace / "_volumes"
    victim_slice = volume_root / "sbx_victim"
    victim_slice.mkdir(parents=True)
    (victim_slice / "victim.bin").write_bytes(b"another tenant's volume")
    outside_slice = workspace / "elsewhere" / "sbx_liar"
    outside_slice.mkdir(parents=True)
    liar_dir = _write_record(
        workspace,
        "sbx_liar",
        project_id=LIAR_PROJID,
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_victim",
                "mount_path": "mnt/data",
                "sandbox_dir": str(victim_slice),
                "projid": VICTIM_VOLUME_PROJID,
            },
            {
                "volume_id": "vol_2",
                "sandbox_id": "sbx_liar",
                "mount_path": "mnt/other",
                "sandbox_dir": str(outside_slice),
                "projid": LIAR_VOLUME_PROJID,
            },
        ],
    )
    quota = _QuotaFake({VICTIM_VOLUME_PROJID: 8, LIAR_VOLUME_PROJID: 8})
    app = _worker_app(workspace, shared_volume_root=str(volume_root))
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {liar_dir: LIAR_PROJID})
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete(app, "sbx_liar")

    assert response.status_code == 204
    assert _agent_log_records(caplog) == [
        f"delete: refusing a volume entry of sbx_liar: {victim_slice} is not "
        "a slice of this sandbox",
        f"delete: refusing the volume slice {outside_slice} of sbx_liar: it "
        f"is outside the shared volume root {volume_root}",
    ]
    # The workspace is reclaimed with the project id the disk reports ...
    assert liar_dir.exists() is False
    assert quota.released == [(str(liar_dir), LIAR_PROJID)]
    # ... and only that: the victim's slice and its row survive, and the
    # out-of-root slice is not deleted on a record's say-so either.
    assert (victim_slice / "victim.bin").read_bytes() == b"another tenant's volume"
    assert outside_slice.is_dir()
    assert quota.rows == {VICTIM_VOLUME_PROJID: 8, LIAR_VOLUME_PROJID: 8}


# ---------------------------------------------------------------------------
# Normal delete semantics, unchanged (keepFiles / keepVolumeSlices included).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_delete_releases_the_disks_project_ids(
    workspace, monkeypatch, caplog
):
    """An honest record deletes exactly what it did before."""
    volume_root = workspace / "_volumes"
    slice_dir = volume_root / "sbx_clean"
    slice_dir.mkdir(parents=True)
    (slice_dir / "data.bin").write_bytes(b"the sandbox's own volume")
    tree = _write_record(
        workspace,
        "sbx_clean",
        project_id=7001,
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_clean",
                "mount_path": "mnt/data",
                "sandbox_dir": str(slice_dir),
                "projid": 7002,
            }
        ],
    )
    quota = _QuotaFake({7001: 8, 7002: 8})
    app = _worker_app(workspace, shared_volume_root=str(volume_root))
    quota.install(monkeypatch)
    disk_calls = _install_disk_projids(
        monkeypatch, {tree: 7001, slice_dir: 7002}
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete(app, "sbx_clean")

    assert response.status_code == 204
    assert _agent_log_records(caplog) == []
    assert sorted(quota.released) == [
        (str(slice_dir), 7002),
        (str(tree), 7001),
    ]
    assert disk_calls == [
        ["lsattr", "-p", "-d", str(tree)],
        ["lsattr", "-p", "-d", str(slice_dir)],
    ]
    assert tree.exists() is False
    assert slice_dir.exists() is False
    assert app.state.runtime_registry.get("sbx_clean") is None
    assert app.state.runtime_registry.list() == []


@pytest.mark.asyncio
async def test_keep_files_still_keeps_the_tree_and_releases_nothing(
    workspace, monkeypatch, caplog
):
    """Migration stop: the runtime goes, the files and the rows stay."""
    volume_root = workspace / "_volumes"
    slice_dir = volume_root / "sbx_keep"
    slice_dir.mkdir(parents=True)
    (slice_dir / "data.bin").write_bytes(b"keep-me")
    tree = _write_record(
        workspace,
        "sbx_keep",
        project_id=7001,
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_keep",
                "mount_path": "mnt/data",
                "sandbox_dir": str(slice_dir),
                "projid": 7002,
            }
        ],
    )
    quota = _QuotaFake({7001: 8, 7002: 8})
    app = _worker_app(workspace, shared_volume_root=str(volume_root))
    quota.install(monkeypatch)
    disk_calls = _install_disk_projids(monkeypatch, {})
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete(app, "sbx_keep", params="?keepFiles=true")

    assert response.status_code == 204
    assert _agent_log_records(caplog) == []
    # Nothing is released and nothing is read: the files are staying, so there
    # is no project state this call could act on.
    assert quota.released == []
    assert disk_calls == []
    assert tree.is_dir()
    assert (slice_dir / "data.bin").read_bytes() == b"keep-me"
    # The record file stays with the tree, so a later request reads it back
    # from the disk -- unchanged: keepFiles is a stop, not a delete.
    assert (tree / "sandbox.json").is_file()


@pytest.mark.asyncio
async def test_keep_volume_slices_still_removes_only_the_workspace(
    workspace, monkeypatch, caplog
):
    """Migration cleanup: the workspace goes, the shared slice stays."""
    volume_root = workspace / "_volumes"
    slice_dir = volume_root / "sbx_migrated"
    slice_dir.mkdir(parents=True)
    (slice_dir / "payload.bin").write_bytes(b"the target still serves this")
    tree = _write_record(
        workspace,
        "sbx_migrated",
        project_id=7001,
        volume_projects=[
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_migrated",
                "mount_path": "mnt/data",
                "sandbox_dir": str(slice_dir),
                "projid": 7002,
            }
        ],
    )
    quota = _QuotaFake({7001: 8, 7002: 8})
    app = _worker_app(workspace, shared_volume_root=str(volume_root))
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {tree: 7001, slice_dir: 7002})
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete(app, "sbx_migrated", params="?keepVolumeSlices=true")

    assert response.status_code == 204
    assert _agent_log_records(caplog) == []
    assert quota.released == [(str(tree), 7001)]
    assert tree.exists() is False
    assert (slice_dir / "payload.bin").read_bytes() == b"the target still serves this"


# ---------------------------------------------------------------------------
# The two teardown races review round 1 registered (F2 / F3).
# ---------------------------------------------------------------------------


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


def _control_stack(workspace: Path):
    """Control plane over one workspace, with every node address unreachable."""
    settings = ControlSettings(api_keys=(API_KEY,), internal_api_key=INTERNAL_KEY)
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    _register_node(nodes, "node_a", "http://127.0.0.1:1")
    registry = SandboxRegistry(settings)
    app = create_control_app(
        settings=settings,
        registry=registry,
        nodes_registry=nodes,
        workspace_base=workspace,
    )
    # Force scheduling onto the registered remote worker, exactly like the
    # orphan-tree GC contracts next door.
    app.state.nodes.remove("local")
    return nodes, registry, app


def _worker_agent(workspace: Path) -> NodeAgent:
    """A freshly started worker (empty in-memory registry) on ``workspace``."""
    agent = NodeAgent(
        settings=_envd_settings(workspace),
        runtime_registry=RuntimeRegistry(workspace),
        control_plane_url="http://control",
        node_address="http://127.0.0.1:1",
    )
    agent._node_id = "node_a"
    return agent


def _headers() -> dict[str, str]:
    return {"X-Internal-Key": INTERNAL_KEY}


@pytest.mark.asyncio
async def test_the_teardown_unregisters_on_the_event_loop(workspace, monkeypatch):
    """Race A: the unregister callback is loop-side code (F2).

    The production callback pops the sandbox's runtime context and shuts it
    down, which cancels the MCP gateway watch -- an ``asyncio.Task`` owned by
    the event loop, and ``Task.cancel()`` is not a policy-free call from
    another thread. The round's heavy half belongs on a worker thread, but the
    unregister must happen here, where the loop is.
    """
    _nodes, registry, control_app = _control_stack(workspace)
    _control_record(registry, "node_a", "sbx_race_loop")
    orphan_id = "sbx_race_orphan"
    orphan_dir = _write_record(workspace, orphan_id, project_id=8001)
    quota = _QuotaFake({8001: 8})
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {orphan_dir: 8001})

    agent = _worker_agent(workspace)
    seen: dict[str, str] = {}
    watcher: asyncio.Task | None = None

    async def watch() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["watch"] = "cancelled"
            raise

    def on_unregister(sandbox_id: str) -> None:
        # Exactly the production shape: the callback runs on whichever thread
        # called unregister(), and it cancels a task this loop owns.
        seen["callback_thread"] = threading.current_thread().name
        seen["callback_id"] = sandbox_id
        watcher.cancel()

    agent._runtime_registry.add_unregister_callback(on_unregister)
    # A record this process has read (any request that called get() does that)
    # is what makes the unregister fire its callbacks.
    assert agent._runtime_registry.get(orphan_id) is not None
    watcher = asyncio.create_task(watch())
    # Let the watcher reach its await: the cancellation below is then
    # delivered to the task's body (as it is for the long-lived MCP gateway
    # watch in production), so what the assertions measure is the thread the
    # callback ran on and not a scheduling accident.
    await asyncio.sleep(0)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control_app),
            base_url="http://control",
        ) as raw:
            seen["loop_thread"] = threading.current_thread().name
            summary = await agent._reconcile_with_control_plane(raw, _headers())
    finally:
        # Never leave the test hanging on the watcher (a round that did not
        # unregister correctly must fail an assertion, not stall the suite).
        if not watcher.done():
            watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher

    assert summary["deleted"] == [orphan_id]
    assert orphan_dir.exists() is False
    assert seen == {
        "callback_thread": "MainThread",
        "callback_id": orphan_id,
        "watch": "cancelled",
        "loop_thread": "MainThread",
    }


@pytest.mark.asyncio
async def test_a_get_inside_the_teardown_window_does_not_resurrect_the_record(
    workspace, monkeypatch
):
    """Race B: the teardown's heavy half runs off the loop (F3).

    ``unregister()`` does not delete ``sandbox.json`` and the rmtree comes
    after the quota release, so a request that calls ``get()`` in that window
    used to read the not-yet-deleted file straight back into the registry: the
    registry claimed a sandbox whose tree was already being removed, and only
    the next round's self-healing cleared it.
    """
    _nodes, registry, control_app = _control_stack(workspace)
    _control_record(registry, "node_a", "sbx_race_get")
    sandbox_id = "sbx_race_window"
    tree = _write_record(workspace, sandbox_id, project_id=8101)
    quota = _QuotaFake({8101: 8})
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {tree: 8101})

    agent = _worker_agent(workspace)
    registry_ = agent._runtime_registry
    # The record is in memory, the way any request that called get() leaves it.
    assert registry_.get(sandbox_id) is not None
    observed: dict[str, object] = {}

    def release_then_look(**kwargs) -> None:
        # The window: after unregister(), before the rmtree.
        observed["get"] = registry_.get(sandbox_id)
        observed["records"] = [record.sandbox_id for record in registry_.list()]
        observed["record_file_on_disk"] = (tree / "sandbox.json").is_file()

    monkeypatch.setattr(agent_mod, "release_project", release_then_look)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app), base_url="http://control"
    ) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert summary["deleted"] == [sandbox_id]
    # The disk really did still hold the record while get() was called ...
    assert observed == {
        "get": None,
        "records": [],
        "record_file_on_disk": True,
    }
    # ... and nothing resurrected it afterwards either.
    assert tree.exists() is False
    assert registry_.get(sandbox_id) is None
    assert registry_.list() == []
