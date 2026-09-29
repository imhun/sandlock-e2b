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

from gateway_common.paths import sandbox_record_path, sandbox_runtime_dir

import control_plane.api.sandboxes as sandboxes
import envd_service.agent as agent_mod
import envd_service.priv_helpers as priv_helpers
import envd_service.volumes as volumes
import envd_service.xfs_quota as xfs_quota
from control_plane.api.sandboxes import _destroy_evicted
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.manager import UnknownSandboxError
from control_plane.registry.nodes import NodeRegistry
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from tests._disk_projids import install_disk_projids as _install_disk_projids

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
    """Write the sandbox's record plus a payload file.

    The record goes to ``<base>/_runtime/<id>/sandbox.json`` -- beside the tree
    rather than inside it. Inside the tree it was a file the sandbox itself
    could delete and rewrite; the W7 verification these tests exercise still
    runs on the record wherever it lives, but an attacker now needs the worker
    uid (or root) to rewrite it, not just the sandbox's own tree ownership.
    """
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
    record_path = sandbox_record_path(base, sandbox_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return tree


class _QuotaFake:
    """Quota-agent ops over an explicit table, recording what was released."""

    def __init__(self, rows: dict[int, int] | None = None) -> None:
        self.rows = dict(rows or {})
        self.released: list[tuple[str, int]] = []
        #: ``(mount_point, projid)`` of every limit reset (N12).
        self.cleared: list[tuple[str, int]] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(
            xfs_quota,
            "agent_ops",
            {
                "reconcile": self.reconcile,
                "release": self.release,
                "clear_limits": self.clear_limits,
                "provision": self.provision,
            },
        )

    def reconcile(self, *, workspace_base, mount_point) -> dict:
        return {"cleaned": [], "skipped": []}

    def release(self, *, project_dir, mount_point, projid) -> None:
        self.released.append((str(project_dir), int(projid)))

    def clear_limits(self, *, mount_point, projid) -> None:
        # Production's delete path resets the limits *after* removing the tree,
        # which is what makes XFS drop the quota row (N12). The fake has to model
        # it, or the warning it logs would be an artefact of the fake.
        self.cleared.append((str(mount_point), int(projid)))

    def provision(self, **kwargs):  # pragma: no cover - never used here
        raise AssertionError("provisioning must not run in these contracts")


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
    workspace, monkeypatch, caplog, disk_read_backend
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
    _install_disk_projids(
        monkeypatch, {tree: LIAR_PROJID}, backend=disk_read_backend
    )
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
    workspace, monkeypatch, caplog, disk_read_backend
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
    _install_disk_projids(
        monkeypatch, {tree: LIAR_PROJID}, backend=disk_read_backend
    )
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
    workspace, monkeypatch, caplog, disk_read_backend
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
    _install_disk_projids(
        monkeypatch,
        {liar: LIAR_PROJID, victim: VICTIM_PROJID},
        backend=disk_read_backend,
    )
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
    workspace, monkeypatch, caplog, disk_read_backend
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
    _install_disk_projids(
        monkeypatch,
        {liar: LIAR_PROJID, victim: VICTIM_PROJID},
        backend=disk_read_backend,
    )
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
    assert sandbox_record_path(workspace, "sbx_victim").is_file()
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
    workspace, monkeypatch, caplog, disk_read_backend
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
        backend=disk_read_backend,
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
    workspace, monkeypatch, caplog, disk_read_backend
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
    _install_disk_projids(
        monkeypatch,
        {tree: LIAR_PROJID, slice_dir: 7200},
        backend=disk_read_backend,
    )
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


# ---------------------------------------------------------------------------
# W9: the branches the C1/C2 review found still open, closed.
# ---------------------------------------------------------------------------


async def _worker_get(app, path: str, *, key: str | None = INTERNAL_KEY):
    headers = {} if key is None else {"X-Internal-Key": key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.get(path, headers=headers)


async def _worker_post(app, path: str, *, key: str | None = INTERNAL_KEY):
    headers = {} if key is None else {"X-Internal-Key": key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(path, headers=headers)


def _record_agent_releases(monkeypatch) -> list[tuple[str, int]]:
    """Record every project release the worker's teardown makes."""
    released: list[tuple[str, int]] = []
    monkeypatch.setattr(
        agent_mod,
        "release_project",
        lambda *, project_dir, mount_point, projid, via_agent: released.append(
            (str(project_dir), int(projid))
        ),
    )
    return released


@pytest.mark.asyncio
async def test_a_refusal_is_not_blinded_by_its_own_marker(
    workspace, monkeypatch, disk_read_backend
):
    """W7-1: the refusal used to arm the very marker that hid its record.

    ``_delete_sandbox_runtime`` unregisters the refused sandbox (the process
    tree must stop -- a refusal protects *files*), and ``unregister`` arms the
    race-B marker for ``UNREGISTER_TOMBSTONE_S`` seconds. The refusal raised
    before the teardown's ``finally: release_tombstone``, so the marker stayed:
    ``RuntimeRegistry.get()`` answered ``None``, "no record" was read as
    "nothing to verify", and the *second* DELETE of the same id answered 204
    while deleting the files the refusal had promised to keep. Now the marker
    is released with the refusal, so the second attempt is refused again.
    """
    victim = _write_tree(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    liar = _write_tree(
        workspace,
        "sbx_liar",
        workspace_dir=str(victim),
        project_id=VICTIM_PROJID,
    )
    _install_disk_projids(
        monkeypatch,
        {victim: VICTIM_PROJID, liar: LIAR_PROJID},
        backend=disk_read_backend,
    )
    app = _worker_app(workspace)
    registry = app.state.runtime_registry

    first = await _worker_delete(app, "sbx_liar")

    assert first.status_code == 409
    assert first.text == (
        f"refusing to tear down sbx_liar: its sandbox.json points at {victim}"
    )
    # the record is visible again: the marker did not outlive the refusal
    assert registry.peek("sbx_liar") is not None
    assert registry.get("sbx_liar") is not None
    assert registry._tombstoned("sbx_liar") is False

    second = await _worker_delete(app, "sbx_liar")

    assert second.status_code == 409
    assert second.text == first.text
    assert (liar / "payload.bin").read_bytes() == b"payload-of-sbx_liar"
    assert sandbox_record_path(workspace, "sbx_liar").is_file()
    assert (victim / "payload.bin").read_bytes() == b"payload-of-sbx_victim"
    assert sandbox_record_path(workspace, "sbx_victim").is_file()


@pytest.mark.asyncio
async def test_the_local_refusal_survives_the_next_delete(
    workspace, monkeypatch, disk_read_backend
):
    """W7-1 on the combined node: two 502s, and the tree is still there."""
    volume_root = workspace / "_volumes"
    victim = _write_tree(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    liar = _write_tree(
        workspace,
        "sbx_liar",
        workspace_dir=str(victim),
        project_id=VICTIM_PROJID,
    )
    registry, app, runtime_registry = _local_control_stack(workspace, volume_root)
    released = _record_releases(monkeypatch)
    _install_disk_projids(
        monkeypatch,
        {victim: VICTIM_PROJID, liar: LIAR_PROJID},
        backend=disk_read_backend,
    )
    _control_record(registry, "local", "sbx_liar")

    first = await _delete_sandbox(app, "sbx_liar")
    assert first.status_code == 502
    assert registry.get("sbx_liar").state == "orphaned"
    # the marker the refusal armed is gone, so the record is readable again
    assert runtime_registry._tombstoned("sbx_liar") is False

    second = await _delete_sandbox(app, "sbx_liar")

    assert second.status_code == 502
    assert registry.get("sbx_liar").state == "orphaned"
    assert liar.is_dir()
    assert (liar / "payload.bin").read_bytes() == b"payload-of-sbx_liar"
    assert (victim / "payload.bin").read_bytes() == b"payload-of-sbx_victim"
    assert released == []


@pytest.mark.asyncio
async def test_the_local_teardown_confirms_the_tree_is_gone(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """W7-2: a tree that survives its removal is a failed teardown.

    ``_destroy_local`` used to call ``shutil.rmtree(..., ignore_errors=True)``
    and return ``acknowledged=True`` unconditionally, so the combined node
    answered 204, the control plane dropped the record, and the leftovers -- a
    tree with no readable record -- were left for a GC that can never reclaim
    them. The removal now goes through the worker's helper (the ``e2b-maint``
    fallback included) and the answer is the disk's.
    """
    volume_root = workspace / "_volumes"
    tree = _write_tree(workspace, "sbx_sealed", project_id=LIAR_PROJID)
    registry, app, runtime_registry = _local_control_stack(workspace, volume_root)
    _record_releases(monkeypatch)
    _install_disk_projids(
        monkeypatch, {tree: LIAR_PROJID}, backend=disk_read_backend
    )
    _control_record(registry, "local", "sbx_sealed")
    calls: list[str] = []

    def deny(path, *, on_error="ignore") -> None:
        calls.append(str(path))
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(priv_helpers, "remove_tree", deny)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(app, "sbx_sealed")

    assert response.status_code == 502
    # the helper is what runs -- not a bare ``shutil.rmtree``
    assert calls == [str(tree)]
    assert _sandbox_lines(caplog) == [
        "local delete: sbx_sealed could not be removed in-process or through "
        f"the broker: [Errno 13] Permission denied: '{tree}'",
        "sandbox sbx_sealed: teardown not confirmed; record kept as orphaned",
    ]
    assert tree.is_dir()
    assert (tree / "payload.bin").read_bytes() == b"payload-of-sbx_sealed"
    # the record survives: the sandbox is still there, so 204 would be a lie
    assert registry.get("sbx_sealed").state == "orphaned"
    assert runtime_registry._tombstoned("sbx_sealed") is False


@pytest.mark.asyncio
async def test_the_local_teardown_passes_when_the_helper_removes_the_tree(
    workspace, monkeypatch, disk_read_backend
):
    """The other half of W7-2: a removal that worked is still a clean 204."""
    import shutil

    volume_root = workspace / "_volumes"
    tree = _write_tree(workspace, "sbx_ok", project_id=LIAR_PROJID)
    registry, app, runtime_registry = _local_control_stack(workspace, volume_root)
    _record_releases(monkeypatch)
    _install_disk_projids(
        monkeypatch, {tree: LIAR_PROJID}, backend=disk_read_backend
    )
    _control_record(registry, "local", "sbx_ok")
    calls: list[str] = []

    def remove(path, *, on_error="ignore") -> None:
        calls.append(str(path))
        shutil.rmtree(path)

    monkeypatch.setattr(priv_helpers, "remove_tree", remove)

    response = await _delete_sandbox(app, "sbx_ok")

    assert response.status_code == 204
    # Two calls, not one (C3 Task 4 / A5): the tree *and* its paired platform
    # directory (``<state base>/_runtime/<id>``) go through the same confirming
    # removal. That directory is `0700` owned by the worker, so a control plane
    # that is neither root nor its owner used to fail the generic
    # ``rmtree(..., ignore_errors=True)`` silently and still report a teardown
    # (docs/c3-privilege-relocation.md §13.7, reproduced by
    # ``deploy/scripts/acceptance/probe_c3_a5_silent_rmtree.py``).
    assert calls == [
        str(tree),
        str(sandbox_runtime_dir(workspace, "sbx_ok")),
    ]
    assert tree.exists() is False
    assert runtime_registry.list() == []
    with pytest.raises(UnknownSandboxError):
        registry.get("sbx_ok")


@pytest.mark.asyncio
async def test_a_refused_tree_is_parked_and_the_row_it_pinned_is_released(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """W7-3: the GC's refusal has a bounded, non-destructive exit.

    Once the control plane has released the record (eviction, TTL, a kill
    while the worker was unreachable) the API's ``force`` can only answer 404
    -- the record is what named the node. Left in place, the tree's own
    ``sandbox.json`` keeps the project ids it claims in the fail-safe scan
    ("still live"), so the row never leaves the table: a permanent pin. The
    exit moves the tree out of the sandbox namespace (never deletes it) and
    releases the project id the disk reports.
    """
    victim = _write_tree(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    liar = _write_tree(
        workspace,
        "sbx_liar",
        workspace_dir=str(victim),
        project_id=LIAR_PROJID,
    )
    _install_disk_projids(
        monkeypatch,
        {victim: VICTIM_PROJID, liar: LIAR_PROJID},
        backend=disk_read_backend,
    )
    released = _record_agent_releases(monkeypatch)
    app = _worker_app(workspace)
    parked_dir = workspace / "_untrusted.trees" / "sbx_liar"
    caplog.set_level(logging.WARNING)
    caplog.clear()

    # 1. the worker refuses to tear the tree down from its record ...
    refused = await _worker_delete(app, "sbx_liar")
    assert refused.status_code == 409
    assert _agent_lines(caplog) == [
        f"delete: refusing to tear down sbx_liar: its sandbox.json points at "
        f"{victim}",
    ]

    # 2. ... the audit view names it, with the reason and the ids involved ...
    caplog.clear()
    listed = await _worker_get(app, "/agent/untrusted")
    assert listed.status_code == 200
    assert listed.json() == {
        "untrusted": [
            {
                "sandbox_id": "sbx_liar",
                "reason": f"its sandbox.json points at {victim}",
                "disk_project_id": LIAR_PROJID,
                "claimed_project_ids": [
                    LIAR_PROJID,
                ],
            }
        ]
    }

    # 3. ... and the bounded exit parks the tree (moved, never deleted)
    monkeypatch.setattr(
        agent_mod, "time", SimpleNamespace(time=lambda: 1_700_000_000.0)
    )
    parked = await _worker_post(app, "/agent/untrusted/sbx_liar/park")
    assert parked.status_code == 200
    assert parked.json() == {
        "sandbox_id": "sbx_liar",
        "reason": f"its sandbox.json points at {victim}",
        "parked_at": "_untrusted.trees/sbx_liar",
        "released_project_id": LIAR_PROJID,
    }
    assert _agent_lines(caplog) == [
        "park: moved the refused tree sbx_liar to _untrusted.trees (payload "
        f"kept, project {LIAR_PROJID} released)",
    ]
    assert liar.exists() is False
    assert parked_dir.is_dir()
    assert (parked_dir / "payload.bin").read_bytes() == b"payload-of-sbx_liar"
    assert (parked_dir / "sandbox.json").is_file()
    assert (workspace / "_untrusted.trees" / "sbx_liar.reason").read_text(
        encoding="utf-8"
    ) == (
        "1700000000\tsbx_liar\t"
        f"its sandbox.json points at {victim}\n"
    )
    assert released == [(str(parked_dir), LIAR_PROJID)]
    # the victim is untouched, and the row the liar's record pinned is no
    # longer in the fail-safe scan (its own row, and the victim's, stay live)
    assert (victim / "payload.bin").read_bytes() == b"payload-of-sbx_victim"
    assert xfs_quota._recorded_projids(workspace) == {VICTIM_PROJID}

    # 4. nothing is parked twice, and a *healthy* tree is never parked
    caplog.clear()
    again = await _worker_post(app, "/agent/untrusted/sbx_liar/park")
    assert again.status_code == 404
    healthy = _write_tree(workspace, "sbx_healthy", project_id=7200)
    _install_disk_projids(
        monkeypatch,
        {victim: VICTIM_PROJID, liar: LIAR_PROJID, healthy: 7200},
    )
    healthy_park = await _worker_post(app, "/agent/untrusted/sbx_healthy/park")
    assert healthy_park.status_code == 404
    assert healthy.is_dir()
    assert _agent_lines(caplog) == []


@pytest.mark.asyncio
async def test_the_untrusted_view_and_the_park_action_need_the_internal_key(
    workspace, monkeypatch
):
    """Both surfaces are operator-only, like every other agent route."""
    _write_tree(
        workspace, "sbx_liar", workspace_dir=str(workspace / "sbx_victim")
    )
    app = _worker_app(workspace)

    assert (await _worker_get(app, "/agent/untrusted", key=None)).status_code == 401
    assert (
        await _worker_post(app, "/agent/untrusted/sbx_liar/park", key=None)
    ).status_code == 401


@pytest.mark.asyncio
async def test_a_tree_without_a_readable_record_is_listed_and_parkable(
    workspace, monkeypatch, disk_read_backend
):
    """The other leftover shape: no readable ``sandbox.json`` at all.

    The GC reports those as ``unmaterialised`` and never tears them down (with
    no record there is no project id to read, so deleting would be all risk and
    no reclaim) -- which also meant they had no reclaim path of their own. The
    audit view names them and the same bounded exit moves them out of the
    sandbox namespace, keeping the payload and releasing the project id the
    disk reports for the directory itself.
    """
    leftover = workspace / "sbx_no_record"
    (leftover / "workspace").mkdir(parents=True)
    (leftover / "payload.bin").write_bytes(b"payload-of-sbx_no_record")
    _install_disk_projids(
        monkeypatch, {leftover: 7100}, backend=disk_read_backend
    )
    released = _record_agent_releases(monkeypatch)
    app = _worker_app(workspace)
    parked_dir = workspace / "_untrusted.trees" / "sbx_no_record"

    listed = await _worker_get(app, "/agent/untrusted")

    assert listed.status_code == 200
    assert listed.json() == {
        "untrusted": [
            {
                "sandbox_id": "sbx_no_record",
                "reason": "it has no readable sandbox.json",
                "disk_project_id": 7100,
                "claimed_project_ids": [],
            }
        ]
    }

    parked = await _worker_post(app, "/agent/untrusted/sbx_no_record/park")

    assert parked.status_code == 200
    assert parked.json() == {
        "sandbox_id": "sbx_no_record",
        "reason": "it has no readable sandbox.json",
        "parked_at": "_untrusted.trees/sbx_no_record",
        "released_project_id": 7100,
    }
    assert leftover.exists() is False
    assert (parked_dir / "payload.bin").read_bytes() == b"payload-of-sbx_no_record"
    assert released == [(str(parked_dir), 7100)]


@pytest.mark.asyncio
async def test_a_slice_that_is_a_link_to_another_tenants_slice_is_refused(
    workspace, monkeypatch, caplog
):
    """W7-5: the slice check is an identity check, not a spelling check.

    A link *inside* the shared volume root, spelled after this sandbox, points
    at another tenant's slice: the name check passes, the containment check
    resolves the link to a path that is still inside the root, and the project
    id read through it is the victim's -- so the pre-fix code adopted the
    entry and released the victim's row (and, with a broker, ``e2b-maint``
    would have deleted the victim's slice). ``lstat`` describes the entry and
    ``stat`` the directory a destructive call would open; they disagree for a
    link, so the entry is refused.
    """
    volume_root = workspace / "_volumes"
    victim_slice = volume_root / "vol_1" / "sbx_victim"
    victim_slice.mkdir(parents=True)
    (victim_slice / "victim.bin").write_bytes(b"another tenant's volume")
    link = volume_root / "vol_1" / "sbx_linked"
    link.symlink_to(victim_slice)
    tree = _write_tree(
        workspace,
        "sbx_linked",
        project_id=LIAR_PROJID,
        volume_projects=(
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_linked",
                "mount_path": "mnt/data",
                "sandbox_dir": str(link),
            },
        ),
    )
    registry, app, runtime_registry = _local_control_stack(workspace, volume_root)
    released = _record_releases(monkeypatch)
    # The kernel's project-id read follows the link, so the pre-fix code read
    # (and released) the *victim's* row through it; that is what this test's
    # `released == []` pins.
    monkeypatch.setattr(
        agent_mod,
        "directory_project_id",
        lambda path: {
            str(victim_slice): VICTIM_PROJID,
            str(tree): LIAR_PROJID,
        }.get(str(Path(path).resolve())),
    )
    _control_record(registry, "local", "sbx_linked")
    caplog.set_level(logging.WARNING)
    caplog.clear()

    response = await _delete_sandbox(app, "sbx_linked")

    assert response.status_code == 204
    assert _agent_lines(caplog) == [
        f"local delete: refusing the volume slice {link} of sbx_linked: the "
        "entry is not the directory it spells (a link or a non-directory), "
        "so it could name another tenant's slice",
    ]
    assert released == []
    assert (victim_slice / "victim.bin").read_bytes() == b"another tenant's volume"
    assert link.is_symlink()
    assert tree.exists() is False
    assert runtime_registry.list() == []


# ---------------------------------------------------------------------------
# R1/R2 (final review of W9, `.superpowers/sdd/task-w9-review.md` §4.2/§9):
# the two "permanent leftover" shapes.
# ---------------------------------------------------------------------------

#: The project id the *victim's* volume slice carries on the disk.
VICTIM_SLICE_PROJID = 1876543211


@pytest.mark.asyncio
async def test_a_prefixed_leftover_that_carries_a_project_id_is_visible_and_parkable(
    workspace, monkeypatch
):
    """R1: the name is not the evidence -- the disk is.

    ``snap_leftover`` is a legal sandbox id (``X-Sandbox-Id`` goes through
    ``validate_sandbox_id`` alone, and ``_``/``snap_`` are legal id
    characters) whose tree lost its record. The shape rule leaves it out
    (infrastructure prefix, no top-level ``sandbox.json``), so before this
    round *every* worker-side surface missed it: not in the audit listing, not
    parkable, not in ``unmaterialised`` -- while the project id the disk
    reports for it kept its quota row unscannable forever. Its tree and its
    row pinned each other with no entry anywhere.

    The disk decides instead. A directory that really carries a project id is
    this worker's quota asset, whatever it is called; the snapshot store (a
    top-level ``snapshot.json`` + ``fs/`` with no project id of its own) and
    the platform namespaces carry none and stay exactly where they are.
    """
    leftover = workspace / "snap_leftover"
    (leftover / "sealed").mkdir(parents=True)
    (leftover / "payload.bin").write_bytes(b"leftover payload")
    (workspace / "_snapshots").mkdir()
    store = workspace / "snap_0040ce7e44f6365f"
    (store / "fs").mkdir(parents=True)
    (store / "snapshot.json").write_text("{}", encoding="utf-8")
    reads: list[str] = []

    def fake_read(path):
        reads.append(str(path))
        return 9100 if Path(path) == leftover else None

    monkeypatch.setattr(agent_mod, "directory_project_id", fake_read)
    released = _record_agent_releases(monkeypatch)
    app = _worker_app(workspace)
    parked_dir = workspace / "_untrusted.trees" / "snap_leftover"
    reason = (
        "it has no readable sandbox.json and its name is outside the sandbox "
        "shapes this worker acts on; the disk reports project id 9100 for it"
    )

    listed = await _worker_get(app, "/agent/untrusted")

    assert listed.status_code == 200
    assert listed.json() == {
        "untrusted": [
            {
                "sandbox_id": "snap_leftover",
                "reason": reason,
                "disk_project_id": 9100,
                "claimed_project_ids": [],
            }
        ]
    }

    parked = await _worker_post(app, "/agent/untrusted/snap_leftover/park")

    assert parked.status_code == 200
    assert parked.json() == {
        "sandbox_id": "snap_leftover",
        "reason": reason,
        "parked_at": "_untrusted.trees/snap_leftover",
        "released_project_id": 9100,
    }
    assert leftover.exists() is False
    assert (parked_dir / "payload.bin").read_bytes() == b"leftover payload"
    assert (parked_dir / "sealed").is_dir()
    assert released == [(str(parked_dir), 9100)]
    assert (store / "snapshot.json").read_text(encoding="utf-8") == "{}"
    assert (store / "fs").is_dir()
    assert (workspace / "_snapshots").is_dir()
    # One read on the store -- the same asset check asks about it and its
    # answer, "no project id", is what keeps it out -- and three on the
    # leftover (the listing's check, the park route's re-check, and the
    # project id park releases). The platform namespace is never read at all:
    # the reservation short-circuits it before the disk is consulted.
    assert reads == [
        str(store),
        str(leftover),
        str(leftover),
        str(leftover),
    ]


@pytest.mark.asyncio
async def test_a_platform_namespace_is_never_listed_even_when_it_reports_an_id(
    workspace, monkeypatch, caplog
):
    """The guard on the disk-truth rule: namespaces are not parkable assets.

    Nothing assigns a project id to ``_volumes`` / ``_snapshots`` (only
    sandbox trees and volume slices are provisioned), so in a healthy
    deployment the disk read already separates them. A base that carried
    ``PROJINHERIT`` would be the one way for them to report one -- and parking
    a namespace would move every tenant's data in it off the workspace. The
    reservation is therefore explicit; it never widens, because a *sandbox
    tree* whose client-chosen id is spelled like a namespace still carries its
    own record and is still a sandbox tree.
    """
    (workspace / "_volumes").mkdir()
    _write_tree(workspace, "_snapshots", project_id=7300)
    reads: list[str] = []

    def fake_read(path):
        reads.append(str(path))
        return 7300

    monkeypatch.setattr(agent_mod, "directory_project_id", fake_read)
    app = _worker_app(workspace)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    listed = await _worker_get(app, "/agent/untrusted")

    assert listed.status_code == 200
    assert listed.json() == {"untrusted": []}
    # ``_snapshots`` carries its own record, so it is a (healthy) sandbox tree
    # and its own plan check reads it once; ``_volumes`` is never read, which
    # is what the reservation buys -- the fake would report 7300 for it.
    assert reads == [str(workspace / "_snapshots")]
    assert (
        await _worker_post(app, "/agent/untrusted/_volumes/park")
    ).status_code == 404
    assert (workspace / "_volumes").is_dir()
    assert sandbox_record_path(workspace, "_snapshots").is_file()
    assert _agent_lines(caplog) == []


@pytest.mark.asyncio
async def test_parking_a_refused_tree_releases_the_slices_it_claims(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """R2: park releases the sandbox's validated volume rows too.

    A refused record can *claim* per-volume projects as well, and those claims
    kept their rows "recorded" for exactly as long as the tree stayed in the
    workspace scan -- after which the tree is parked, the record sits inside
    the parked tree, and no scan could reach those rows again. Park therefore
    releases them, and it does so the only way this codebase releases
    anything: the slice has to pass the teardown's own guards (named after
    this sandbox, inside the configured volume root, and *being* the directory
    it spells), and the project id released is the one the disk reports for
    that slice. Two entries are refused outright and one claim is overruled;
    the row the victim's slice carries is never touched.
    """
    volume_root = workspace / "_volumes"
    victim_tree = _write_tree(workspace, "sbx_victim", project_id=VICTIM_PROJID)
    own_slice = volume_root / "vol_1" / "sbx_liar"
    own_slice.mkdir(parents=True)
    (own_slice / "data.bin").write_bytes(b"this sandbox's own slice")
    victim_slice = volume_root / "vol_1" / "sbx_victim"
    victim_slice.mkdir(parents=True)
    (victim_slice / "victim.bin").write_bytes(b"another tenant's volume")
    outside_slice = workspace / "elsewhere" / "sbx_liar"
    outside_slice.mkdir(parents=True)
    liar = _write_tree(
        workspace,
        "sbx_liar",
        workspace_dir=str(victim_tree),
        project_id=LIAR_PROJID,
        volume_projects=(
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_liar",
                "mount_path": "mnt/data",
                "sandbox_dir": str(own_slice),
                # The record *claims* 8200; the disk reports 8300.
                "projid": 8200,
            },
            {
                "volume_id": "vol_1",
                "sandbox_id": "sbx_victim",
                "mount_path": "mnt/victim",
                "sandbox_dir": str(victim_slice),
                "projid": VICTIM_SLICE_PROJID,
            },
            {
                "volume_id": "vol_2",
                "sandbox_id": "sbx_liar",
                "mount_path": "mnt/other",
                "sandbox_dir": str(outside_slice),
                "projid": 8400,
            },
        ),
    )
    _install_disk_projids(
        monkeypatch,
        {
            victim_tree: VICTIM_PROJID,
            liar: LIAR_PROJID,
            own_slice: 8300,
            victim_slice: VICTIM_SLICE_PROJID,
            outside_slice: 8400,
        },
        backend=disk_read_backend,
    )
    released = _record_agent_releases(monkeypatch)
    app = _worker_app(workspace, shared_volume_root=str(volume_root))
    parked_dir = workspace / "_untrusted.trees" / "sbx_liar"
    caplog.set_level(logging.WARNING)
    caplog.clear()

    parked = await _worker_post(app, "/agent/untrusted/sbx_liar/park")

    assert parked.status_code == 200
    assert parked.json() == {
        "sandbox_id": "sbx_liar",
        "reason": f"its sandbox.json points at {victim_tree}",
        "parked_at": "_untrusted.trees/sbx_liar",
        "released_project_id": LIAR_PROJID,
    }
    # The parked tree's own row, then the slice's *disk* id -- never the
    # claimed 8200, never the victim's row, never the out-of-root row.
    assert released == [
        (str(parked_dir), LIAR_PROJID),
        (str(own_slice), 8300),
    ]
    assert _agent_lines(caplog) == [
        f"park: forcing {own_slice}: its sandbox.json claims project id 8200 "
        "but the disk says 8300; releasing the project id the disk reports",
        f"park: refusing a volume entry of sbx_liar: {victim_slice} is not a "
        "slice of this sandbox",
        f"park: refusing the volume slice {outside_slice} of sbx_liar: it is "
        f"outside the shared volume root {volume_root}",
        f"park: sbx_liar: released the project id 8300 its volume slice "
        f"{own_slice} carries on the disk",
        "park: moved the refused tree sbx_liar to _untrusted.trees (payload "
        f"kept, project {LIAR_PROJID} released)",
    ]
    assert (own_slice / "data.bin").read_bytes() == b"this sandbox's own slice"
    assert victim_slice.is_dir()
    assert (victim_slice / "victim.bin").read_bytes() == b"another tenant's volume"
    assert outside_slice.is_dir()
