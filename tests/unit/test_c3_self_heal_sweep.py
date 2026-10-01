"""C3 Task 6: 自愈改走 agent —— agent 巡检 → CP 决策 → agent 执行.

The worker's own orphan sweep is *off* in the agent shape (Task 4's named
warning), because reclaiming a tree needs ``chown --worker`` -- the privilege
escalation C3 §14.3 measured. This file pins the replacement: the **agent** is
the eyes (it sees the whole shared mount from its own node, without any worker
being alive), the **control plane** is the brain (the only holder of the
authoritative records), and the agent is the executor again.

Three cases the brief names, one test each:

* a worker that crashes and never restarts -- the disk still converges;
* a rolling control-plane restart during the sweep -- no live sandbox is lost;
* stale records -- the whole sweep defers (``protected_elsewhere``'s semantics,
  redone where the authority now lives), with each leg of the gate pinned.

Every hop is asserted by its *exact* words: what the agent sees, what it
reports, what the control plane decides, what it instructs, and what a refusal
says when a hop cannot be made. A sweep that deletes one tree too many destroys
a live sandbox, so "nothing was deleted" is asserted as precisely as "this was
deleted".
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.c3_agent_client import (
    AgentTarget,
    C3AgentClient,
    ComposeAgentAddressResolver,
)
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.worker_identity_source import StaticWorkerIdentitySource
from c3_agent.app import create_app as create_agent_app
from c3_agent.config import Settings as AgentSettings
from c3_agent.fileops import AgentFileOpRefusal
from c3_agent.scan import (
    HttpInventoryReporter,
    InventoryScanner,
    ScanSchedule,
)

#: The agent's **own** identity: the host it runs on (D12). In k8s that is
#: ``spec.nodeName``; the worker's identity (its pod name) is a different fact,
#: and the fixtures below deliberately contain no worker at all -- the sweep
#: must not need one (that is what (e) buys over (b1)).
HOST = "k0s-node-1"
AGENT_IP = "10.44.0.7"
AGENT_A_URL = f"http://{AGENT_IP}:49985"
AGENT_B_URL = f"http://{AGENT_IP}:49986"
AGENT_TOKEN = "agent-token"
FLEET_KEY = "fleet-key"
LIVE = "sbx_live"
ORPHAN = "sbx_orphan"
CP_URL = "http://control-plane:3000"
MAINT = "/var/lib/e2b-priv/e2b-maint"
WORKER = "worker-1"
WORKER_ENDPOINT = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")


class _SharedStore:
    """The record store the control plane shares with its replicas (Redis).

    Only the slice of the interface ``SandboxRegistry`` uses, so a lane without
    ``fakeredis`` can still build a *shared-store* control plane -- which is what
    the staleness gate's leg (a) turns on. ``payload=None`` is a record the
    store holds but cannot answer (§11.2.1's "unknown ≠ empty"), i.e. leg (b).
    """

    def __init__(self, payloads: dict[str, dict | None] | None = None) -> None:
        self.payloads: dict[str, dict | None] = dict(payloads or {})
        self.tombstones: set[str] = set()

    def keys(self) -> list[str]:
        return sorted(self.payloads)

    def get(self, record_id: str) -> dict | None:
        return self.payloads.get(record_id)

    def put(self, record_id: str, payload: dict, ttl: int | None = None) -> None:
        self.payloads[record_id] = payload

    def delete(self, record_id: str) -> None:
        self.payloads.pop(record_id, None)

    def record_key(self, record_id: str) -> str:
        return f"e2b:record:{record_id}"

    def is_tombstoned(self, record_id: str) -> bool:
        return record_id in self.tombstones


class _RecordingMaintRunner:
    """Stand-in for ``e2b-maint``: records the exact argv+env, really removes.

    The *path discipline* is ``priv_common.c``'s and is pinned by its own
    layers; what this file has to prove is that the control plane derived the
    path (never a worker, never the agent) and that the agent executed exactly
    that instruction. So the runner performs the removal on the fixture tree and
    keeps the argv for the assertion.
    """

    def __init__(self, *, refuse: str | None = None) -> None:
        self.calls: list[dict] = []
        self.refuse = refuse

    def run(self, argv: list[str], *, env) -> str:
        self.calls.append({"argv": list(argv), "env": dict(env)})
        if self.refuse is not None:
            raise AgentFileOpRefusal(self.refuse)
        path = Path(argv[argv.index("--path") + 1])
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        return ""


class _StaticAgentResolver:
    """A node→agent table keyed by the **agent's own** node identity (D12)."""

    def __init__(self, targets: dict[str, AgentTarget]) -> None:
        self._targets = dict(targets)

    def resolve(self, node_id: str) -> AgentTarget | None:
        return self._targets.get(node_id)

    def resolve_host(self, node_identity: str) -> AgentTarget | None:
        target = self._targets.get(node_identity)
        if target is None or target.node_identity != node_identity:
            return None
        return target


class _ControlPlane:
    """One control plane, its shared store, and the agent it may instruct."""

    def __init__(self, root: Path, *, store: _SharedStore | None = None) -> None:
        self.root = root
        self.workspace_base = root / "workspaces"
        self.state_base = root / "state"
        self.image_cache = root / "_images"
        for path in (self.workspace_base, self.state_base, self.image_cache):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = ControlSettings(
            api_keys=("local-key",),
            internal_api_key=FLEET_KEY,
            internal_api_keys=(),
            c3_agent_token=AGENT_TOKEN,
            workspace_base=self.workspace_base,
            state_base=self.state_base,
            image_cache_dir=self.image_cache,
            shared_volume_root=str(root),
            max_sandboxes=200,
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        )
        self.store = store
        self.registry = (
            SandboxRegistry(self.settings)
            if store is None
            else SandboxRegistry(self.settings, record_store=store)
        )
        self.maint = _RecordingMaintRunner()
        self.agent = create_agent_app(
            settings=AgentSettings(
                token=AGENT_TOKEN,
                node_id=HOST,
                maint_path=MAINT,
                workspace_base=str(self.workspace_base),
                state_base=str(self.state_base),
            ),
            maint_runner=self.maint,
        )
        self.client = C3AgentClient(
            resolver=_StaticAgentResolver(
                {
                    HOST: AgentTarget(
                        node_identity=HOST,
                        url=AGENT_A_URL,
                        maint_url=AGENT_B_URL,
                        source_ips=(AGENT_IP,),
                    )
                }
            ),
            token=AGENT_TOKEN,
            timeout_s=5.0,
            file_op_timeout_s=30.0,
            transport=httpx.ASGITransport(app=self.agent),
        )
        self.app = create_control_app(
            settings=self.settings,
            registry=self.registry,
            nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
            c3_agent_client=self.client,
            node_address_resolver=StaticAddressResolver({WORKER: WORKER_ENDPOINT}),
            worker_identity_source=StaticWorkerIdentitySource({}),
        )

    # -- fixtures -----------------------------------------------------------

    def tree(self, sandbox_id: str) -> Path:
        path = self.workspace_base / sandbox_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "sandbox.json").write_text("{}", encoding="utf-8")
        return path

    def live_record(self, sandbox_id: str = LIVE) -> None:
        record = self.registry.create(
            template_id="base",
            sandbox_id=sandbox_id,
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
        )
        record.node_id = "worker-1"
        record.host_uid = 10007
        self.registry.save(record)

    # -- driving ------------------------------------------------------------

    def client_for(self, *, source_ip: str = AGENT_IP):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(source_ip, 44444)),
            base_url=CP_URL,
        )

    async def report(
        self, body, *, source_ip: str = AGENT_IP, key: str | None = AGENT_TOKEN
    ):
        async with self.client_for(source_ip=source_ip) as client:
            headers = {} if key is None else {"X-Internal-Key": key}
            return await client.post(
                f"/internal/nodes/{HOST}/agent/inventory",
                headers=headers,
                json=body,
            )

    def scanner(self, **overrides) -> InventoryScanner:
        fields = dict(
            token=AGENT_TOKEN,
            node_id=HOST,
            control_plane_url=CP_URL,
            scan_enabled=True,
            maint_path=MAINT,
            workspace_base=str(self.workspace_base),
            state_base=str(self.state_base),
        )
        fields.update(overrides)
        settings = AgentSettings(**fields)
        return InventoryScanner(
            settings=settings,
            reporter=HttpInventoryReporter(
                url=CP_URL,
                node_id=HOST,
                token=AGENT_TOKEN,
                timeout_s=10.0,
                transport=httpx.ASGITransport(
                    app=self.app, client=(AGENT_IP, 44444)
                ),
            ),
        )


def _shape(root: Path, *, store: _SharedStore | None = None) -> _ControlPlane:
    return _ControlPlane(root, store=store if store is not None else _SharedStore())


# --------------------------------------------------------------- case ①


@pytest.mark.asyncio
async def test_a_worker_that_never_restarts_still_converges(workspace: Path) -> None:
    """A tree no record anywhere claims goes, with no worker in the picture."""
    cp = _shape(workspace)
    cp.live_record()
    live_tree = cp.tree(LIVE)
    orphan_tree = cp.tree(ORPHAN)

    # No worker registered, no worker pod, no worker heartbeat: the sweep must
    # not depend on one being alive (the whole point of (e) over (b1)). The only
    # node row is the control plane's own in-process ``local`` one.
    assert [node.node_id for node in cp.app.state.nodes.list()] == ["local"]

    round_ = await cp.scanner().round()
    assert round_.scanned == (LIVE, ORPHAN)
    assert round_.answer == {
        "node": HOST,
        "scanned": 2,
        "protected": [LIVE],
        "orphans": [ORPHAN],
        "removed": [ORPHAN],
        "failed": [],
        "deferred": None,
    }
    assert orphan_tree.exists() is False
    assert live_tree.is_dir() is True
    # The instruction the agent executed is the control plane's own derivation
    # of ``<workspace base>/<id>`` -- and it carries **no** worker identity,
    # because the removal acts as nobody (that identity is exactly what the
    # sweep takes away from the worker).
    assert cp.maint.calls == [
        {
            "argv": [MAINT, "rm", "--path", str(cp.workspace_base / ORPHAN)],
            "env": {
                "E2B_UID_POOL_START": "10000",
                "E2B_UID_POOL_SIZE": "1000",
                "E2B_WORKSPACE_BASE": str(cp.workspace_base),
                "E2B_STATE_BASE": str(cp.state_base),
            },
        }
    ]


@pytest.mark.asyncio
async def test_a_removal_the_agent_refuses_is_reported_never_swallowed(
    workspace: Path,
) -> None:
    """A second agent on the shared mount meets an already-removed tree.

    Every agent sees every tree, so a loser of that race gets ``e2b-maint``'s
    refusal. Fail-closed: the control plane reports it as a failure and never
    claims a removal that did not happen.
    """
    cp = _shape(workspace)
    refusal = (
        "e2b-maint rm refused (exit 77): removing /x failed: "
        "No such file or directory"
    )
    cp.maint.refuse = refusal
    cp.tree(ORPHAN)
    round_ = await cp.scanner().round()
    assert round_.answer == {
        "node": HOST,
        "scanned": 1,
        "protected": [],
        "orphans": [ORPHAN],
        "removed": [],
        "failed": [
            {
                "sandboxID": ORPHAN,
                "reason": f"the agent for node {HOST} refused the rm: {refusal}",
            }
        ],
        "deferred": None,
    }


# --------------------------------------------------------------- case ②


@pytest.mark.asyncio
async def test_a_rolling_control_plane_restart_does_not_delete_a_live_sandbox(
    workspace: Path,
) -> None:
    """The records live in the shared store, so a fresh replica still sees them."""
    store = _SharedStore()
    first = _shape(workspace, store=store)
    first.live_record()
    live_tree = first.tree(LIVE)
    first.tree(ORPHAN)
    assert (await first.scanner().round()).answer["removed"] == [ORPHAN]
    assert (first.workspace_base / ORPHAN).exists() is False

    # The restart: a brand-new process, the same shared store, and a sandbox
    # created while it was down. Both recorded trees survive.
    second = _ControlPlane(workspace, store=store)
    restarted_tree = second.tree("sbx_created_during_restart")
    second.live_record("sbx_created_during_restart")
    round_ = await second.scanner().round()
    assert round_.answer == {
        "node": HOST,
        "scanned": 2,
        "protected": ["sbx_created_during_restart", LIVE],
        "orphans": [],
        "removed": [],
        "failed": [],
        "deferred": None,
    }
    assert restarted_tree.is_dir() is True
    assert live_tree.is_dir() is True
    assert second.maint.calls == []


@pytest.mark.asyncio
async def test_a_restart_that_cannot_see_the_records_defers_the_whole_sweep(
    workspace: Path,
) -> None:
    """Leg (a): a process-local record set cannot certify "no record anywhere".

    This is the catastrophic shape: a control plane whose records are its own
    memory, restarted, would see *every* live tree as ownerless. It has to defer
    -- named -- rather than delete.
    """
    cp = _ControlPlane(workspace, store=None)
    live_tree = cp.tree(LIVE)
    orphan_tree = cp.tree(ORPHAN)
    round_ = await cp.scanner().round()
    assert round_.answer == {
        "node": HOST,
        "scanned": 2,
        "protected": [],
        "orphans": [],
        "removed": [],
        "failed": [],
        "deferred": (
            "this control plane's records are process-local (no shared record "
            "store): a process-local set cannot certify that no record anywhere "
            "claims a tree -- deferring the sweep"
        ),
    }
    assert live_tree.is_dir() is True
    assert orphan_tree.is_dir() is True
    assert cp.maint.calls == []


# --------------------------------------------------------------- case ③


@pytest.mark.asyncio
async def test_an_unreadable_record_defers_the_whole_sweep(workspace: Path) -> None:
    """Leg (b): one record the store cannot answer makes the view incomplete.

    ``_iter_stored_records`` used to *skip* such entries silently, which is
    exactly how a live sandbox's record becomes invisible while its tree is
    still on the disk.
    """
    store = _SharedStore({LIVE: None})
    cp = _shape(workspace, store=store)
    tree = cp.tree(LIVE)
    round_ = await cp.scanner().round()
    assert round_.answer == {
        "node": HOST,
        "scanned": 1,
        "protected": [],
        "orphans": [],
        "removed": [],
        "failed": [],
        "deferred": (
            "1 record(s) in the shared store could not be read: the fleet view "
            "is incomplete -- deferring the sweep"
        ),
    }
    assert tree.is_dir() is True
    assert cp.maint.calls == []


@pytest.mark.asyncio
async def test_a_tombstoned_record_is_not_an_unreadable_one(workspace: Path) -> None:
    """A deleted record is a fact, not an unreadable one: the sweep runs."""
    store = _SharedStore({ORPHAN: None})
    store.tombstones.add(ORPHAN)
    cp = _shape(workspace, store=store)
    cp.tree(ORPHAN)
    round_ = await cp.scanner().round()
    assert round_.answer == {
        "node": HOST,
        "scanned": 1,
        "protected": [],
        "orphans": [ORPHAN],
        "removed": [ORPHAN],
        "failed": [],
        "deferred": None,
    }
    assert (cp.workspace_base / ORPHAN).exists() is False


@pytest.mark.asyncio
async def test_the_id_count_compared_against_the_fleet_metrics_defers(
    workspace: Path, monkeypatch
) -> None:
    """Leg (c): the enumeration count must match the fleet-wide record count."""
    import control_plane.self_heal as self_heal

    cp = _shape(workspace)
    cp.tree(ORPHAN)
    monkeypatch.setattr(self_heal, "active_sandbox_count", lambda state: 1)
    round_ = await cp.scanner().round()
    assert round_.answer == {
        "node": HOST,
        "scanned": 1,
        "protected": [],
        "orphans": [],
        "removed": [],
        "failed": [],
        "deferred": (
            "fleet sandbox enumeration is incomplete (0 of 1 records accounted "
            "for) -- deferring the sweep"
        ),
    }
    assert (cp.workspace_base / ORPHAN).is_dir() is True


@pytest.mark.asyncio
async def test_a_deferred_sweep_backs_off_and_logs_one_named_line(
    workspace: Path, caplog
) -> None:
    """A deferral is retried at a decaying rate, and never silently (M1)."""
    cp = _ControlPlane(workspace, store=None)
    cp.tree(ORPHAN)
    scanner = cp.scanner()
    with caplog.at_level(logging.WARNING, logger="c3_agent.scan"):
        first = await scanner.round()
    assert first.next_delay_s == 240.0
    # Scoped to this module's own logger: the control plane's warning (and the
    # deployment's startup lines) are other components' business.
    assert [
        record.message
        for record in caplog.records
        if record.name == "c3_agent.scan"
    ] == [
        "c3-agent inventory: the sweep was deferred by the control plane "
        "(1 tree(s) reported): this control plane's records are process-local "
        "(no shared record store): a process-local set cannot certify that no "
        "record anywhere claims a tree -- deferring the sweep"
    ]
    assert (await scanner.round()).next_delay_s == 480.0
    assert (await scanner.round()).next_delay_s == 600.0
    assert (await scanner.round()).next_delay_s == 600.0


def test_the_scan_schedule_is_the_documented_one() -> None:
    schedule = ScanSchedule(
        initial_delay_s=30.0, interval_s=120.0, backoff_max_s=600.0
    )
    assert schedule.first_delay_s == 30.0
    assert [
        schedule.delay_after(consecutive_deferrals=n) for n in (0, 1, 2, 3, 4, 9)
    ] == [120.0, 240.0, 480.0, 600.0, 600.0, 600.0]


def test_a_scan_that_cannot_be_configured_says_so_instead_of_polling(
    caplog,
) -> None:
    """A container asked to scan without a destination (or with a 0 cadence) is inert."""
    from c3_agent.scan import scanner_for

    with caplog.at_level(logging.WARNING, logger="c3_agent.scan"):
        assert (
            scanner_for(
                AgentSettings(token=AGENT_TOKEN, node_id=HOST, scan_enabled=True)
            )
            is None
        )
    assert [
        record.message
        for record in caplog.records
        if record.name == "c3_agent.scan"
    ] == [
        "c3-agent inventory: E2B_C3_AGENT_SCAN is on but E2B_CONTROL_PLANE_URL "
        "is empty: this container cannot report what it sees, so the sweep is "
        "inert here"
    ]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="c3_agent.scan"):
        assert (
            scanner_for(
                AgentSettings(
                    token=AGENT_TOKEN,
                    node_id=HOST,
                    scan_enabled=True,
                    control_plane_url=CP_URL,
                    scan_interval_s=0.0,
                )
            )
            is None
        )
    assert [
        record.message
        for record in caplog.records
        if record.name == "c3_agent.scan"
    ] == [
        "c3-agent inventory: E2B_C3_AGENT_SCAN_INTERVAL_S must be positive (got "
        "0.0): the sweep is inert here"
    ]


# ------------------------------------------------------- the agent's eyes


def test_the_scan_reads_sandbox_shaped_trees_only(workspace: Path) -> None:
    """The scan is a directory read of the agent's own mount -- nobody's argv."""
    cp = _shape(workspace)
    cp.tree(ORPHAN)
    cp.tree(LIVE)
    (cp.workspace_base / "_images").mkdir()
    (cp.workspace_base / "state").mkdir()
    (cp.workspace_base / ".hidden").mkdir()
    (cp.workspace_base / "not-a-sandbox.txt").write_text("x", encoding="utf-8")
    assert cp.scanner().scan_once() == [LIVE, ORPHAN]


def test_a_missing_workspace_base_is_an_empty_scan_not_a_crash(
    workspace: Path, caplog
) -> None:
    cp = _shape(workspace)
    absent = str(workspace / "absent")
    with caplog.at_level(logging.WARNING, logger="c3_agent.scan"):
        assert cp.scanner(workspace_base=absent).scan_once() == []
    assert [
        record.message
        for record in caplog.records
        if record.name == "c3_agent.scan"
    ] == [
        "c3-agent inventory: the workspace base "
        f"{absent} does not exist: nothing to report"
    ]


@pytest.mark.asyncio
async def test_the_report_body_is_the_ids_and_nothing_else(workspace: Path) -> None:
    """Hard rule 3/§14.4: the eyes report ids; the control plane names targets."""
    cp = _shape(workspace)
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/internal/nodes/{HOST}/agent/inventory"
        assert request.headers["X-Internal-Key"] == AGENT_TOKEN
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"node": HOST, "removed": [ORPHAN]})

    reporter = HttpInventoryReporter(
        url=CP_URL,
        node_id=HOST,
        token=AGENT_TOKEN,
        timeout_s=10.0,
        transport=httpx.MockTransport(handler),
    )
    scanner = InventoryScanner(
        settings=AgentSettings(
            token=AGENT_TOKEN,
            node_id=HOST,
            control_plane_url=CP_URL,
            scan_enabled=True,
            workspace_base=str(cp.workspace_base),
        ),
        reporter=reporter,
    )
    cp.tree(ORPHAN)
    round_ = await scanner.round()
    assert seen == [{"sandboxes": [ORPHAN]}]
    assert round_.answer == {"node": HOST, "removed": [ORPHAN]}
    assert round_.next_delay_s == 120.0


@pytest.mark.asyncio
async def test_an_unreachable_control_plane_is_named_and_retried(
    workspace: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    reporter = HttpInventoryReporter(
        url=CP_URL,
        node_id=HOST,
        token=AGENT_TOKEN,
        timeout_s=10.0,
        transport=httpx.MockTransport(handler),
    )
    scanner = InventoryScanner(
        settings=AgentSettings(
            token=AGENT_TOKEN,
            node_id=HOST,
            control_plane_url=CP_URL,
            scan_enabled=True,
        ),
        reporter=reporter,
    )
    round_ = await scanner.round()
    assert round_.failure == f"the control plane at {CP_URL} is unreachable: boom"
    assert round_.next_delay_s == 240.0


@pytest.mark.asyncio
async def test_a_refusing_control_plane_is_named_with_its_own_words(
    workspace: Path,
) -> None:
    message = (
        f"a report for agent {HOST} came from 10.9.9.9, expected {AGENT_IP}"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": 403, "message": message})

    reporter = HttpInventoryReporter(
        url=CP_URL,
        node_id=HOST,
        token=AGENT_TOKEN,
        timeout_s=10.0,
        transport=httpx.MockTransport(handler),
    )
    scanner = InventoryScanner(
        settings=AgentSettings(
            token=AGENT_TOKEN,
            node_id=HOST,
            control_plane_url=CP_URL,
            scan_enabled=True,
        ),
        reporter=reporter,
    )
    round_ = await scanner.round()
    assert round_.failure == (
        f"the control plane refused the inventory report (status 403): {message}"
    )


# ------------------------------------------------- the hop's other failures


@pytest.mark.asyncio
async def test_the_agent_credential_is_not_a_general_internal_key(
    workspace: Path,
) -> None:
    """Narrow on purpose: the agent token opens the agent surfaces only."""
    cp = _shape(workspace)
    report = await cp.report({"sandboxes": [ORPHAN]}, key=FLEET_KEY)
    async with cp.client_for() as client:
        other = await client.post(
            "/internal/nodes/worker-1/file-op",
            headers={"X-Internal-Key": AGENT_TOKEN},
            json={"op": "remove-workspace", "sandbox_id": LIVE},
        )
    assert report.status_code == 401
    assert report.json() == {"code": 401, "message": "Unauthorized"}
    assert other.status_code == 401


@pytest.mark.asyncio
async def test_a_report_from_the_wrong_network_position_is_refused(
    workspace: Path,
) -> None:
    cp = _shape(workspace)
    resp = await cp.report({"sandboxes": [ORPHAN]}, source_ip="10.9.9.9")
    assert resp.status_code == 403
    assert resp.json() == {
        "code": 403,
        "message": (
            f"a report for agent {HOST} came from 10.9.9.9, expected {AGENT_IP}"
        ),
    }


@pytest.mark.asyncio
async def test_a_compose_report_is_accepted_from_either_face(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compose lane's two faces are two containers, so two addresses.

    Measured on the live multinode stack (2026-09-30): the scanner is face B
    (the container that mounts the workspaces), so its report arrives from
    ``c3-agent-maint``'s address while the instruction path is addressed by
    ``c3-agent``'s. A resolver that named only face A refused every report --
    ``a report for agent c3-agent came from 192.168.117.3, expected
    192.168.117.2`` -- which is the shape that made the lane's self-heal inert
    even with a shared record store. Both faces' addresses are this agent's;
    the union is *not* a widening, and a third address is still refused.
    """
    cp = _shape(workspace)
    orphan_path = cp.tree(ORPHAN)
    hosts = {
        "c3-agent": ("10.44.0.7",),
        "c3-agent-maint": ("10.44.0.9",),
    }
    monkeypatch.setattr(
        "control_plane.c3_agent_client._host_source_ips",
        lambda host: hosts[host],
    )
    # The compose shape's agent: one name (`c3-agent`) that is both the URL host
    # the control plane dials and the identity each face compares the
    # instruction's path against (D12). Only the address differs between the
    # two faces.
    agent = create_agent_app(
        settings=AgentSettings(
            token=AGENT_TOKEN,
            node_id="c3-agent",
            maint_path=MAINT,
            workspace_base=str(cp.workspace_base),
            state_base=str(cp.state_base),
        ),
        maint_runner=cp.maint,
    )
    cp.app.state.c3_agent_client = C3AgentClient(
        resolver=ComposeAgentAddressResolver(
            "http://c3-agent:49985", "http://c3-agent-maint:49986"
        ),
        token=AGENT_TOKEN,
        timeout_s=5.0,
        file_op_timeout_s=30.0,
        transport=httpx.ASGITransport(app=agent),
    )

    async def report(source_ip: str):
        async with cp.client_for(source_ip=source_ip) as client:
            return await client.post(
                "/internal/nodes/c3-agent/agent/inventory",
                headers={"X-Internal-Key": AGENT_TOKEN},
                json={"sandboxes": [ORPHAN]},
            )

    # Face B's address -- the scanner's -- is accepted, and the orphan goes.
    accepted = await report("10.44.0.9")
    assert accepted.status_code == 200
    assert accepted.json()["removed"] == [ORPHAN]
    assert not orphan_path.exists()
    # ...and so is face A's (either container is this agent).
    assert (await report("10.44.0.7")).status_code == 200
    # A third address is still refused, and the refusal names the whole union.
    refused = await report("10.9.9.9")
    assert refused.status_code == 403
    assert refused.json() == {
        "code": 403,
        "message": (
            "a report for agent c3-agent came from 10.9.9.9, expected one of "
            "10.44.0.7, 10.44.0.9"
        ),
    }


@pytest.mark.asyncio
async def test_an_agent_the_control_plane_cannot_locate_is_a_named_503(
    workspace: Path,
) -> None:
    cp = _shape(workspace)
    cp.app.state.c3_agent_client = C3AgentClient(
        resolver=_StaticAgentResolver({}),
        token=AGENT_TOKEN,
        timeout_s=5.0,
    )
    resp = await cp.report({"sandboxes": [ORPHAN]})
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            f"cannot determine the address of the agent for node {HOST}: "
            "refusing (fail closed)"
        ),
    }


@pytest.mark.asyncio
async def test_an_unconfigured_agent_credential_refuses_by_name(
    workspace: Path,
) -> None:
    cp = _shape(workspace)
    cp.app.state.settings.c3_agent_token = ""
    resp = await cp.report({"sandboxes": [ORPHAN]})
    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            "this control plane names no agent credential "
            "(E2B_C3_AGENT_TOKEN): refusing agent reports"
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"sandboxes": [ORPHAN], "path": "/etc"},
        {"sandboxes": [ORPHAN], "uid": 0},
        {"sandboxes": ORPHAN},
        {"sandboxes": ["../etc"]},
        {"sandboxes": ["state"]},
    ],
)
async def test_a_report_that_names_a_target_is_refused(
    workspace: Path, body
) -> None:
    cp = _shape(workspace)
    resp = await cp.report(body)
    assert resp.status_code == 400
    assert cp.maint.calls == []


@pytest.mark.asyncio
async def test_the_scan_does_not_report_and_the_control_plane_does_not_delete_a_reserved_name(
    workspace: Path,
) -> None:
    """``state`` spells a legal sandbox id -- both layers must refuse it."""
    cp = _shape(workspace)
    (cp.workspace_base / "state").mkdir(parents=True, exist_ok=True)
    assert cp.scanner().scan_once() == []
    resp = await cp.report({"sandboxes": ["state"]})
    assert (resp.status_code, cp.maint.calls) == (400, [])


# ---------------------------------------------- the surfaces around the sweep


@pytest.mark.asyncio
async def test_a_worker_may_not_ask_for_the_sweeps_removal(workspace: Path) -> None:
    """The op is in the platform's table, and *not* in the worker's vocabulary.

    The sweep's removal deletes a tree that no record claims; a worker that
    could ask for it would hold exactly the privilege C3 took away from it.
    """
    from control_plane import file_ops

    cp = _shape(workspace)
    cp.live_record()
    cp.tree(ORPHAN)
    async with cp.client_for(source_ip=WORKER_ENDPOINT.ip) as client:
        enrolled = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": FLEET_KEY},
            json={
                "nodeID": WORKER,
                "address": WORKER_ENDPOINT.address,
                "totalMemoryMB": 1024,
                "totalCPUPercent": 100,
                "totalDiskMB": 1024,
                "totalProcesses": 64,
            },
        )
        assert enrolled.status_code == 200
        resp = await client.post(
            f"/internal/nodes/{WORKER}/file-op",
            headers={"X-Internal-Key": FLEET_KEY},
            json={"op": "remove-orphan-workspace", "sandbox_id": ORPHAN},
        )
    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": (
            "the worker surface may not ask for remove-orphan-workspace (it is "
            "in the self-heal set): refusing"
        ),
    }
    assert file_ops.FILE_OPS["remove-orphan-workspace"].callers == frozenset(
        {"self-heal"}
    )
    assert (cp.workspace_base / ORPHAN).is_dir() is True


@pytest.mark.asyncio
async def test_the_count_surface_is_the_one_the_fleet_metrics_endpoint_reports(
    workspace: Path,
) -> None:
    """Leg (c) compares against *the* fleet-wide count, not a second definition."""
    from control_plane.fleet_view import active_sandbox_count

    cp = _shape(workspace)
    cp.live_record()
    async with cp.client_for() as client:
        resp = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": FLEET_KEY}
        )
    assert resp.status_code == 200
    assert resp.json()["activeSandboxes"] == active_sandbox_count(cp.app.state) == 1


@pytest.mark.asyncio
async def test_the_agent_app_runs_the_scan_loop_only_when_it_is_configured(
    workspace: Path,
) -> None:
    """The wiring: a container that is asked to scan actually runs the loop."""
    cp = _shape(workspace)
    started: list[int] = []

    class _Scanner:
        schedule = ScanSchedule(initial_delay_s=30.0, interval_s=120.0)

        async def run(self, stop) -> None:
            started.append(1)
            await stop.wait()

    app = create_agent_app(
        settings=AgentSettings(
            token=AGENT_TOKEN,
            node_id=HOST,
            control_plane_url=CP_URL,
            scan_enabled=True,
            maint_path=MAINT,
            workspace_base=str(cp.workspace_base),
        ),
        maint_runner=cp.maint,
        inventory=_Scanner(),
    )
    async with app.router.lifespan_context(app):
        # The loop is a task; let it reach its first await before asserting.
        await asyncio.sleep(0)
        assert started == [1]
    # And an app that was told not to scan runs no loop at all.
    quiet = create_agent_app(
        settings=AgentSettings(
            token=AGENT_TOKEN, node_id=HOST, maint_path=MAINT
        ),
        maint_runner=cp.maint,
        inventory=None,
    )
    async with quiet.router.lifespan_context(quiet):
        assert started == [1]


@pytest.mark.asyncio
async def test_the_face_that_executes_a_removal_needs_no_worker_identity(
    workspace: Path,
) -> None:
    """``rm`` acts as nobody; ``chown`` still needs the worker it acts as."""
    cp = _shape(workspace)
    tree = cp.tree(ORPHAN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cp.agent), base_url="http://agent"
    ) as client:
        headers = {"X-Internal-Key": AGENT_TOKEN}
        rm = await client.post(
            f"/internal/nodes/{HOST}/agent/rm",
            headers=headers,
            json={"sandbox_id": ORPHAN, "path": str(tree)},
        )
        chown = await client.post(
            f"/internal/nodes/{HOST}/agent/chown",
            headers=headers,
            json={"sandbox_id": ORPHAN, "path": str(tree), "uid": 10007},
        )
    assert rm.status_code == 200
    assert tree.exists() is False
    assert chown.status_code == 400
    # The body names no worker at all, so the missing half is the group the
    # binary gates ``--gid`` against. The message names *that* half rather than
    # "the worker's own identity" because the two are no longer one
    # precondition: a ``--gid``-carrying chown needs the gid, and only a
    # ``--worker`` chown needs the uid the binary would write into the tree
    # (``c3_agent.fileops.run_file_op``'s shape gate; the create path's
    # ``materialize-tree`` is the ``chown --uid … --gid …`` shape).
    assert chown.json() == {
        "error": (
            "a chown instruction needs the group it hands the tree to (the "
            "binary checks --gid against the worker's own gid): refusing"
        )
    }
