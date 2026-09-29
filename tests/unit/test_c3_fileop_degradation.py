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
