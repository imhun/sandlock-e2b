"""C3 Task 4 / second review: what the agent shape does when the CP is not there.

The first wave turned four privileged steps into control-plane round trips. That
is the point of the shape, but each of these call sites had a *pre-existing*
contract about being unable to measure or hand something over, and a round trip
that fails must land inside that contract -- not on top of it:

* ``/metrics`` (N2): a walk that blocks the event loop, and a diagnostics call
  that used to answer a number;
* the shared volume root (N3): a documented best-effort step;
* a checkpoint image's size and the tree-size scan (N4): reads that already have
  an "unknown" answer.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from pathlib import Path

import httpx
import pytest

from envd_service import agent_fileops, volumes
from envd_service.agent_fileops import AgentFileOpsError
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime import checkpoint_store
from envd_service.runtime.registry import RuntimeRegistry

SANDBOX = "sbx_degrade"
UID_X = 10007


class _FailingClient:
    """An agent client that cannot reach the control plane."""

    def __init__(self, verb: str = "workspace_bytes") -> None:
        self.calls: list[tuple] = []
        self._verb = verb

    def _refuse(self, op: str, *args, **kwargs):
        self.calls.append((op, args, kwargs))
        raise AgentFileOpsError(
            f"the control plane is unreachable for {op}: connection refused"
        )

    def workspace_bytes(self, sandbox_id: str):
        self._refuse("walk-workspace", sandbox_id)

    def checkpoint_bytes(self, sandbox_id: str):
        self._refuse("walk-checkpoint", sandbox_id)

    def chown_volume_root(self, sandbox_id: str, volume: str) -> None:
        self._refuse("chown-volume-root", sandbox_id, volume)


@pytest.fixture()
def install_client(monkeypatch):
    def _install(client):
        monkeypatch.setattr(agent_fileops, "_ACTIVE", [client])
        return client

    return _install


# ------------------------------------------------------------------- N2: /metrics


async def test_the_metrics_walk_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A minute-long walk must not park the worker's loop (N2).

    In the agent shape ``_dir_size`` is an HTTP round trip whose deadline is the
    *file-op* one (minutes, because a big tree legitimately takes that long).
    Awaiting it inline stopped heartbeats and every sandbox API behind it.
    """
    from envd_service.http import health as health_module

    runtime_registry = RuntimeRegistry(tmp_path)
    workspace = tmp_path / SANDBOX
    workspace.mkdir()
    app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=runtime_registry,
    )
    runtime_registry.register(
        sandbox_id=SANDBOX,
        access_token="tok",
        workspace_dir=str(workspace),
        disk_mb=1024,
    )
    seen: list[str] = []
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []

    def _measure(path, *, sandbox_id=None):
        threads.append(threading.current_thread())
        return 1234

    monkeypatch.setattr(health_module, "_dir_size", _measure)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.get(
            "/metrics",
            headers={"E2b-Sandbox-Id": SANDBOX, "X-Access-Token": "tok"},
        )
    assert resp.status_code == 200
    assert resp.json()["disk"]["usedBytes"] == 1234
    # The assertion is the *thread*: a blocking call awaited inline would run on
    # the loop's own thread, which is the one this test body is on.
    assert len(threads) == 1
    assert threads[0] is not loop_thread


async def test_a_failing_metrics_walk_still_answers_the_number_it_can(
    tmp_path: Path, install_client
) -> None:
    """A reachable-but-refusing CP is a *number*, not a 500 (N2's second half)."""
    runtime_registry = RuntimeRegistry(tmp_path)
    workspace = tmp_path / SANDBOX
    workspace.mkdir()
    app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=runtime_registry,
    )
    runtime_registry.register(
        sandbox_id=SANDBOX,
        access_token="tok",
        workspace_dir=str(workspace),
        disk_mb=1024,
    )
    install_client(_FailingClient())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.get(
            "/metrics",
            headers={"E2b-Sandbox-Id": SANDBOX, "X-Access-Token": "tok"},
        )
    assert resp.status_code == 200
    # The tree is the worker's own in this fixture, so the in-process walk still
    # answers -- what must not happen is a 500.
    assert isinstance(resp.json()["disk"]["usedBytes"], int)


async def test_the_import_removal_runs_off_the_event_loop(
    tmp_path: Path, install_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2: an import over an existing tree must not park the worker's loop.

    ``agent_import_sandbox`` is ``async``, and the removal it does first is a
    synchronous control-plane round trip whose read budget is the *file-op* one
    (minutes: a whole tree comes off the disk). Inline it stopped heartbeats and
    every sandbox API on the worker -- the same class as ``/metrics``, and the
    reason the create and delete handlers already run their work in a thread.
    """
    import io
    import tarfile

    workspace_base = tmp_path / "workspaces"
    existing = workspace_base / SANDBOX
    existing.mkdir(parents=True)
    (existing / "stale.txt").write_text("old", encoding="utf-8")

    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []

    class _Recording:
        def remove_workspace(self, sandbox_id: str) -> None:
            threads.append(threading.current_thread())
            shutil.rmtree(workspace_base / sandbox_id, ignore_errors=True)

    install_client(_Recording())
    app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=workspace_base),
        runtime_registry=RuntimeRegistry(workspace_base),
    )
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        data = b"restored"
        info = tarfile.TarInfo("workspace/note.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post(
            f"/agent/sandboxes/{SANDBOX}/import",
            headers={"X-Internal-Key": "internal-key"},
            content=payload.getvalue(),
        )
    assert resp.status_code == 204
    assert len(threads) == 1
    assert threads[0] is not loop_thread
    assert (existing / "workspace" / "note.txt").read_text(encoding="utf-8") == "restored"


# ------------------------------------------------------- N3: volume root hand-over


def test_a_refused_volume_root_handover_is_best_effort_and_named(
    tmp_path: Path, install_client, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """N3: the agent branch must live inside the documented best-effort contract.

    ``AgentFileOpsError`` is a ``RuntimeError``, so the ``OSError`` arm the
    function was written with did not catch it and a *permissions-widening*
    step aborted the whole sandbox create. The step stays best-effort (the
    sandbox's own mount view is its slice, which is handed over separately and
    still fails closed), with the reason on record.
    """
    install_client(_FailingClient())
    volume_root = tmp_path / "_volumes" / "vol_a"
    volume_root.mkdir(parents=True)
    # Force the branch: only a root-owned shared root is handed over, and who
    # owns the fixture depends on the lane (root in the container, the runner
    # elsewhere) -- the predicate is the seam.
    monkeypatch.setattr(volumes, "_volume_root_needs_handover", lambda st: True)
    # ...and keep the ancestor-widening pass, which is a different best-effort
    # step with its own warning on hosts whose ``tmp`` is not ours to chmod.
    monkeypatch.setattr(volumes, "_ensure_traversable", lambda path: None)
    caplog.set_level(logging.WARNING, logger="envd_service.volumes")

    volumes._ensure_shared_volume_root(
        volume_root, UID_X, sandbox_id=SANDBOX, volume="vol_abc"
    )

    assert [
        record.message
        for record in caplog.records
        if record.name == "envd_service.volumes"
    ] == [
        f"cannot hand volume root {volume_root} to uid {UID_X} through the "
        "agent: the control plane is unreachable for chown-volume-root: "
        "connection refused",
    ]


# ------------------------------------------------------------- N4: diagnostics


def test_checkpoint_status_reports_unknown_bytes_as_zero(
    tmp_path: Path, install_client, caplog
) -> None:
    """A diagnostic that cannot measure says 0 with the reason (N4)."""
    image = checkpoint_store.checkpoint_image_dir(tmp_path, SANDBOX)
    image.mkdir(parents=True)
    (image / "img").write_bytes(b"x" * 4096)
    install_client(_FailingClient())
    caplog.set_level(logging.WARNING, logger="envd_service.runtime.checkpoint_store")

    status = checkpoint_store.checkpoint_status(tmp_path, SANDBOX)

    assert status["hasImage"] is True
    assert status["imageMB"] == 0
    assert [
        record.message
        for record in caplog.records
        if record.name == "envd_service.runtime.checkpoint_store"
    ] == [
        f"sandbox {SANDBOX}: cannot measure the checkpoint image: "
        "AgentFileOpsError: the control plane is unreachable for "
        "walk-checkpoint: connection refused",
    ]


def test_the_tree_size_scan_reports_unknown_instead_of_raising(
    tmp_path: Path, install_client, caplog
) -> None:
    """The heartbeat's scan answers "unknown" for a tree it cannot measure (N4)."""
    registry = RuntimeRegistry(tmp_path)
    workspace = tmp_path / SANDBOX
    workspace.mkdir()
    registry.register(
        sandbox_id=SANDBOX,
        access_token="tok",
        workspace_dir=str(workspace),
        disk_mb=1024,
    )
    install_client(_FailingClient())
    caplog.set_level(logging.WARNING, logger="envd_service.runtime.registry")

    assert registry.disk_usage_snapshot() == {}
    assert [
        record.message
        for record in caplog.records
        if record.name == "envd_service.runtime.registry"
    ] == [
        f"cannot measure {SANDBOX} through the agent: AgentFileOpsError: the "
        "control plane is unreachable for walk-workspace: connection refused",
    ]


# ------------------------------------------------ I-3: the platform's own account


def test_a_root_worker_reports_no_identity_and_says_so_once(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """m-1, worker side: a root worker stays joinable and the reason is logged.

    ``0`` is not a worker identity (the uid-pool gate refuses it, and the CP
    refuses the field by name), but registration and heartbeats are *not* file
    operations: reporting ``0`` would make the whole node unjoinable for a fact
    that only the file operations care about. So the pair is omitted -- every
    operation that needs it then fails closed at the CP with its own named 503 --
    and this line is what tells an operator why.
    """
    from envd_service import worker_identity

    monkeypatch.setattr(worker_identity.os, "geteuid", lambda: 0)
    monkeypatch.setattr(worker_identity.os, "getegid", lambda: 0)
    monkeypatch.setattr(worker_identity, "_ROOT_IDENTITY_DISCLOSED", False)
    caplog.set_level(logging.WARNING, logger="envd_service.worker_identity")

    assert worker_identity.worker_identity_fields() == {}
    # ...and once, not on every heartbeat.
    assert worker_identity.worker_identity_fields() == {}
    assert [
        record.message
        for record in caplog.records
        if record.name == "envd_service.worker_identity"
    ] == [
        "this worker runs as 0:0: it reports no worker identity (a non-zero "
        "uid/gid is what a sandbox tree's group and the agent's `--worker` form "
        "mean), so the control plane will refuse every C3 file operation on "
        "this node by name -- run the worker as 65534:65534 (the shipped "
        "image's USER) for the agent shape",
    ]


def test_a_normal_worker_still_reports_its_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from envd_service import worker_identity

    monkeypatch.setattr(worker_identity.os, "geteuid", lambda: 65534)
    monkeypatch.setattr(worker_identity.os, "getegid", lambda: 65534)
    assert worker_identity.worker_identity_fields() == {
        "workerUID": 65534,
        "workerGID": 65534,
    }


def test_a_child_the_worker_cannot_read_makes_the_account_unknown_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """I-3's silent-0 guard: an unreadable child is *named*, never billed as 0.

    ``<state>/_runtime/.checkpoints/<id>`` is ``0700`` owned by the sandbox uid.
    Pre-C3 the worker reached it through the broker; in the agent shape it has
    nobody to ask (routing that walk is a slice-B follow-up), and the walk's
    ``None`` used to be folded into "the platform stores nothing" -- which is
    what both the checkpoint admission and the ledger alert read as "there is
    room". A test that lets 0 come back is a test that lets the fail-open back.
    """
    from envd_service import priv_helpers
    from envd_service.runtime import platform_disk

    state = tmp_path / "state"
    runtime = state / "_runtime"
    # The child the walk cannot finish: the ``.checkpoints`` gate, whose store
    # (``.checkpoints/<id>``) is the ``0700`` directory owned by the sandbox.
    gate = runtime / ".checkpoints"
    store = gate / SANDBOX
    store.mkdir(parents=True)
    readable = runtime / "sbx_other"
    readable.mkdir()
    (readable / "sandbox.json").write_text("{}", encoding="utf-8")

    real_dir_size = priv_helpers.dir_size

    def _unreadable(path):
        if Path(path) == gate:
            return None  # what a walk the worker cannot make answers
        return real_dir_size(path)

    monkeypatch.setattr(priv_helpers, "dir_size", _unreadable)
    caplog.set_level(logging.WARNING, logger="envd_service.runtime.platform_disk")

    assert platform_disk.measure_platform_disk_bytes(tmp_path, state_base=state) is None
    assert [
        record.message
        for record in caplog.records
        if record.name == "envd_service.runtime.platform_disk"
    ] == [
        f"cannot measure {gate} (a child of {runtime}): reporting the platform "
        "disk account as unknown",
    ]
    # ...and the absence of a runtime dir is still a real 0: nothing is there.
    assert platform_disk.measure_platform_disk_bytes(tmp_path / "empty") == 0


def test_admission_and_the_pre_capture_check_refuse_an_unknown_account() -> None:
    """Unknown is never "there is room" (I-3), on both decisions."""
    from envd_service.runtime.platform_disk import (
        UNKNOWN_ACCOUNT_REASON,
        checkpoint_admission,
        checkpoint_no_room_reason,
    )

    limit = 100 * 1024 * 1024
    decision = UNKNOWN_ACCOUNT_REASON.format(
        decision="no image can be taken until the account can be measured"
    )
    assert checkpoint_admission(
        used_bytes=None, incoming_bytes=1, limit_bytes=limit
    ) == (False, decision)
    assert checkpoint_no_room_reason(used_bytes=None, limit_bytes=limit) == decision


def test_the_heartbeat_omits_an_unmeasurable_platform_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire value: omit, do not assert 0 (I-3).

    The CP's ``update_usage`` reads a missing field as "no update" and keeps the
    number it already has; a 0 would be a claim that the platform stores
    nothing.
    """
    from envd_service import agent as node_agent
    from envd_service.runtime import platform_disk

    monkeypatch.setattr(
        platform_disk, "measure_platform_disk_bytes", lambda *a, **k: None
    )
    monkeypatch.setenv("E2B_PLATFORM_DISK_MB", "4096")
    measured = node_agent._measure_platform_account(tmp_path)
    assert measured == {"platformDiskBudgetMB": 4096}


def test_a_measurable_account_still_reports_its_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other arm: nothing above may weaken the ordinary case."""
    from envd_service import agent as node_agent
    from envd_service.runtime import platform_disk

    monkeypatch.setattr(
        platform_disk,
        "measure_platform_disk_bytes",
        lambda *a, **k: 3 * 1024 * 1024,
    )
    monkeypatch.setenv("E2B_PLATFORM_DISK_MB", "4096")
    measured = node_agent._measure_platform_account(tmp_path)
    assert measured == {"platformDiskUsedMB": 3, "platformDiskBudgetMB": 4096}


class _UnknownSandboxClient:
    """An agent client the control plane answers with "no such sandbox"."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def workspace_bytes(self, sandbox_id: str):
        self.calls.append(("walk-workspace", sandbox_id))
        raise agent_fileops.AgentFileOpsUnknownSandbox(
            f"the control plane refused walk-workspace for sandbox {sandbox_id} "
            f"(HTTP 404): Sandbox {sandbox_id} not found"
        )


def _register_one_record(registry, workspace: Path) -> None:
    workspace.mkdir(exist_ok=True)
    registry.register(
        sandbox_id=SANDBOX,
        access_token="tok",
        workspace_dir=str(workspace),
        disk_mb=1024,
    )


def test_a_record_the_control_plane_does_not_know_is_dropped(
    tmp_path: Path, install_client, caplog
) -> None:
    """A worker record must not outlive the control plane's record.

    Measured on the fleet (2026-10-01): stale runtime records made *every* disk
    round ask the control plane to walk a sandbox it had already forgotten --
    ~5 requests/s of 404s on one worker, hours of it, and enough log volume to
    push the worker's own startup lines out of the container log. A *definite*
    "no such sandbox" is the one answer that must not be retried: nothing can
    ever authorize an operation on that id again, and the tree (if it is still
    there) belongs to the control plane's orphan GC, not to this record.
    """
    registry = RuntimeRegistry(tmp_path)
    _register_one_record(registry, tmp_path / SANDBOX)
    client = install_client(_UnknownSandboxClient())
    caplog.set_level(logging.WARNING, logger="envd_service.runtime.registry")

    assert registry.disk_usage_snapshot() == {}

    assert [record.message for record in caplog.records if record.name == "envd_service.runtime.registry"] == [
        f"the control plane has no record of {SANDBOX}: dropping this worker's "
        "runtime record (AgentFileOpsUnknownSandbox: the control plane refused "
        f"walk-workspace for sandbox {SANDBOX} (HTTP 404): Sandbox {SANDBOX} "
        "not found); its tree, if any, is left to the control plane's "
        "orphan-tree GC",
    ]
    # The claim is gone...
    assert registry.list() == []
    # ...and the next round does not ask again (that is the whole point: the
    # 404 storm stops here instead of repeating every scan).
    assert registry.disk_usage_snapshot() == {}
    assert client.calls == [("walk-workspace", SANDBOX)]


def test_a_control_plane_that_is_unreachable_keeps_the_record(
    tmp_path: Path, install_client, caplog
) -> None:
    """The contrast: a transport failure is retryable, so the record stays.

    Dropping a record because the control plane was briefly unreachable would
    turn a network blip into data the worker can never reclaim.
    """
    registry = RuntimeRegistry(tmp_path)
    _register_one_record(registry, tmp_path / SANDBOX)
    client = install_client(_FailingClient())
    caplog.set_level(logging.WARNING, logger="envd_service.runtime.registry")

    assert registry.disk_usage_snapshot() == {}
    assert [record.sandbox_id for record in registry.list()] == [SANDBOX]
    assert client.calls == [("walk-workspace", (SANDBOX,), {})]
