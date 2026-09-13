"""W7 contract: a teardown that did not happen is never reported as success.

Wave-1 review (C1) traced one chain through the real code: the worker refused
a rewritten record with 409, ``_destroy_remote`` never looked at the status
code, and ``kill_sandbox`` had already deleted the control-plane record -- so
the SDK saw 204 while the tree, its quota row and its process tree all stayed
behind. The refusal also covered *legitimate* records whose ``workspace_dir``
was merely spelled differently (a base reached through a symlink, a base that
moved), which ``de555f8`` deleted correctly.

The rework has two halves, and both are pinned here:

* the control plane reads the answer (204 = torn down, 409 = refused), keeps
  the record when the node refused, and only then deletes it -- a kill cannot
  end in "the record is gone and the tree is not";
* the worker compares *identities*, not spellings, so the legitimate shapes
  go back to a clean 204 (and their quota rows stop being pinned), while a
  record that aims at another tenant's tree is still refused -- and a refused
  record stops the process tree, keeps the files, and can still be reclaimed
  from the disk with an explicit ``force``.

Plus the combined node's ``_destroy_local`` (C2), which used to take its
volume targets from the sandbox-writable ``sandbox.json``.

Every assertion is an exact value: the whole point is which side of the
boundary a byte of evidence lands on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

import control_plane.api.sandboxes as sandboxes
import envd_service.agent as agent_mod
import envd_service.volumes as volumes
import envd_service.xfs_quota as xfs_quota
from control_plane.api.sandboxes import _destroy_evicted
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry

INTERNAL_KEY = "internal-key"
API_KEY = "local-key"
#: The victim's project id, borrowed from the M4 contracts next door.
VICTIM_PROJID = 1807253611
LIAR_PROJID = 7001


# ---------------------------------------------------------------------------
# Harnesses: a real control plane, and an agent that answers on command.
# ---------------------------------------------------------------------------


def _control_settings(workspace: Path, **overrides) -> ControlSettings:
    return ControlSettings(
        api_keys=(API_KEY,),
        internal_api_key=INTERNAL_KEY,
        workspace_base=workspace,
        **overrides,
    )


def _envd_settings(workspace: Path, **overrides) -> EnvdSettings:
    return EnvdSettings(
        executor="local",
        workspace_base=workspace,
        internal_api_key=INTERNAL_KEY,
        quota_via_agent=True,
        **overrides,
    )


def _register_remote_node(nodes: NodeRegistry, node_id: str, address: str) -> None:
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


def _remote_control_stack(workspace: Path, *, node_id: str = "node_a"):
    """A control plane whose only worker address answers nothing by default."""
    settings = _control_settings(workspace)
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    _register_remote_node(nodes, node_id, "http://node-a.invalid")
    registry = SandboxRegistry(settings)
    app = create_control_app(
        settings=settings,
        registry=registry,
        nodes_registry=nodes,
        workspace_base=workspace,
    )
    app.state.nodes.remove("local")
    return registry, app


class _AgentAnswer:
    """Stands in for ``httpx.AsyncClient`` and answers like a worker agent.

    ``status``/``text`` are what the agent answers, ``error`` is what it
    raises instead of answering (an unreachable node), and ``on_delete`` sees
    the world at the moment the request is made -- which is how the ordering
    contract below observes that the record is still there.

    The teardown helper builds a plain client; the test's own control-plane
    client passes a ``transport``. Only the former is faked, so the two never
    shadow each other.
    """

    def __init__(
        self,
        real_client,
        *,
        status: int = 204,
        text: str = "",
        error: Exception | None = None,
        on_delete=None,
    ) -> None:
        self._real_client = real_client
        self.status = status
        self.text = text
        self.error = error
        self.on_delete = on_delete
        self.urls: list[str] = []

    def __call__(self, **kwargs):
        if "transport" in kwargs:
            return self._real_client(**kwargs)
        return self

    async def __aenter__(self) -> "_AgentAnswer":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def delete(self, url, headers=None) -> httpx.Response:
        self.urls.append(url)
        if self.on_delete is not None:
            self.on_delete(url)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, text=self.text)


async def _delete_sandbox(app, sandbox_id: str, params: str = "") -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        return await client.delete(
            f"/sandboxes/{sandbox_id}{params}", headers={"X-API-Key": API_KEY}
        )


def _sandbox_lines(caplog) -> list[str]:
    return [
        record.message
        for record in caplog.records
        if record.name == "control_plane.api.sandboxes"
    ]


# ---------------------------------------------------------------------------
# C1-1 / C1-2: what the control plane tells the SDK, and in which order.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_remote_teardown_is_not_reported_as_success(
    workspace, monkeypatch, caplog
):
    """The worker said 409; the SDK must not see 204, and the record must stay.

    This is the exact chain review C1 measured: the agent refused (its record
    disagreed with the disk), the control plane had already deleted its own
    record, and the SDK was told the kill succeeded. Now the refusal is a
    502, the record survives (as ``orphaned``, with the reason in its log) so
    the sandbox stays visible and retryable, and the whole thing is a WARNING
    an operator can read.
    """
    registry, control_app = _remote_control_stack(workspace)
    _control_record(registry, "node_a", "sbx_refused")
    answer = _AgentAnswer(
        httpx.AsyncClient,
        status=409,
        text=(
            "refusing to tear down sbx_refused: its sandbox.json points at "
            "/elsewhere/sbx_victim"
        ),
    )
    monkeypatch.setattr(httpx, "AsyncClient", answer)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(control_app, "sbx_refused")

    assert response.status_code == 502
    assert response.json() == {
        "code": 502,
        "message": (
            "Sandbox sbx_refused teardown failed on node node_a; its runtime "
            "was stopped and its files are kept"
        ),
    }
    assert answer.urls == [
        "http://node-a.invalid/agent/sandboxes/sbx_refused"
    ]
    # The record is still there, marked, and says what happened.
    keep = registry.get("sbx_refused")
    assert keep.state == "orphaned"
    assert keep.logs[-1]["line"] == (
        "delete: the node did not confirm the teardown; runtime stopped, "
        "files kept"
    )
    assert [record.sandbox_id for record in registry.list()] == ["sbx_refused"]
    assert _sandbox_lines(caplog) == [
        "sandbox sbx_refused: node node_a refused the teardown (HTTP 409): "
        "refusing to tear down sbx_refused: its sandbox.json points at "
        "/elsewhere/sbx_victim; its runtime was stopped and its files are kept",
        "sandbox sbx_refused: teardown not confirmed; record kept as orphaned",
    ]


@pytest.mark.asyncio
async def test_the_record_is_still_there_while_the_node_is_asked(
    workspace, monkeypatch
):
    """C1-2: the record goes *after* the teardown is confirmed, not before.

    The deleted record is what the worker's reconcile compares against, so a
    record removed before the answer is the "control plane forgot it" state
    itself. The observation is taken inside the request: at that moment the
    record still exists, in its normal state.
    """
    registry, control_app = _remote_control_stack(workspace)
    _control_record(registry, "node_a", "sbx_order")
    seen: dict[str, object] = {}

    def observe(url: str) -> None:
        record = registry.get("sbx_order")
        seen["status"] = record.state
        seen["ids"] = [r.sandbox_id for r in registry.list()]

    answer = _AgentAnswer(httpx.AsyncClient, on_delete=observe)
    monkeypatch.setattr(httpx, "AsyncClient", answer)

    response = await _delete_sandbox(control_app, "sbx_order")

    assert response.status_code == 204
    assert seen == {"status": "running", "ids": ["sbx_order"]}
    with pytest.raises(Exception):
        registry.get("sbx_order")
    assert registry.list() == []


@pytest.mark.asyncio
async def test_an_unreachable_node_is_deferred_loudly_not_silently(
    workspace, monkeypatch, caplog
):
    """The one failure that is still a 204 -- and it says so.

    A node that cannot be reached is the documented E6.1 case: nothing can be
    confirmed, the record is released, and the worker's next reconcile
    reclaims the tree and its row
    (``test_kill_while_the_hosting_worker_is_down_is_reclaimed_on_the_next
    _start``). What used to be invisible is now a WARNING naming the sandbox
    and the reason.
    """
    registry, control_app = _remote_control_stack(workspace)
    _control_record(registry, "node_a", "sbx_unreachable")
    answer = _AgentAnswer(
        httpx.AsyncClient, error=httpx.ConnectError("connection refused")
    )
    monkeypatch.setattr(httpx, "AsyncClient", answer)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(control_app, "sbx_unreachable")

    assert response.status_code == 204
    assert registry.list() == []
    assert _sandbox_lines(caplog) == [
        "sandbox sbx_unreachable: node node_a did not answer its teardown "
        "request: connection refused; the tree and its runtime are left to "
        "that worker's next reconcile",
        "sandbox sbx_unreachable: teardown deferred to the worker's next "
        "reconcile (node node_a unreachable); the record is released and the "
        "worker reclaims the tree and its quota row",
    ]


@pytest.mark.asyncio
async def test_an_unknown_node_is_not_a_local_teardown(workspace, monkeypatch, caplog):
    """A node the registry does not know cannot confirm anything.

    The old code fell through to the *local* teardown in that case: it found
    nothing to remove on the control-plane host, answered 204 and dropped the
    record -- the tree on the worker stayed. The record now stays with the
    failure.
    """
    registry, control_app = _remote_control_stack(workspace)
    _control_record(registry, "node_gone", "sbx_unknown_node")
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(control_app, "sbx_unknown_node")

    assert response.status_code == 502
    assert registry.get("sbx_unknown_node").state == "orphaned"
    assert _sandbox_lines(caplog) == [
        "sandbox sbx_unknown_node: node node_gone is not in the registry; "
        "teardown cannot be confirmed",
        "sandbox sbx_unknown_node: teardown not confirmed; record kept as "
        "orphaned",
    ]


@pytest.mark.asyncio
async def test_force_is_not_something_a_tenant_key_can_turn_on(
    workspace, monkeypatch, caplog
):
    """The bounded exit is an operator action (review W7 / C1-3).

    It tears a tree down from the disk alone, so it is gated the same way the
    ownership bypass is: only a key without a tenant (admin, or a
    single-tenant deployment) may ask for it.
    """
    registry, control_app = _remote_control_stack(workspace)
    _control_record(registry, "node_a", "sbx_force_gate")
    answer = _AgentAnswer(httpx.AsyncClient)
    monkeypatch.setattr(httpx, "AsyncClient", answer)
    monkeypatch.setattr(
        sandboxes, "tenant_of", lambda request: ("tenant-1", False)
    )
    caplog.set_level(logging.WARNING)

    response = await _delete_sandbox(control_app, "sbx_force_gate", "?force=true")

    assert response.status_code == 403
    assert registry.get("sbx_force_gate").state == "running"
    assert answer.urls == []


# ---------------------------------------------------------------------------
# C1-5: identities, not spellings -- and the shapes that must still refuse.
# ---------------------------------------------------------------------------


def _write_tree(
    base: Path,
    sandbox_id: str,
    *,
    workspace_dir: str | None = None,
    project_id: int | None = None,
    volume_projects: tuple = (),
) -> Path:
    """Write ``<base>/<id>/sandbox.json`` plus a payload file."""
    tree = base / sandbox_id
    (tree / "workspace").mkdir(parents=True, exist_ok=True)
    (tree / "payload.bin").write_bytes(f"payload-of-{sandbox_id}".encode())
    record = {
        "sandbox_id": sandbox_id,
        "access_token": "tok",
        "created_at": 1_600_000_000.0,
        "workspace_dir": str(tree) if workspace_dir is None else workspace_dir,
    }
    if project_id is not None:
        record["project_id"] = project_id
    if volume_projects:
        record["volume_projects"] = list(volume_projects)
    (tree / "sandbox.json").write_text(json.dumps(record), encoding="utf-8")
    return tree


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


def _install_disk_projids(monkeypatch, mapping: dict[Path, int]) -> None:
    """Answer the project-id read from an explicit disk table.

    ``_verified_teardown_plan`` reads the truth from the filesystem, because
    the record inside the sandbox-owned tree is input the sandbox can
    rewrite. This host has no XFS to answer it, so the read is supplied here;
    the real wiring (including the mismatch refusal) still runs.
    """
    monkeypatch.setattr(
        agent_mod,
        "directory_project_id",
        lambda path: mapping.get(Path(path)),
    )


def _worker_app(workspace: Path, **settings_overrides):
    return create_envd_app(
        settings=_envd_settings(workspace, **settings_overrides),
    )


async def _worker_delete(app, sandbox_id: str, params: str = "") -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.delete(
            f"/agent/sandboxes/{sandbox_id}{params}",
            headers={"X-Internal-Key": INTERNAL_KEY},
        )


def _agent_lines(caplog) -> list[str]:
    return [
        record.message
        for record in caplog.records
        if record.name == "envd_service.agent"
    ]


@pytest.mark.asyncio
async def test_a_symlinked_base_spelling_is_the_same_tree(
    workspace, monkeypatch, caplog
):
    """C1-5 / M1: the shape ``de555f8`` deleted correctly must delete again.

    The tree is ``<real>/<id>``; the record carries the path it was written
    with, spelled through a symlink to the same real directory. That is one
    tree, not a contradiction: the delete answers 204, removes the tree and
    its record, releases the disk's project id, and fires the unregister
    callbacks that stop the process tree.
    """
    real_dir = workspace / "ws-real"
    real_dir.mkdir()
    link_dir = workspace / "ws-link"
    link_dir.symlink_to(real_dir)
    tree = _write_tree(
        real_dir,
        "sbx_symlink",
        workspace_dir=str(link_dir / "sbx_symlink"),
        project_id=LIAR_PROJID,
    )
    quota = _QuotaFake({LIAR_PROJID: 8})
    app = _worker_app(real_dir)
    # The app's own startup is what installs the degraded quota hooks when no
    # agent URL is configured, so the fake goes on top of it.
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {tree: LIAR_PROJID})
    fired: list[str] = []
    app.state.runtime_registry.add_unregister_callback(fired.append)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _worker_delete(app, "sbx_symlink")

    assert response.status_code == 204
    assert response.text == ""
    assert _agent_lines(caplog) == []
    assert fired == ["sbx_symlink"]
    assert tree.exists() is False
    assert app.state.runtime_registry.list() == []
    assert quota.released == [(str(tree), LIAR_PROJID)]
    assert quota.rows == {LIAR_PROJID: 8}


@pytest.mark.asyncio
async def test_a_stale_recorded_base_is_reclaimed_and_stops_pinning_its_row(
    workspace, monkeypatch, caplog
):
    """C1-3: the legitimate "old base" shape is reclaimed, row included.

    The record points at ``/srv/e2b-legacy/<id>`` -- a path this worker does
    not have (a base that moved, an upgraded deployment). It names no other
    tree, so the convention path is the only target: the tree goes, the disk's
    project id is released, and the row the record used to pin *forever*
    (``_recorded_projids``) is now an orphan the fail-safe reconcile cleans.
    """
    tree = _write_tree(
        workspace,
        "sbx_stale",
        workspace_dir="/srv/e2b-legacy/sbx_stale",
        project_id=LIAR_PROJID,
    )
    quota = _QuotaFake({LIAR_PROJID: 8})
    app = _worker_app(workspace)
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {tree: LIAR_PROJID})
    caplog.set_level(logging.WARNING)
    caplog.clear()
    # The pin, before: the readable record is what keeps the row out of the
    # orphan scan, no matter how it disagrees with the disk.
    assert xfs_quota._recorded_projids(workspace) == {LIAR_PROJID}

    response = await _worker_delete(app, "sbx_stale")

    assert response.status_code == 204
    assert _agent_lines(caplog) == []
    assert tree.exists() is False
    assert quota.released == [(str(tree), LIAR_PROJID)]
    # The pin, after: nothing references the row, and the fail-safe pass the
    # worker runs reclaims it (no directory left to clear, or one with no
    # usage -- the shape the reconcile is written for).
    assert xfs_quota._recorded_projids(workspace) == set()
    assert _reconcile_rows(monkeypatch, workspace, {LIAR_PROJID: 0}) == {
        "cleaned": [LIAR_PROJID],
        "skipped": [],
    }


def _reconcile_rows(monkeypatch, workspace: Path, rows: dict[int, int]) -> dict:
    """Run the real local quota reconcile over an explicit table."""
    table = {
        projid: SimpleNamespace(used_blocks=blocks)
        for projid, blocks in rows.items()
    }
    monkeypatch.setattr(xfs_quota, "project_quota_table", lambda mount: table)
    monkeypatch.setattr(xfs_quota, "_use_quotactl", lambda mount: False)
    monkeypatch.setattr(
        xfs_quota, "_local_run_xfs_quota", lambda mount, command: ""
    )
    return xfs_quota.reconcile_orphan_projects(
        workspace_base=workspace, mount_point=workspace
    )


@pytest.mark.asyncio
async def test_a_record_aiming_at_another_tree_still_refuses_but_stops_the_runtime(
    workspace, monkeypatch, caplog
):
    """C1-5 + C1-4: the attack shape is refused, and its runtime still goes.

    The record aims at another tenant's *existing* tree and claims its
    project id. Nothing of the victim's may be touched -- not its tree, not
    its row -- and the sandbox's own tree stays for an operator. What must
    not stay is the process tree: ``unregister`` runs even on a refusal, so
    "refused" can never mean "the control plane forgot it and it is still
    running".
    """
    victim = _write_tree(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    liar = _write_tree(
        workspace,
        "sbx_liar",
        workspace_dir=str(victim),
        project_id=VICTIM_PROJID,
    )
    quota = _QuotaFake({VICTIM_PROJID: 8, LIAR_PROJID: 8})
    app = _worker_app(workspace)
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {liar: LIAR_PROJID, victim: VICTIM_PROJID})
    fired: list[str] = []
    app.state.runtime_registry.add_unregister_callback(fired.append)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _worker_delete(app, "sbx_liar")

    assert response.status_code == 409
    assert response.text == (
        f"refusing to tear down sbx_liar: its sandbox.json points at {victim}"
    )
    assert _agent_lines(caplog) == [
        f"delete: refusing to tear down sbx_liar: its sandbox.json points at "
        f"{victim}"
    ]
    assert quota.released == []
    assert quota.rows == {VICTIM_PROJID: 8, LIAR_PROJID: 8}
    assert (victim / "payload.bin").read_bytes() == b"payload-of-sbx_victim"
    assert liar.is_dir()
    # The runtime is not part of what the refusal protects.
    assert fired == ["sbx_liar"]
    assert app.state.runtime_registry.list() == []


@pytest.mark.asyncio
async def test_force_reclaims_the_refused_tree_from_the_disk_alone(
    workspace, monkeypatch, caplog
):
    """C1-3: the bounded exit of a refusal, with the victim still untouched.

    ``force=true`` is the operator's "tear this tree down from the disk"
    decision. Every target still comes from the disk -- the convention path
    and the project id the filesystem reports -- so the rewritten claims are
    overruled, logged, and never acted on: the victim's tree and row survive.
    """
    victim = _write_tree(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    liar = _write_tree(
        workspace,
        "sbx_liar",
        workspace_dir=str(victim),
        project_id=VICTIM_PROJID,
    )
    quota = _QuotaFake({VICTIM_PROJID: 8, LIAR_PROJID: 8})
    app = _worker_app(workspace)
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, {liar: LIAR_PROJID, victim: VICTIM_PROJID})
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _worker_delete(app, "sbx_liar", "?force=true")

    assert response.status_code == 204
    assert _agent_lines(caplog) == [
        f"delete: forcing the teardown of sbx_liar: its sandbox.json points "
        f"at {victim}; using the convention path {liar}",
        f"delete: forcing {liar}: its sandbox.json claims project id "
        f"{VICTIM_PROJID} but the disk says {LIAR_PROJID}; releasing the "
        "project id the disk reports",
    ]
    # The sandbox's own tree and its own row are reclaimed ...
    assert liar.exists() is False
    assert quota.released == [(str(liar), LIAR_PROJID)]
    # ... and nothing of the victim's is (its row is untouched by this call).
    assert (victim / "payload.bin").read_bytes() == b"payload-of-sbx_victim"
    assert (victim / "sandbox.json").is_file()
    assert quota.rows == {VICTIM_PROJID: 8, LIAR_PROJID: 8}


# ---------------------------------------------------------------------------
# C2: the combined node's local teardown takes its slices from the disk too.
# ---------------------------------------------------------------------------


def _local_control_stack(workspace: Path, volume_root: Path):
    """A combined node: control plane + the runtime registry of the same tree."""
    settings = _control_settings(workspace, shared_volume_root=str(volume_root))
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    nodes.add_local_node(
        node_id="local",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=16384,
        total_processes=512,
    )
    registry = SandboxRegistry(settings)
    runtime_registry = RuntimeRegistry(workspace)
    app = create_control_app(
        settings=settings,
        registry=registry,
        nodes_registry=nodes,
        workspace_base=workspace,
        runtime_registry=runtime_registry,
    )
    return registry, app, runtime_registry


def _record_releases(monkeypatch) -> list[tuple[str, int]]:
    """Record every volume project release the local teardown makes."""
    released: list[tuple[str, int]] = []
    monkeypatch.setattr(
        volumes,
        "release_project",
        lambda *, project_dir, mount_point, projid, via_agent: released.append(
            (str(project_dir), int(projid))
        ),
    )
    return released


@pytest.mark.asyncio
async def test_the_combined_node_never_reaches_another_tenants_slice(
    workspace, monkeypatch, caplog
):
    """C2: a rewritten ``volume_projects`` may not aim the local teardown.

    The shape review C2 measured against the real control plane: a cold
    registry (the sandbox's own ``sandbox.json`` is the only source), an
    entry naming another tenant's slice, and one naming this sandbox outside
    the configured volume root. Neither is touched: no slice is removed and
    no project id is released. The sandbox's own tree still goes -- it is the
    one target that is verified by construction.
    """
    volume_root = workspace / "_volumes"
    victim_slice = volume_root / "vol_1" / "sbx_victim"
    victim_slice.mkdir(parents=True)
    (victim_slice / "victim.bin").write_bytes(b"another tenant's volume")
    outside_slice = workspace / "elsewhere" / "sbx_local_liar"
    outside_slice.mkdir(parents=True)
    tree = _write_tree(
        workspace,
        "sbx_local_liar",
        project_id=LIAR_PROJID,
        volume_projects=(
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_victim",
                "mount_path": "mnt/data",
                "sandbox_dir": str(victim_slice),
                "projid": VICTIM_PROJID,
            },
            {
                "volume_id": "vol_2",
                "sandbox_id": "sbx_local_liar",
                "mount_path": "mnt/other",
                "sandbox_dir": str(outside_slice),
                "projid": LIAR_PROJID,
            },
        ),
    )
    registry, app, runtime_registry = _local_control_stack(workspace, volume_root)
    released = _record_releases(monkeypatch)
    _install_disk_projids(
        monkeypatch,
        {tree: LIAR_PROJID, victim_slice: VICTIM_PROJID, outside_slice: LIAR_PROJID},
    )
    _control_record(registry, "local", "sbx_local_liar")
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(app, "sbx_local_liar")

    assert response.status_code == 204
    assert _agent_lines(caplog) == [
        f"local delete: refusing a volume entry of sbx_local_liar: "
        f"{victim_slice} is not a slice of this sandbox",
        f"local delete: refusing the volume slice {outside_slice} of "
        f"sbx_local_liar: it is outside the shared volume root {volume_root}",
    ]
    assert released == []
    assert (victim_slice / "victim.bin").read_bytes() == b"another tenant's volume"
    assert outside_slice.is_dir()
    assert tree.exists() is False
    assert runtime_registry.list() == []


@pytest.mark.asyncio
async def test_the_combined_node_still_cleans_its_own_slice(
    workspace, monkeypatch, caplog
):
    """The verified target set must not turn the normal delete into a no-op.

    One honest entry: named after this sandbox, inside the configured volume
    root, and its project id is the one the disk reports. It is released and
    removed exactly as before the rework.
    """
    volume_root = workspace / "_volumes"
    slice_dir = volume_root / "vol_1" / "sbx_local_own"
    slice_dir.mkdir(parents=True)
    (slice_dir / "data.bin").write_bytes(b"this sandbox's own volume")
    tree = _write_tree(
        workspace,
        "sbx_local_own",
        project_id=LIAR_PROJID,
        volume_projects=(
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_local_own",
                "mount_path": "mnt/data",
                "sandbox_dir": str(slice_dir),
                "projid": 7200,
            },
        ),
    )
    registry, app, runtime_registry = _local_control_stack(workspace, volume_root)
    released = _record_releases(monkeypatch)
    _install_disk_projids(monkeypatch, {tree: LIAR_PROJID, slice_dir: 7200})
    _control_record(registry, "local", "sbx_local_own")
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(app, "sbx_local_own")

    assert response.status_code == 204
    assert _agent_lines(caplog) == []
    assert released == [(str(slice_dir), 7200)]
    assert slice_dir.exists() is False
    assert tree.exists() is False
    assert runtime_registry.list() == []
