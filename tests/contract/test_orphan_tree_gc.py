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

import asyncio
import contextlib
import errno
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import httpx
import pytest

import envd_service.agent as agent_mod
import envd_service.priv_helpers as priv_helpers
import envd_service.xfs_quota as xfs_quota
from envd_service import xfs_quotactl
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from envd_service.agent import NodeAgent
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from tests._disk_projids import install_disk_projids as _install_disk_projids
from tests._disk_projids import read_calls

INTERNAL_KEY = "internal-key"
API_KEY = "local-key"
STRANDED_PROJID = 1677253610
STRANDED_VOLUME_PROJID = 987654321
#: Review round 1 (M4): the project ids of an unrelated, live tenant whose
#: tree and rows a rewritten ``sandbox.json`` tries to aim the sweep at.
VICTIM_PROJID = 1807253611
VICTIM_VOLUME_PROJID = 1876543211


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


class _OnlyThisThread(logging.Filter):
    """Keep the log records this test's own thread emitted.

    ``caplog`` is a handler on the root logger, so it records *every* thread's
    output. A full-directory run has the session-scoped harness workers next
    door (``multinode_servers``, started by ``test_command_logs.py``)
    heartbeating and reconciling in uvicorn's threads for the rest of the
    session, and their ``envd_service.agent`` lines used to land in this
    file's exact log assertions -- ``Left contains 2 more items`` with two
    ``reconcile summary`` lines nobody in this test asked for. The worker
    under test runs on this test's own event loop, i.e. in this thread.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == threading.get_ident()


@pytest.fixture(autouse=True)
def _owned_log_capture(caplog):
    """Scope ``caplog`` to this test's thread (see ``_OnlyThisThread``)."""
    caplog.handler.addFilter(_OnlyThisThread())
    yield caplog


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
        #: Per-release latency, for the "teardown runs off the event loop"
        #: contract (M3): the quota agent is a synchronous HTTP call with a
        #: multi-second timeout in production.
        self.release_delay_s = 0.0
        #: The thread each release ran on. A release is a synchronous call,
        #: so "the teardown is off the event loop" is exactly "no entry in
        #: here is the loop's thread" -- a thread identity, not a wall-clock
        #: gap (W8).
        self.release_threads: list[int] = []
        self.reconcile_calls: list[dict] = []
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
                "report": self.report,
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
        self.release_threads.append(threading.get_ident())
        if self.release_delay_s:
            time.sleep(self.release_delay_s)
        self.released.append((str(project_dir), int(projid)))

    def clear_limits(self, *, mount_point, projid) -> None:
        """Reset the limits, like ``limit -p bsoft=0 bhard=0`` (N12).

        The worker calls this *after* the tree is gone, and with usage at zero
        that is what removes the record. Deferred accounting is the exception
        the bounded-retry contract needs: XFS only drops a row whose usage *and*
        limits are zero, so while ``defer_rounds`` says the usage has not
        settled the row survives -- and the reconcile rounds it, not the
        teardown.
        """
        self.cleared.append((str(mount_point), int(projid)))
        if self.defer_rounds <= 0:
            self.rows.pop(int(projid), None)

    def report(self, mount_point) -> dict:  # noqa: ANN001
        """The quota table as the worker would read it (E2.4 contract).

        The reclaim pass asks the table whether a row is still there rather than
        trusting the reconciliation's ``cleaned`` list -- a row the teardown
        already dropped (N12) is settled and must not be reported as
        unreclaimed.
        """
        return {
            "projects": {
                str(projid): {
                    "used_blocks": used,
                    "soft_blocks": 0,
                    "hard_blocks": 0,
                }
                for projid, used in self.rows.items()
            }
        }

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
    created_at: float | None = 1_600_000_000.0,
    mtime: float | None = None,
    record_text: str | None = None,
) -> Path:
    """Write a sandbox tree (``<base>/<id>/sandbox.json``) on disk.

    ``created_at=None`` omits the key entirely, the shape of the trees
    written before the field existed (2026-09-02). ``mtime`` sets the
    ``sandbox.json`` modification time, which is what those legacy records
    have to fall back to.
    """
    sandbox_dir = workspace / sandbox_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "workspace").mkdir(exist_ok=True)
    if record_text is not None:
        record_path = sandbox_dir / "sandbox.json"
        record_path.write_text(record_text, encoding="utf-8")
        if mtime is not None:
            os.utime(record_path, (mtime, mtime))
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
    if created_at is not None:
        payload["created_at"] = created_at
    record_path = sandbox_dir / "sandbox.json"
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(record_path, (mtime, mtime))
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


def _agent(
    workspace: Path, *, metrics_provider=None, **settings_overrides
) -> NodeAgent:
    """A freshly started worker (empty in-memory registry) on ``workspace``."""
    agent = NodeAgent(
        settings=_envd_settings(workspace, **settings_overrides),
        runtime_registry=RuntimeRegistry(workspace),
        control_plane_url="http://control",
        node_address="http://127.0.0.1:1",
        metrics_provider=metrics_provider,
    )
    agent._node_id = "node_a"
    return agent


def _foreign_agent(workspace: Path, metrics_provider=None) -> NodeAgent:
    """A second worker whose heartbeats never reach this test's control plane.

    Its address is unreachable, so it stays on the failed-heartbeat path: one
    heartbeat round per wait, no reconcile, nothing of this test's touched.
    """
    agent = NodeAgent(
        settings=_envd_settings(workspace),
        runtime_registry=RuntimeRegistry(workspace),
        control_plane_url="http://127.0.0.1:1",
        node_address="http://127.0.0.1:1",
        metrics_provider=metrics_provider,
    )
    agent._node_id = "node_foreign"
    return agent


def _tick(rounds: list[int]) -> dict:
    """A ``metrics_provider`` that records one entry per heartbeat round."""

    def metrics() -> dict:
        rounds.append(len(rounds) + 1)
        return {}

    return metrics


async def test_heartbeats_keep_their_cadence_while_a_round_scans_the_base(
    workspace, monkeypatch
):
    """N21: a reconcile round must not sit in the heartbeat's way.

    The round used to run inline in the heartbeat coroutine, so the gap between
    two beats was ``interval + round duration`` -- and a round walks every tree
    on the shared base, so that duration grows with the fleet's history and has
    no bound. Both halves of the fix are checked here by making the scan block:

    * the scan runs on a worker thread, so blocking it cannot freeze the event
      loop (with the old synchronous call the loop below could not pulse at all);
    * the round is its own task, so the heartbeat does not wait for it even when
      it is stuck.

    The scan is held open by an event rather than a sleep, so the assertion is
    "these heartbeats went out *while* the round was inside the scan" and not a
    timing coincidence.
    """
    control_nodes, _registry, control_app = _stack(workspace)
    sent: list[int] = []
    agent = _agent(workspace, metrics_provider=_tick(sent))
    agent._node_id = None  # the loop registers first, like production

    scan_started = threading.Event()
    scan_release = threading.Event()

    def blocking_scan(_settings, _runtime_registry):
        scan_started.set()
        scan_release.wait(timeout=30)
        return {}, []

    monkeypatch.setattr(agent_mod, "_scan_workspace_runtimes", blocking_scan)

    intervals: list[int] = []
    parked = asyncio.Event()

    async def hook(_interval):
        intervals.append(len(intervals) + 1)
        if len(intervals) >= 3:
            parked.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(agent._loop())
    # No ``agent=``: this contract wants the harness to stay out of the round's
    # way (the draining hook would hide exactly what it asserts).
    _patch_loop_transport(monkeypatch, control_app, driven=task, agent=agent)
    _patch_loop_sleep(monkeypatch, hook, driven=task)
    try:
        await asyncio.wait_for(parked.wait(), timeout=30)
        # The round is parked inside the scan right now...
        assert scan_started.is_set()
        assert agent._reconcile_task is not None
        assert not agent._reconcile_task.done()
        # ...and the loop still registered and kept beating: three intervals
        # elapsed with the scan holding a thread.
        assert len(intervals) >= 3
        # The first pulse is the registration (no usage payload); every later
        # pulse is a heartbeat, and each one got out while the round was parked.
        assert len(sent) == len(intervals) - 1, (
            f"{len(sent)} heartbeat(s) over {len(intervals)} interval(s)"
        )
        assert len(sent) >= 2
        # ...and the registration itself landed (the loop generates its own node
        # id: ``_node_id`` was None on purpose, as it is on a real first start).
        assert agent._node_id is not None
        assert agent._node_id in {
            node.node_id for node in control_nodes.list()
        }
    finally:
        scan_release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        round_task = agent._reconcile_task
        if round_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await round_task


class _ForeignHeartbeat:
    """A ``NodeAgent`` heartbeat loop running in its own thread and loop.

    This is the shape a full-directory run has anyway: the session-scoped
    ``multinode_servers`` harness (started by
    ``tests/contract/test_command_logs.py``) boots real worker agents in
    uvicorn's threads and leaves them heartbeating for the rest of the
    session, long after their own tests are done.
    """

    def __init__(self, workspace: Path) -> None:
        self.rounds = 0
        self.first_round = threading.Event()
        self._workspace = Path(workspace)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _tick(self) -> dict:
        self.rounds += 1
        self.first_round.set()
        return {}

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        foreign = _foreign_agent(self._workspace, self._tick)
        self._task = loop.create_task(foreign._loop())
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:  # the only way this loop ends
            pass
        finally:
            loop.close()

    def start(self) -> None:
        self._thread.start()

    async def wait_for_first_round(self) -> None:
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, self.first_round.wait, 30.0):
            raise AssertionError("the foreign heartbeat loop never ran a round")

    def stop(self) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self._thread.join(timeout=10)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    )


def _is_driven(driven, agent: NodeAgent | None = None) -> bool:
    """Whether the caller is the loop under test, or one of its rounds.

    Since N21 the reconcile round is a *separate task* (``NodeAgent`` detaches it
    so the heartbeat never waits behind a sweep), so the loop's task is no longer
    the only task this harness has to serve: the round creates its own HTTP client
    and would otherwise talk to the real ``http://control``.
    """
    try:
        current = asyncio.current_task()
    except RuntimeError:  # no running loop: the caller is sync code
        return False
    if current is driven:
        return True
    return (
        agent is not None
        and agent._reconcile_task is not None
        and current is agent._reconcile_task
    )


async def _drain_reconcile_round(agent: NodeAgent) -> None:
    """Let the agent's detached reconcile round finish.

    The rounds below are counted per heartbeat interval. While the round ran
    inline that ordering was implied; now it is a task of its own, so a tick can
    land in the middle of one. Awaiting it here keeps the assertions about the
    retry *schedule* from racing the sweep they are describing.
    """
    task = agent._reconcile_task
    if task is not None and not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _patch_loop_transport(monkeypatch, control_app, *, driven, agent=None) -> None:
    """Point the loop under test's own ``httpx.AsyncClient`` at the app.

    ``httpx`` is patched on the module the agent imports it from, which is
    process-wide, so the redirection is scoped to ``driven``: a full-directory
    run has session-scoped harness workers (``multinode_servers``, started by
    ``test_command_logs.py``) heartbeating in uvicorn's threads for the rest
    of the session, and an unscoped patch re-pointed *their* clients at this
    test's control plane.
    """
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        if not _is_driven(driven, agent):
            return real_client(*args, **kwargs)
        kwargs.pop("timeout", None)
        return real_client(
            transport=httpx.ASGITransport(app=control_app),
            base_url="http://control",
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _patch_loop_sleep(monkeypatch, hook, *, driven, agent=None) -> None:
    """Drive ``NodeAgent._loop``'s 5s heartbeat sleep from the test.

    Only ``asyncio``'s attribute inside ``envd_service.agent`` is replaced
    (by a proxy that forwards everything else), so the loop's own waits are
    what the test controls: ``hook(interval)`` runs once per heartbeat
    interval of the loop under test, and its sub-second waits (the quota
    reclaim's) stay instant.

    The hook is scoped to ``driven`` because ``envd_service.agent.asyncio``
    is a *module* attribute: it reaches every ``NodeAgent`` loop in the
    process, and the harness workers above heartbeat in the very rounds this
    contract counts (their traffic shows up as ``rounds=[1, 2, 6, 14]`` and
    the shape goes red only when the whole directory runs). Every other loop
    keeps its real 5s cadence and its real client.
    """
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        if _is_driven(driven, agent):
            if delay >= 1.0:
                # Only from the loop's own task: awaiting the round from inside
                # the round would deadlock on itself.
                if agent is not None and asyncio.current_task() is driven:
                    await _drain_reconcile_round(agent)
                await hook(delay)
            await real_sleep(0)
            return
        await real_sleep(delay)

    class _AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        sleep = staticmethod(fake_sleep)

    monkeypatch.setattr(agent_mod, "asyncio", _AsyncioProxy())


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
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert client.posts == [
        {"sandboxIDs": [sandbox_id], "snapshotIDs": [sandbox_id]}
    ]


@pytest.mark.asyncio
async def test_restart_reclaims_an_unowned_tree_and_its_quota_row(
    workspace, monkeypatch, caplog, disk_read_backend
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
    # follow-up 1: the project-id read asks the path itself what state it is
    # in before it trusts any backend, so the slice this record points at has
    # to be materialised the way a live sandbox's volume leaves it. The
    # "volume deleted before the sweep" shape has its own contract next to
    # this one.
    slice_dir = (
        sandbox_dir / "volumes" / f"vol_id_{STRANDED_VOLUME_PROJID}" / sandbox_id
    )
    slice_dir.mkdir(parents=True)
    # The target machine reports the project ids from the disk; the record's
    # own claim is sandbox-writable, so the teardown has to match this.
    disk = _install_disk_projids(
        monkeypatch,
        {
            sandbox_dir: STRANDED_PROJID,
            slice_dir: STRANDED_VOLUME_PROJID,
        },
        backend=disk_read_backend,
    )
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
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        # Deferred accounting (`defer_rounds=1`): a limit reset cannot drop a row
        # whose usage has not settled, so this is the case the bounded reclaim
        # pass still exists for -- it is what cleans both rows.
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
    assert disk.calls == read_calls(
        disk_read_backend, sandbox_dir, volume_slice / sandbox_id
    )
    assert _agent_messages(caplog) == [
        f"reconcile: removing orphan runtime {sandbox_id} (not in control plane)",
    ]


@pytest.mark.asyncio
async def test_quota_reclaim_is_bounded_and_reported_when_accounting_never_settles(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """A row that never settles is reported, not retried forever.

    The tree removal is already correct in this case; the round must stay
    quiet about it apart from a precise warning, and a later reconcile (the
    next startup) still reclaims the row.

    The tree is torn down *from the project id the disk reports*, so the disk
    has to answer it here: left to the host, a machine whose fd backend works
    reads the untagged tree's real project id (0), the round has no row to
    reclaim, and the assertions below never see a reclaim attempt at all.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_never_settles"
    sandbox_dir = _tree(workspace, sandbox_id, project_id=STRANDED_PROJID)
    quota = _QuotaFake({STRANDED_PROJID: 8}, defer_rounds=99)
    quota.install(monkeypatch)
    _install_disk_projids(
        monkeypatch,
        {sandbox_dir: STRANDED_PROJID},
        backend=disk_read_backend,
    )
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
    ) in _agent_messages(caplog)

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
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert _agent_messages(caplog) == []


@pytest.mark.asyncio
async def test_snapshot_store_directory_is_spared_by_its_shape(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M1 rework, RED-B: the store's own shape is still never a candidate.

    ``SnapshotRegistry``'s base *is* the workspace base
    (``control_plane/app.py``), so a snapshot is a top-level ``snap_<hex>``
    directory next to the ``sbx_*`` trees, and ``snap_`` passes
    ``validate_sandbox_id`` (``_`` is a legal id character). The predicate
    separates it from a sandbox tree by shape, not by name: the store holds
    ``snapshot.json`` and the copied filesystem under ``fs/`` (the sandbox
    record of that copy lives at ``snap_X/fs/sandbox.json``), so it carries no
    *top-level* ``sandbox.json`` and stays out of both scans.

    ``tmp/fu-m1-02-verify-prior.log`` pins that shape against the real
    ``SnapshotRegistry`` and the real snapshot API (top level is exactly
    ``['fs', 'snapshot.json']``; a client-supplied ``snapshotID`` in the body
    is ignored).
    """
    _nodes, _registry, control_app = _stack(workspace)

    snap_id = "snap_0040ce7e44f6365f"
    snap_dir = workspace / snap_id
    (snap_dir / "fs").mkdir(parents=True)
    snapshot_marker = snap_dir / "snapshot.json"
    snapshot_marker.write_text(
        json.dumps({"snapshot_id": snap_id}), encoding="utf-8"
    )
    fs_payload = snap_dir / "fs" / "marker.txt"
    fs_payload.write_text("snapshot payload", encoding="utf-8")
    nested_record = snap_dir / "fs" / "sandbox.json"
    nested_record.write_text(
        json.dumps(
            {"sandbox_id": "sbx_snapshotted", "workspace_dir": "/elsewhere"}
        ),
        encoding="utf-8",
    )

    # A real orphan tree of the same round, so the tightening is observable.
    orphan_id = "sbx_stranded"
    orphan_dir = _tree(workspace, orphan_id, project_id=STRANDED_VOLUME_PROJID)
    quota = _QuotaFake({STRANDED_VOLUME_PROJID: 8})
    quota.install(monkeypatch)
    disk = _install_disk_projids(
        monkeypatch,
        {orphan_dir: STRANDED_VOLUME_PROJID},
        backend=disk_read_backend,
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    # The scan behind both the GC and the quota reclaim: the store is neither a
    # materialised nor an unmaterialised workspace runtime.
    records, unmaterialised = agent_mod._scan_workspace_runtimes(
        _envd_settings(workspace), RuntimeRegistry(workspace)
    )
    assert sorted(records) == [orphan_id]
    assert unmaterialised == []

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert summary == {
        "deleted": [orphan_id],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    # The store is untouched — marker, copied filesystem and the nested record
    # all still there — and the round never reached the disk for it.
    assert snapshot_marker.read_text(encoding="utf-8") == json.dumps(
        {"snapshot_id": snap_id}
    )
    assert fs_payload.read_text(encoding="utf-8") == "snapshot payload"
    assert nested_record.is_file()
    assert quota.released == [(str(orphan_dir), STRANDED_VOLUME_PROJID)]
    assert quota.rows == {}
    assert disk.calls == read_calls(disk_read_backend, orphan_dir)
    assert _agent_messages(caplog) == [
        f"reconcile: removing orphan runtime {orphan_id} (not in control plane)",
    ]


@pytest.mark.parametrize("chosen_id", ["snap_client1", "_client1"])
@pytest.mark.asyncio
async def test_a_client_chosen_prefixed_id_is_reclaimed_not_stranded(
    workspace, monkeypatch, caplog, chosen_id, disk_read_backend
):
    """M1 rework, RED-A: the leak the unconditional prefix exclusion caused.

    ``X-Sandbox-Id`` is validated with ``validate_sandbox_id`` alone
    (``control_plane/api/sandboxes.py``), so ``snap_client1`` and ``_client1``
    are legal sandbox ids and their trees land at ``<base>/<id>`` — the real
    API accepts both with ``201`` (``tmp/fu-m1-03-create-prefixed-id.log``).
    Such a tree carries its own top-level ``sandbox.json``, so it *is* a
    sandbox tree, whatever its name starts with: its orphan tree must be
    reclaimed and its quota row freed like any other.

    Before the fix the name alone excluded it from the GC candidate set, while
    ``_recorded_projids`` — which reads ``sandbox.json`` directly and never
    consults the predicate — kept pinning its row through the surviving file:
    the tree and the row pinned each other forever, silently.
    """
    _nodes, _registry, control_app = _stack(workspace)

    chosen_dir = _tree(workspace, chosen_id, project_id=STRANDED_PROJID)
    orphan_id = "sbx_stranded"
    orphan_dir = _tree(workspace, orphan_id, project_id=STRANDED_VOLUME_PROJID)
    # The real API's record for such an id, as the review logged it.
    record = json.loads((chosen_dir / "sandbox.json").read_text(encoding="utf-8"))
    assert record["sandbox_id"] == chosen_id
    assert record["workspace_dir"] == str(chosen_dir)

    quota = _QuotaFake({STRANDED_PROJID: 8, STRANDED_VOLUME_PROJID: 8})
    quota.install(monkeypatch)
    disk = _install_disk_projids(
        monkeypatch,
        {
            chosen_dir: STRANDED_PROJID,
            orphan_dir: STRANDED_VOLUME_PROJID,
        },
        backend=disk_read_backend,
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    records, unmaterialised = agent_mod._scan_workspace_runtimes(
        _envd_settings(workspace), RuntimeRegistry(workspace)
    )
    assert sorted(records) == sorted([chosen_id, orphan_id])
    assert unmaterialised == []

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert chosen_dir.exists() is False
    assert orphan_dir.exists() is False
    assert summary == {
        "deleted": sorted([chosen_id, orphan_id]),
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert quota.rows == {}
    # The round walks its targets in id order.
    by_id = {
        chosen_id: (chosen_dir, STRANDED_PROJID),
        orphan_id: (orphan_dir, STRANDED_VOLUME_PROJID),
    }
    assert quota.released == [
        (str(by_id[sandbox_id][0]), by_id[sandbox_id][1])
        for sandbox_id in sorted([chosen_id, orphan_id])
    ]
    assert _agent_messages(caplog) == [
        f"reconcile: removing orphan runtime {sandbox_id} "
        f"(not in control plane)"
        for sandbox_id in sorted([chosen_id, orphan_id])
    ]
    # Every prefixed tree that carries its own record reaches the disk read;
    # none is filtered out by its name.
    assert sorted(str(call[-1]) for call in disk.calls) == sorted(
        [str(chosen_dir), str(orphan_dir)]
    )


@pytest.mark.asyncio
async def test_a_whole_tree_copy_under_a_snapshot_id_is_refused(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M1 rework, RED-C: the dangerous shape is refused, not deleted.

    A whole-tree copy of a sandbox landing at ``<base>/snap_<hex>`` carries the
    copied ``sandbox.json``, whose record still names the *original* sandbox
    and its directory — that is what ``cp -r <base>/sbx_src <base>/snap_X``
    produces, and the only shape of it the product can produce (the store's own
    copies put the record under ``fs/``). It now enters the candidate set (it
    looks like a sandbox tree), and the M4 guards then refuse it: the teardown
    is aimed by the directory name, so a record that disagrees with it is not
    evidence of anything. The tree is left alone and reported as
    ``untrusted_records`` — the direction that must never relax.
    """
    _nodes, registry, control_app = _stack(workspace)

    # The source is a live sandbox of this node (its record is in the control
    # plane), so the round's only candidate is the copy.
    source_id = "sbx_src"
    _control_record(registry, "node_a", source_id)
    source_dir = _tree(workspace, source_id, project_id=VICTIM_PROJID)
    snap_id = "snap_0040ce7e44f6365f"
    copied_dir = workspace / snap_id
    shutil.copytree(source_dir, copied_dir, symlinks=True)
    copied_record = json.loads(
        (copied_dir / "sandbox.json").read_text(encoding="utf-8")
    )
    assert copied_record["sandbox_id"] == source_id
    assert copied_record["workspace_dir"] == str(source_dir)

    quota = _QuotaFake({VICTIM_PROJID: 8})
    quota.install(monkeypatch)
    # The disk can answer for the copy (its directory carries the project
    # state), so the refusal has to come from the record/directory mismatch,
    # not from a silent disk.
    _install_disk_projids(
        monkeypatch, {copied_dir: VICTIM_PROJID}, backend=disk_read_backend
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert copied_dir.exists()
    assert (copied_dir / "sandbox.json").is_file()
    assert quota.rows == {VICTIM_PROJID: 8}
    assert quota.released == []
    assert summary == {
        "deleted": [],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "disk_sweep_skipped": [],
        "untrusted_records": [snap_id],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert _agent_messages(caplog) == [
        f"reconcile: leaving {snap_id} on disk: its sandbox.json names "
        f"sandbox {source_id!r}",
    ]


@pytest.mark.asyncio
async def test_infrastructure_namespaces_are_still_excluded(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M1 rework, RED-D: the reserved ``_`` namespaces keep their exclusion.

    ``_volumes`` / ``_snapshots`` / ``_templates`` / ``_secrets`` carry no
    top-level ``sandbox.json``, so the shape rule leaves them where they were:
    out of the GC candidate set, out of ``unmaterialised``, out of the quota
    scan's projid map. A real orphan of the same round is still reclaimed.
    """
    _nodes, _registry, control_app = _stack(workspace)
    for name in ("_volumes", "_snapshots", "_templates", "_secrets"):
        (workspace / name).mkdir(exist_ok=True)
    (workspace / "_snapshots" / "snap_a").mkdir(exist_ok=True)
    (workspace / "_snapshots" / "snap_a" / "snapshot.json").write_text(
        "{}", encoding="utf-8"
    )

    orphan_id = "sbx_stranded"
    orphan_dir = _tree(workspace, orphan_id, project_id=STRANDED_VOLUME_PROJID)
    quota = _QuotaFake({STRANDED_VOLUME_PROJID: 8})
    quota.install(monkeypatch)
    disk = _install_disk_projids(
        monkeypatch,
        {orphan_dir: STRANDED_VOLUME_PROJID},
        backend=disk_read_backend,
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    records, unmaterialised = agent_mod._scan_workspace_runtimes(
        _envd_settings(workspace), RuntimeRegistry(workspace)
    )
    assert sorted(records) == [orphan_id]
    assert unmaterialised == []

    # The quota-side consumer of the same predicate (``xfs_quota.py``): the
    # infrastructure namespaces never reach the disk read either.
    assert xfs_quota._scan_project_dirs(workspace) == {
        STRANDED_VOLUME_PROJID: orphan_dir
    }
    # This half of the read sits behind the *administration* gate, which these
    # contracts pin to the subprocess form on every host (the pin, and the
    # reason, are in ``tests/_disk_projids.py``), so the scan asks ``lsattr``
    # in both forms. The teardown's own read -- the two-stage one W2 added --
    # is what ``disk_read_backend`` parameterizes, and the assertions around
    # this one carry both forms.
    assert disk.calls == [["lsattr", "-p", "-d", str(orphan_dir)]]

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert summary == {
        "deleted": [orphan_id],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    for name in ("_volumes", "_snapshots", "_templates", "_secrets"):
        assert (workspace / name).is_dir()


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
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert _agent_messages(caplog) == [
        f"reconcile: cannot read the sandbox record of {empty}",
        "reconcile: 3 sandbox tree(s) on disk have no readable sandbox.json "
        "and were left alone: sbx_corrupt_record,sbx_empty_record,sbx_no_record",
    ]


@pytest.mark.asyncio
async def test_one_failing_tree_does_not_abort_the_round(
    workspace, monkeypatch, caplog, disk_read_backend
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
    _install_disk_projids(
        monkeypatch,
        {failing_dir: 2001, surviving_dir: 2002},
        backend=disk_read_backend,
    )

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
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    assert client.posts == [{"sandboxIDs": [], "snapshotIDs": []}]
    assert _agent_messages(caplog) == [
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
        "disk_sweep_skipped": [],
        "untrusted_records": [],
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
        "disk_sweep_skipped": ["sbx_unowned"],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [],
        "quota_unreclaimed": [],
    }
    # The skip is observable and bounded: the round says why it skipped (log
    # and summary) and schedules the next attempt instead of going silent
    # until the process restarts (M1).
    assert agent._reconcile_retry_in == 1
    assert agent._reconcile_retry_attempts == 1
    assert _agent_messages(caplog) == [
        "reconcile: fleet sandbox enumeration is incomplete "
        "(0 of 1 records accounted for)",
        "reconcile: leaving 1 orphan tree(s) on disk alone this round "
        "(fleet record enumeration unavailable): sbx_unowned",
        "reconcile: disk sweep deferred by an incomplete fleet enumeration; "
        "retrying in 1 heartbeat interval(s) (attempt 1)",
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


# ---------------------------------------------------------------------------
# Review round 1: M1 (a deferred sweep must not go silent), M2 (legacy records
# without ``created_at``), M3 (teardown off the event loop) and M4 (the record
# is sandbox-writable input, so destructive targets come from the disk).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incomplete_fleet_enumeration_is_retried_until_the_fleet_is_complete(
    workspace, monkeypatch, caplog
):
    """M1: one non-enumerable record cannot silence the disk sweep forever.

    ``node_id`` defaults to ``"local"`` and the production stack runs with
    ``E2B_ENABLE_LOCAL_NODE=false``, so a single record on a node that is not
    in ``/internal/nodes`` makes the enumeration short of
    ``/internal/fleet/metrics`` and defers the sweep. The agent has to come
    back on a later heartbeat and reclaim the tree once the enumeration is
    complete again: the first rounds may skip, the sweep must not stay silent
    for the life of the process.
    """
    control_nodes, registry, control_app = _stack(workspace)
    _control_record(registry, "node_ghost", "sbx_ghost_record")
    orphan_id = "sbx_unowned_retry"
    orphan_dir = _tree(workspace, orphan_id, project_id=4101)
    quota = _QuotaFake({4101: 8})
    quota.install(monkeypatch)
    caplog.set_level(logging.INFO)
    caplog.clear()

    agent = _agent(workspace)
    agent._node_id = None  # the loop registers before its first round
    rounds = 0
    skipped_rounds: list[int] = []
    sweep_round: int | None = None
    parked = asyncio.Event()

    async def hook(interval):
        nonlocal rounds, sweep_round
        rounds += 1
        deferred = [
            message
            for message in _agent_messages(caplog)
            if message.startswith("reconcile: disk sweep deferred")
        ]
        if len(deferred) > len(skipped_rounds):
            skipped_rounds.append(rounds)
        if sweep_round is None and not orphan_dir.exists():
            sweep_round = rounds
        if rounds == 2:
            # The node holding the record re-registers: the enumeration is
            # complete again, so the deferred sweep can run.
            _register_node(control_nodes, "node_ghost", "http://127.0.0.1:1")
        if sweep_round is not None or rounds >= 8:
            parked.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(agent._loop())
    _patch_loop_transport(monkeypatch, control_app, driven=task, agent=agent)
    _patch_loop_sleep(monkeypatch, hook, driven=task, agent=agent)
    try:
        await asyncio.wait_for(parked.wait(), timeout=30)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert sweep_round == 4
    assert skipped_rounds == [1, 2]
    assert orphan_dir.exists() is False
    assert quota.rows == {}
    assert [
        message
        for message in _agent_messages(caplog)
        if message.startswith("reconcile: disk sweep deferred")
    ] == [
        "reconcile: disk sweep deferred by an incomplete fleet enumeration; "
        "retrying in 1 heartbeat interval(s) (attempt 1)",
        "reconcile: disk sweep deferred by an incomplete fleet enumeration; "
        "retrying in 2 heartbeat interval(s) (attempt 2)",
    ]
    # L1: every round publishes its summary at INFO, so the fields that only
    # exist in the summary are a positive signal and not just WARNING text.
    assert [
        message
        for message in _agent_messages(caplog)
        if message.startswith("reconcile summary")
    ] == [
        "reconcile summary: deleted=0 delete_failures=0 unmaterialised=0 "
        "protected_elsewhere=0 concurrent_creates=0 quota_cleaned=0 "
        "quota_unreclaimed=0 disk_sweep_skipped=[sbx_unowned_retry] "
        "untrusted_records=[]",
        "reconcile summary: deleted=0 delete_failures=0 unmaterialised=0 "
        "protected_elsewhere=0 concurrent_creates=0 quota_cleaned=0 "
        "quota_unreclaimed=0 disk_sweep_skipped=[sbx_unowned_retry] "
        "untrusted_records=[]",
        "reconcile summary: deleted=1 delete_failures=0 unmaterialised=0 "
        "protected_elsewhere=0 concurrent_creates=0 quota_cleaned=1 "
        "quota_unreclaimed=0 disk_sweep_skipped=[] untrusted_records=[]",
    ]
    # The round that finally swept cleared the backoff again.
    assert agent._reconcile_retry_in is None
    assert agent._reconcile_retry_attempts == 0


@pytest.mark.asyncio
async def test_deferred_disk_sweep_backs_off_instead_of_polling_every_heartbeat(
    workspace, monkeypatch, caplog
):
    """M1: the retry is bounded, not a 5s busy loop.

    With the shortfall never repaired, the enumeration is attempted 4 times
    in 12 heartbeat rounds -- rounds 1 and 2 (registration plus the pending
    flag) and then the doubling backoff (rounds 4 and 8) -- instead of every
    round, and the wait between attempts grows 1, 2, 4, 8 intervals.
    """
    _nodes, registry, control_app = _stack(workspace)
    _control_record(registry, "node_ghost", "sbx_ghost_record")
    orphan_id = "sbx_unowned_backoff"
    orphan_dir = _tree(workspace, orphan_id, project_id=4201)
    quota = _QuotaFake({4201: 8})
    quota.install(monkeypatch)
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    agent._node_id = None
    rounds = 0
    attempt_rounds: list[int] = []
    parked = asyncio.Event()

    async def hook(interval):
        nonlocal rounds
        rounds += 1
        deferred = [
            message
            for message in _agent_messages(caplog)
            if message.startswith("reconcile: disk sweep deferred")
        ]
        if len(deferred) > len(attempt_rounds):
            attempt_rounds.append(rounds)
        if rounds >= 12:
            parked.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(agent._loop())
    _patch_loop_transport(monkeypatch, control_app, driven=task, agent=agent)
    _patch_loop_sleep(monkeypatch, hook, driven=task, agent=agent)
    try:
        await asyncio.wait_for(parked.wait(), timeout=30)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert attempt_rounds == [1, 2, 4, 8]
    assert [
        message
        for message in _agent_messages(caplog)
        if message.startswith("reconcile: disk sweep deferred")
    ] == [
        "reconcile: disk sweep deferred by an incomplete fleet enumeration; "
        f"retrying in {delay} heartbeat interval(s) (attempt {attempt})"
        for attempt, delay in ((1, 1), (2, 2), (3, 4), (4, 8))
    ]
    # Nothing was touched while the fleet's records stayed incomplete.
    assert orphan_dir.exists()
    assert quota.rows == {4201: 8}
    assert quota.reconcile_calls == []


@pytest.mark.asyncio
async def test_the_heartbeat_hook_counts_only_the_loop_under_test(
    workspace, monkeypatch
):
    """W5: the driven loop's rounds are its own, whatever else heartbeats.

    ``envd_service.agent.asyncio`` is a *module* attribute, so the seam the
    two contracts above install reaches every ``NodeAgent`` loop in the
    process. A full directory has a second agent heartbeating next door: the
    session-scoped ``multinode_servers`` harness (booted by
    ``test_command_logs.py``) leaves real worker agents in uvicorn's threads
    for the rest of the session, and while their waits counted as rounds the
    M1 contracts went red only when the whole directory ran
    (``attempt_rounds=[4, 10]`` instead of ``[1, 2, 4, 8]``, with another
    node id's heartbeat traffic in the same log).

    Three loops are alive here -- the one under test, a second ``NodeAgent``
    in this event loop, and a third in its own thread -- and the hook sees
    exactly the driven loop's four heartbeats.
    """
    nodes, _registry, control_app = _stack(workspace)
    driven_rounds: list[int] = []
    driven = _agent(workspace, metrics_provider=_tick(driven_rounds))
    driven._reconcile_pending = False  # this contract counts heartbeats only
    in_loop_rounds: list[int] = []
    first_in_loop_round = asyncio.Event()

    def in_loop_metrics() -> dict:
        in_loop_rounds.append(len(in_loop_rounds) + 1)
        first_in_loop_round.set()
        return {}

    foreign_in_loop = asyncio.create_task(
        _foreign_agent(workspace, in_loop_metrics)._loop()
    )
    await asyncio.wait_for(first_in_loop_round.wait(), timeout=30)
    foreign_thread = _ForeignHeartbeat(workspace)
    foreign_thread.start()
    await foreign_thread.wait_for_first_round()

    rounds = 0
    counted_from: set[object] = set()
    parked = asyncio.Event()

    async def hook(interval):
        nonlocal rounds
        rounds += 1
        counted_from.add(asyncio.current_task())
        if rounds >= 4:
            parked.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(driven._loop())
    _patch_loop_transport(monkeypatch, control_app, driven=task, agent=driven)
    _patch_loop_sleep(monkeypatch, hook, driven=task, agent=driven)
    try:
        await asyncio.wait_for(parked.wait(), timeout=30)
    finally:
        for pending in (task, foreign_in_loop):
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        foreign_thread.stop()

    # The hook fired once per heartbeat of the loop under test...
    assert rounds == 4
    assert counted_from == {task}
    assert driven_rounds == [1, 2, 3, 4]
    # ...while both foreign loops really were heartbeating in this process,
    # each on its own wait, and neither was re-pointed at this test's control
    # plane.
    assert len(in_loop_rounds) >= 1
    assert foreign_thread.rounds >= 1
    assert sorted(node.node_id for node in nodes.list()) == ["node_a"]


@pytest.mark.asyncio
async def test_legacy_record_without_created_at_uses_the_sandbox_json_mtime(
    workspace, monkeypatch
):
    """M2: a pre-``created_at`` tree is old, not a concurrent create.

    The field only exists since 2026-09-02, so trees written before it parse
    with the dataclass default (the time of the read) and every one of them
    looks like a create that raced the reconcile window -- pinned forever.
    The fallback is the ``sandbox.json`` mtime, which is what the disk
    actually knows about the tree.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_legacy_no_created_at"
    legacy_mtime = 1_600_000_000.0
    sandbox_dir = _tree(
        workspace,
        sandbox_id,
        project_id=8001,
        created_at=None,
        mtime=legacy_mtime,
    )
    quota = _QuotaFake({8001: 8})
    quota.install(monkeypatch)

    agent = _agent(workspace)
    assert agent._runtime_registry.peek(sandbox_id).created_at == legacy_mtime
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert sandbox_dir.exists() is False
    assert quota.rows == {}
    assert summary["deleted"] == [sandbox_id]
    assert summary["concurrent_creates"] == []
    assert summary["quota_cleaned"] == [8001]
    assert summary["untrusted_records"] == []


@pytest.mark.asyncio
async def test_legacy_shaped_record_with_a_fresh_tree_is_still_a_concurrent_create(
    workspace, monkeypatch
):
    """M2 counter-case: the mtime fallback must not open a delete window.

    A record without ``created_at`` whose file is newer than the reconcile
    snapshot is a create that raced the round, and its tree has to be kept
    and reported back exactly like the keyed case next door.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_legacy_fresh"
    fresh_mtime = time.time() + 60
    sandbox_dir = _tree(
        workspace,
        sandbox_id,
        project_id=8002,
        created_at=None,
        mtime=fresh_mtime,
    )
    quota = _QuotaFake({8002: 8})
    quota.install(monkeypatch)

    agent = _agent(workspace)
    assert agent._runtime_registry.peek(sandbox_id).created_at == fresh_mtime
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert sandbox_dir.exists()
    assert (sandbox_dir / "sandbox.json").is_file()
    assert quota.rows == {8002: 8}
    assert summary["deleted"] == []
    assert summary["concurrent_creates"] == [sandbox_id]


@pytest.mark.asyncio
async def test_tree_teardown_does_not_block_the_event_loop(
    workspace, monkeypatch, disk_read_backend
):
    """M3: the per-tree teardown runs off the event loop -- structurally.

    Every tree's teardown releases its project state through the
    (synchronous) quota agent and then removes the tree. Measured in the
    loop, six trees with a 250ms release stall the whole worker for 1.5s --
    long enough for the control plane's 15s heartbeat timeout to orphan a
    node's live sandboxes once a shared workspace holds dozens of trees.

    The contract is therefore *where the work runs*, not how quickly the
    loop comes back (W8). A wall-clock bound measures the host: the same
    correct code produced 0.10-0.22s loop gaps on a loaded machine against a
    0.1s threshold, i.e. it reported "this box is busy", never "the teardown
    is in the loop" -- while a thread identity is precisely what a
    regression changes. Both halves of the heavy step (the quota release and
    the removal of the tree) record the thread they ran on, and neither may
    be the thread that runs this loop's callbacks.
    """
    _nodes, _registry, control_app = _stack(workspace)
    ids = [f"sbx_slow_release_{index}" for index in range(6)]
    mapping: dict[Path, int] = {}
    for index, sandbox_id in enumerate(ids):
        mapping[_tree(workspace, sandbox_id, project_id=9000 + index)] = 9000 + index
    quota = _QuotaFake({9000 + index: 8 for index in range(len(ids))})
    quota.release_delay_s = 0.25
    quota.install(monkeypatch)
    _install_disk_projids(monkeypatch, mapping, backend=disk_read_backend)

    agent = _agent(workspace)
    loop = asyncio.get_running_loop()
    # The thread that runs this loop's callbacks, recorded by one of its own
    # callbacks: the contract is about *this* loop's thread, not a hard-coded
    # thread name and not "whichever thread the test body happens to be on".
    loop_threads: list[int] = []
    loop.call_soon(lambda: loop_threads.append(threading.get_ident()))
    await asyncio.sleep(0)
    assert len(loop_threads) == 1
    loop_thread_id = loop_threads[0]

    # The teardown's other half: the tree itself. ``remove_tree`` is the
    # module-level entry the product calls, so wrapping it observes the
    # thread the removal ran on without touching the product.
    real_remove_tree = priv_helpers.remove_tree
    remove_threads: list[int] = []

    def _recording_remove_tree(path, **kwargs):
        remove_threads.append(threading.get_ident())
        return real_remove_tree(path, **kwargs)

    monkeypatch.setattr(priv_helpers, "remove_tree", _recording_remove_tree)

    started = loop.time()
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())
    elapsed = loop.time() - started

    assert summary["deleted"] == sorted(ids)
    # The work really happened: six releases (1.5s of stalls in production)
    # and six removals, none of them left out by this round.
    assert len(quota.released) == len(ids)
    assert len(quota.release_threads) == len(ids)
    assert len(remove_threads) == len(ids)
    assert elapsed >= 1.0
    # And it happened off the loop: a teardown put back into the loop is the
    # only thing that makes either list non-empty, whatever the host is doing
    # at the time.
    assert [
        thread for thread in quota.release_threads if thread == loop_thread_id
    ] == []
    assert [thread for thread in remove_threads if thread == loop_thread_id] == []


@pytest.mark.asyncio
async def test_record_pointing_at_another_tree_is_refused(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M4: a rewritten ``sandbox.json`` cannot aim the sweep at another tree.

    The record found in ``sbx_rewritten`` names a different sandbox and
    points ``workspace_dir``/``project_id``/``volume_projects`` at a live
    tenant's tree and quota rows. The victim is whole afterwards: its tree,
    its record and both of its quota rows. The rewritten tree itself is left
    alone too -- a record that does not describe its own directory is
    evidence of nothing, and the sweep never guesses.
    """
    _nodes, registry, control_app = _stack(workspace, nodes=("node_a", "node_b"))
    victim_id = "sbx_victim_tree"
    _control_record(registry, "node_b", victim_id)
    victim_dir = _tree(
        workspace,
        victim_id,
        project_id=VICTIM_PROJID,
        volume_projids=(VICTIM_VOLUME_PROJID,),
    )
    tamper_id = "sbx_rewritten"
    tamper_dir = workspace / tamper_id
    (tamper_dir / "workspace").mkdir(parents=True)
    victim_slice = victim_dir / "volumes" / f"vol_id_{VICTIM_VOLUME_PROJID}"
    (tamper_dir / "sandbox.json").write_text(
        json.dumps(
            {
                "sandbox_id": victim_id,
                "access_token": "tok",
                "workspace_dir": str(victim_dir),
                "created_at": 1_600_000_000.0,
                "project_id": VICTIM_PROJID,
                "volume_projects": [
                    {
                        "volume_id": f"vol_id_{VICTIM_VOLUME_PROJID}",
                        "sandbox_id": victim_id,
                        "mount_path": "/mnt/vol",
                        "sandbox_dir": str(victim_slice / victim_id),
                        "projid": VICTIM_VOLUME_PROJID,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    quota = _QuotaFake({VICTIM_PROJID: 8, VICTIM_VOLUME_PROJID: 4})
    quota.install(monkeypatch)
    _install_disk_projids(
        monkeypatch, {victim_dir: VICTIM_PROJID}, backend=disk_read_backend
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert victim_dir.exists()
    assert (victim_dir / "sandbox.json").is_file()
    assert registry.get(victim_id).state == "running"
    assert registry.get(victim_id).node_id == "node_b"
    assert quota.rows == {VICTIM_PROJID: 8, VICTIM_VOLUME_PROJID: 4}
    assert quota.released == []
    assert tamper_dir.exists()
    assert summary["deleted"] == []
    assert summary["untrusted_records"] == [tamper_id]
    assert summary["disk_sweep_skipped"] == []
    assert _agent_messages(caplog) == [
        f"reconcile: leaving {tamper_id} on disk: its sandbox.json names "
        f"sandbox {victim_id!r}",
    ]


@pytest.mark.asyncio
async def test_record_whose_project_id_contradicts_the_disk_is_refused(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M4: the project id is read from the disk, not from the record.

    The tree's directory reports project id 7001 while its record claims the
    victim's id. Nothing may release the claimed row, so the tree is left
    alone and the contradiction is reported.
    """
    _nodes, registry, control_app = _stack(workspace, nodes=("node_a", "node_b"))
    victim_id = "sbx_project_row_victim"
    _control_record(registry, "node_b", victim_id)
    victim_dir = _tree(workspace, victim_id, project_id=VICTIM_PROJID)
    liar_id = "sbx_project_row_liar"
    liar_dir = workspace / liar_id
    (liar_dir / "workspace").mkdir(parents=True)
    (liar_dir / "sandbox.json").write_text(
        json.dumps(
            {
                "sandbox_id": liar_id,
                "access_token": "tok",
                "workspace_dir": str(liar_dir),
                "created_at": 1_600_000_000.0,
                "project_id": VICTIM_PROJID,
            }
        ),
        encoding="utf-8",
    )
    quota = _QuotaFake({7001: 8, VICTIM_PROJID: 8})
    quota.install(monkeypatch)
    _install_disk_projids(
        monkeypatch,
        {liar_dir: 7001, victim_dir: VICTIM_PROJID},
        backend=disk_read_backend,
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert quota.released == []
    assert quota.rows == {7001: 8, VICTIM_PROJID: 8}
    assert liar_dir.exists()
    assert victim_dir.exists()
    assert summary["deleted"] == []
    assert summary["untrusted_records"] == [liar_id]
    assert _agent_messages(caplog) == [
        f"reconcile: leaving {liar_id} on disk: its sandbox.json claims "
        f"project id {VICTIM_PROJID} but the disk says 7001",
    ]


@pytest.mark.asyncio
async def test_volume_entry_naming_another_sandbox_slice_is_refused(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M4: volume slices are anchored to this sandbox and the volume root.

    Two rewritten entries: one names another sandbox's slice (the existing
    slice guard only checks the entry's own ``sandbox_id``, so it would
    otherwise pass), one is named after this sandbox but sits outside the
    worker's shared volume root. Neither slice is touched and neither row is
    released; the tree itself -- whose target *is* verified -- is reclaimed.
    """
    _nodes, registry, control_app = _stack(workspace, nodes=("node_a", "node_b"))
    volume_root = workspace / "_volume-root"
    outside_root = workspace / "_outside-root"
    victim_id = "sbx_slice_victim"
    _control_record(registry, "node_b", victim_id)
    # The victim's own record is what keeps its rows "recorded" for the quota
    # reconcile; without it the fail-safe pass would reclaim them the moment
    # the (rewritten) record naming them disappeared.
    victim_dir = _tree(
        workspace,
        victim_id,
        project_id=VICTIM_PROJID,
        volume_projids=(VICTIM_VOLUME_PROJID,),
    )
    victim_slice = volume_root / "vol-1" / victim_id
    victim_slice.mkdir(parents=True)
    liar_id = "sbx_slice_liar"
    liar_dir = workspace / liar_id
    (liar_dir / "workspace").mkdir(parents=True)
    outside_slice = outside_root / "vol-2" / liar_id
    outside_slice.mkdir(parents=True)
    (liar_dir / "sandbox.json").write_text(
        json.dumps(
            {
                "sandbox_id": liar_id,
                "access_token": "tok",
                "workspace_dir": str(liar_dir),
                "created_at": 1_600_000_000.0,
                "project_id": 7002,
                "volume_projects": [
                    {
                        "volume_id": "vol-1",
                        "sandbox_id": victim_id,
                        "mount_path": "/mnt/vol-1",
                        "sandbox_dir": str(victim_slice),
                        "projid": VICTIM_VOLUME_PROJID,
                    },
                    {
                        "volume_id": "vol-2",
                        "sandbox_id": liar_id,
                        "mount_path": "/mnt/vol-2",
                        "sandbox_dir": str(outside_slice),
                        "projid": 7003,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    quota = _QuotaFake(
        {VICTIM_PROJID: 8, VICTIM_VOLUME_PROJID: 4, 7002: 8, 7003: 4}
    )
    quota.install(monkeypatch)
    _install_disk_projids(
        monkeypatch,
        {liar_dir: 7002, victim_slice: VICTIM_VOLUME_PROJID},
        backend=disk_read_backend,
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace, shared_volume_root=str(volume_root))
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert victim_slice.exists()
    assert victim_dir.exists()
    # The victim's two rows survive; the liar's own two are reclaimed by the
    # fail-safe pass now that its tree (and its record) are gone.
    assert quota.rows == {VICTIM_PROJID: 8, VICTIM_VOLUME_PROJID: 4}
    # Only the liar's own, verified workspace project was released: neither
    # the victim's slice nor the slice outside the volume root was touched.
    assert quota.released == [(str(liar_dir), 7002)]
    assert liar_dir.exists() is False
    assert summary["deleted"] == [liar_id]
    assert summary["untrusted_records"] == []
    assert _agent_messages(caplog) == [
        f"reconcile: refusing a volume entry of {liar_id}: {victim_slice} is "
        "not a slice of this sandbox",
        f"reconcile: refusing the volume slice {outside_slice} of {liar_id}: "
        f"it is outside the shared volume root {volume_root}",
        f"reconcile: removing orphan runtime {liar_id} (not in control plane)",
    ]


@pytest.mark.asyncio
async def test_teardown_without_a_readable_project_id_still_reclaims_the_tree(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M4 / production shape: a disk the worker cannot ask degrades safely.

    Where the workspace is NFS-mounted and only the quota agent can read the
    project ids (no ``lsattr`` here), the record's claim must not be used.
    The tree is still reclaimed and its row is dropped by the fail-safe quota
    reconcile once the tree is gone: reclaimed, just not by a release the
    worker cannot verify.

    "The worker cannot ask the disk" has one shape per backend, so the line
    the operator sees names whichever mechanism this run is on: ``lsattr``
    failing, or the fd backend's own failure. Both are
    :class:`ProjectQuotaError` -- "cannot ask the disk", not "there is no
    project id" -- which is what keeps the claim out of the release set.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_unreadable_projid"
    sandbox_dir = _tree(workspace, sandbox_id, project_id=8301)
    quota = _QuotaFake({8301: 8})
    quota.install(monkeypatch)
    read_failure = "simulated read failure"
    _install_disk_projids(
        monkeypatch,
        {},
        backend=disk_read_backend,
        failure=read_failure,
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert sandbox_dir.exists() is False
    assert quota.released == []
    assert quota.rows == {}
    assert summary["deleted"] == [sandbox_id]
    assert summary["quota_cleaned"] == [8301]
    assert summary["quota_unreclaimed"] == []
    mechanism = {
        "lsattr": f"lsattr failed for {sandbox_dir}",
        "quotactl": f"the fd backend failed on {sandbox_dir}",
    }[disk_read_backend]
    assert _agent_messages(caplog) == [
        f"reconcile: cannot ask the disk for the project id of {sandbox_dir} "
        f"({mechanism}: {read_failure}); "
        "reclaiming it without releasing its quota row",
        f"reconcile: removing orphan runtime {sandbox_id} (not in control plane)",
    ]


@pytest.mark.asyncio
async def test_in_memory_record_cached_from_a_rewritten_json_is_refused(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """M4: an in-memory record is only as trustworthy as where it came from.

    Any request that calls ``RuntimeRegistry.get()`` caches the record it
    reads out of the sandbox-writable ``sandbox.json``. The reconcile window
    (the control plane deleted this sandbox while the worker was unreachable)
    then makes that cached record an "orphan this process owns" -- the same
    cross-tenant reach as the disk path, so the same verified targets apply.
    The rewritten record keeps its own id (that is the shape the fleet guard
    cannot filter on: the id really is unowned) and moves ``workspace_dir``
    and ``project_id`` onto a live tenant's tree instead -- the victim's tree
    and its quota row stay whole, and the rewritten tree is reported rather
    than acted on.
    """
    _nodes, registry, control_app = _stack(workspace, nodes=("node_a", "node_b"))
    victim_id = "sbx_cached_victim"
    _control_record(registry, "node_b", victim_id)
    victim_dir = _tree(workspace, victim_id, project_id=VICTIM_PROJID)
    tamper_id = "sbx_cached_rewrite"
    tamper_dir = workspace / tamper_id
    (tamper_dir / "workspace").mkdir(parents=True)
    (tamper_dir / "sandbox.json").write_text(
        json.dumps(
            {
                "sandbox_id": tamper_id,
                "access_token": "tok",
                "workspace_dir": str(victim_dir),
                "created_at": 1_600_000_000.0,
                "project_id": VICTIM_PROJID,
            }
        ),
        encoding="utf-8",
    )
    quota = _QuotaFake({VICTIM_PROJID: 8})
    quota.install(monkeypatch)
    _install_disk_projids(
        monkeypatch, {victim_dir: VICTIM_PROJID}, backend=disk_read_backend
    )
    caplog.set_level(logging.WARNING)
    caplog.clear()

    agent = _agent(workspace)
    # A request for the rewritten sandbox caches its record, exactly like the
    # proxied command/file paths do.
    cached = agent._runtime_registry.get(tamper_id)
    assert cached.sandbox_id == tamper_id
    assert cached.workspace_dir == str(victim_dir)
    assert [record.sandbox_id for record in agent._runtime_registry.list()] == [
        tamper_id
    ]
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert victim_dir.exists()
    assert (victim_dir / "sandbox.json").is_file()
    assert registry.get(victim_id).state == "running"
    assert quota.rows == {VICTIM_PROJID: 8}
    assert quota.released == []
    assert tamper_dir.exists()
    assert summary["deleted"] == []
    assert summary["untrusted_records"] == [tamper_id]
    assert _agent_messages(caplog) == [
        f"reconcile: leaving {tamper_id} on disk: its sandbox.json points at "
        f"{victim_dir}",
    ]


@pytest.mark.asyncio
async def test_in_memory_orphan_is_torn_down_from_the_verified_target(
    workspace, monkeypatch, disk_read_backend
):
    """The E6.1 in-memory semantics survive the M4 hardening.

    A runtime this worker registered itself and the control plane no longer
    knows is still torn down -- but from ``<workspace_base>/<id>`` and the
    project id the disk reports, which for an honest record is exactly what
    the record said.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_in_memory_orphan"
    sandbox_dir = _tree(workspace, sandbox_id, project_id=8101)
    quota = _QuotaFake({8101: 8})
    quota.install(monkeypatch)
    _install_disk_projids(
        monkeypatch, {sandbox_dir: 8101}, backend=disk_read_backend
    )

    agent = _agent(workspace)
    agent._runtime_registry.register(
        sandbox_id=sandbox_id,
        access_token="tok",
        workspace_dir=str(sandbox_dir),
        project_id=8101,
    )
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert sandbox_dir.exists() is False
    assert quota.released == [(str(sandbox_dir), 8101)]
    assert quota.rows == {}
    # N12: the teardown also reset the limits, which is what drops the row when
    # the usage is already zero -- the reconciliation below therefore has
    # nothing left to clean (that is what `quota_cleaned == []` says too).
    assert quota.cleared == [(str(workspace), 8101)]
    assert summary["deleted"] == [sandbox_id]
    assert summary["untrusted_records"] == []
    assert summary["quota_cleaned"] == []


def _gone_reason(path: Path) -> str:
    """The exact reason ``os.open`` gives for a path that is not there."""
    try:
        os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError as probe:
        return str(probe)
    raise AssertionError(f"{path} must not exist")  # pragma: no cover


def _agent_lines(caplog) -> list[tuple[str, str]]:
    """The agent's own lines, in order.

    ``caplog`` is a handler on the root logger: besides this worker's lines it
    also records ``httpx`` at INFO, other ``envd_service`` loggers (the disk
    watermark warning fires on a nearly full host disk) and -- in a
    full-directory run -- the session-scoped harness workers next door. Only
    the ``envd_service.agent`` records of *this* thread are this worker's
    (``_OnlyThisThread`` scopes the capture).
    """
    return [
        (record.levelname, record.message)
        for record in caplog.records
        if record.name == "envd_service.agent"
    ]


def _agent_messages(caplog) -> list[str]:
    """``_agent_lines`` without the level, for the exact-list contracts."""
    return [message for _level, message in _agent_lines(caplog)]


@pytest.mark.asyncio
async def test_a_slice_deleted_before_the_sweep_is_info_not_a_warning(
    workspace, monkeypatch, caplog, disk_read_backend
):
    """follow-up 1: the 2026-09-12 production shape, end to end.

    ``DELETE /volumes/<id>`` rmtree'd the volume root -- and the slices inside
    it -- 5h40m before worker-1 started, so the teardown asked the disk about
    six slice directories that no longer existed. The read failed with
    ENOENT, the old gate reported that as "this backend does not work", the
    ``lsattr`` fallback failed because the image has no e2fsprogs, and the
    box logged 12 WARNINGs that read like a permission problem.

    The corrected contract: the line is INFO and says the path is gone,
    ``lsattr`` is never consulted, the tree is still reclaimed, no release is
    made for the unverifiable slice (fail-safe), and the row the record
    claimed is reclaimed by the fail-safe quota pass once the tree -- and with
    it the record -- is gone.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_slice_gone"
    sandbox_dir = _tree(
        workspace, sandbox_id, project_id=8401, volume_projids=(8402,)
    )
    slice_dir = sandbox_dir / "volumes" / "vol_id_8402" / sandbox_id
    assert slice_dir.exists() is False          # the production shape
    quota = _QuotaFake({8401: 8, 8402: 4})
    quota.install(monkeypatch)
    disk = _install_disk_projids(
        monkeypatch, {sandbox_dir: 8401}, backend=disk_read_backend
    )
    caplog.set_level(logging.INFO)
    caplog.clear()

    agent = _agent(workspace, shared_volume_root=str(sandbox_dir / "volumes"))
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert sandbox_dir.exists() is False
    # Only the tree's own, verified project was released: the slice had no
    # verified (directory, projid) pair to release.
    assert quota.released == [(str(sandbox_dir), 8401)]
    assert quota.rows == {}
    assert summary == {
        "deleted": [sandbox_id],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [8402],
        "quota_unreclaimed": [],
    }
    assert _agent_lines(caplog) == [
        (
            "INFO",
            f"reconcile: {slice_dir} is gone from the disk "
            f"({_gone_reason(slice_dir)}); nothing to verify, its quota row is "
            "left to the fail-safe reconcile",
        ),
        (
            "WARNING",
            f"reconcile: removing orphan runtime {sandbox_id} (not in control plane)",
        ),
    ]
    # The production line came from the fallback: a path that is not there
    # must never reach it. The tree's own, readable path legitimately does,
    # and it is the only path ``lsattr`` is asked about -- the fd form asks
    # the gone slice too and fails on its ``open``, which is the same ENOENT
    # the path-state check answers with on this side (see
    # ``DiskProjids._install_quotactl``).
    reads = [sandbox_dir] if disk_read_backend == "lsattr" else [
        sandbox_dir,
        slice_dir,
    ]
    assert disk.calls == read_calls(disk_read_backend, *reads)


@pytest.mark.asyncio
async def test_a_slice_this_worker_may_not_read_keeps_its_own_warning(
    workspace, monkeypatch, caplog
):
    """follow-up 1: EACCES stays a WARNING and the caller semantics stay put.

    A slice that exists but cannot be read is a real anomaly (a tenant
    chmod'ing its own slice 0700, a 0710 directory): the line stays a WARNING
    and names the reason, so it is never confused with a deleted slice or
    with a host that cannot ask the disk. The fail-safe semantics are
    unchanged: no release without a verified (directory, projid) pair, the
    tree is still reclaimed, and the unverifiable claim is still reported.
    """
    _nodes, _registry, control_app = _stack(workspace)
    sandbox_id = "sbx_slice_unreadable"
    sandbox_dir = _tree(
        workspace, sandbox_id, project_id=8501, volume_projids=(8502,)
    )
    slice_dir = sandbox_dir / "volumes" / "vol_id_8502" / sandbox_id
    slice_dir.mkdir(parents=True)
    denial = PermissionError(
        errno.EACCES, os.strerror(errno.EACCES), str(slice_dir)
    )
    quota = _QuotaFake({8501: 8, 8502: 4})
    quota.install(monkeypatch)
    monkeypatch.setattr(xfs_quota, "_use_quotactl_read", lambda mount: True)
    monkeypatch.setattr(
        xfs_quota,
        "_directory_read_error",
        lambda directory: denial if Path(directory) == slice_dir else None,
    )

    def read_back(path):
        path = Path(path)
        if path == slice_dir:
            raise xfs_quotactl.QuotactlError(f"cannot open {path}: {denial}")
        return 8501

    monkeypatch.setattr(xfs_quotactl, "projid_of", read_back)
    caplog.set_level(logging.INFO)
    caplog.clear()

    agent = _agent(workspace, shared_volume_root=str(sandbox_dir / "volumes"))
    async with _client(control_app) as raw:
        summary = await agent._reconcile_with_control_plane(raw, _headers())

    assert sandbox_dir.exists() is False
    # No release for the slice, the tree's own verified project released, and
    # the row reclaims through the fail-safe pass.
    assert quota.released == [(str(sandbox_dir), 8501)]
    assert quota.rows == {}
    assert summary == {
        "deleted": [sandbox_id],
        "delete_failures": [],
        "unmaterialised": [],
        "protected_elsewhere": [],
        "disk_sweep_skipped": [],
        "untrusted_records": [],
        "concurrent_creates": [],
        "quota_cleaned": [8502],
        "quota_unreclaimed": [],
    }
    assert _agent_lines(caplog) == [
        (
            "WARNING",
            f"reconcile: {slice_dir} exists but this worker cannot read it "
            f"({denial}); reclaiming it without releasing its quota row",
        ),
        (
            "WARNING",
            f"reconcile: removing orphan runtime {sandbox_id} (not in control plane)",
        ),
    ]
