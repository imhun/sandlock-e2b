"""G1a: control-plane pause/resume delivery to a remote worker agent.

In a separated deployment the control plane has no shared runtime registry
with the worker, so pause/resume must be pushed over HTTP to the hosting
agent. This pins the delivery status mapping and the rollback semantics with
a real uvicorn stub worker:

* 204 -> delivered (local state kept);
* 404 -> treated as success (no live runtime to freeze/thaw);
* explicit non-404 errors -> local state rolled back and HTTP 502 surfaced;
* transport loss -> best-effort WARNING, HTTP 204 kept (documented caveat);
* ``Sandbox.connect`` auto-resume (the SDK's only public resume surface)
  pushes the same way and rolls back on explicit errors.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from control_plane.config import Settings as ControlSettings
from tests.conftest import _ServerThread, _free_port

API = {"X-API-Key": "local-key"}


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=1024,
        max_total_cpu_percent=200,
        max_total_disk_mb=2048,
        max_total_processes=128,
        # E9.4 isolation: these cases assert delivery semantics; the create
        # queue must not stall them.
        create_queue_timeout_s=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


class _StubWorker:
    """Minimal worker agent recording agent pause/resume/create calls."""

    def __init__(self) -> None:
        self.pause_status = 204
        self.resume_status = 204
        self.pause_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.create_calls: list[str] = []
        self.app = FastAPI()
        self._wire_routes()

    def _wire_routes(self) -> None:
        stub = self

        @self.app.post("/agent/sandboxes")
        async def _agent_create(request: Request) -> Response:
            body = await request.json()
            stub.create_calls.append(body.get("sandboxID", ""))
            return Response(status_code=201)

        @self.app.post("/agent/sandboxes/{sandbox_id}/pause")
        async def _agent_pause(sandbox_id: str) -> Response:
            stub.pause_calls.append(sandbox_id)
            return Response(status_code=stub.pause_status)

        @self.app.post("/agent/sandboxes/{sandbox_id}/resume")
        async def _agent_resume(sandbox_id: str) -> Response:
            stub.resume_calls.append(sandbox_id)
            return Response(status_code=stub.resume_status)

        @self.app.delete("/agent/sandboxes/{sandbox_id}")
        async def _agent_delete(sandbox_id: str) -> Response:
            return Response(status_code=204)


@pytest.fixture()
def make_remote_harness(make_apps):
    """Factory: control app + one registered remote worker stub agent."""
    servers: list[_ServerThread] = []

    def _make(
        *,
        control_settings: ControlSettings | None = None,
        **overrides,
    ) -> dict:
        control, _envd = make_apps(
            control_settings=control_settings or _settings(**overrides)
        )
        stub = _StubWorker()
        port = _free_port()
        server = _ServerThread(stub.app, port)
        server.start()
        try:
            control.state.nodes.register(
                node_id="worker-1",
                address=f"http://127.0.0.1:{port}",
                total_memory_mb=4096,
                total_cpu_percent=400,
                total_disk_mb=8192,
                total_processes=512,
            )
            control.state.nodes.remove("local")
        except BaseException:
            server.stop()
            raise
        servers.append(server)
        return {"control": control, "stub": stub, "server": server}

    yield _make
    for server in servers:
        server.stop()


@pytest.fixture()
def remote_harness(make_remote_harness):
    return make_remote_harness()


async def _create(control, sandbox_id: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        return await client.post(
            "/sandboxes",
            headers={**API, "X-Sandbox-Id": sandbox_id},
            json={"templateID": "base", "timeout": 300},
        )


async def _call(control, sandbox_id: str, action: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        return await client.post(
            f"/sandboxes/{sandbox_id}/{action}", headers=API, json={}
        )


async def _connect(control, sandbox_id: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        return await client.post(
            f"/sandboxes/{sandbox_id}/connect", headers=API, json={}
        )


def _ledgers(control, sandbox_id: str) -> tuple[int, int, str]:
    registry = control.state.registry
    node = control.state.nodes.get("worker-1")
    record = registry.get(sandbox_id)
    return (
        registry._reserved_memory,
        node.reserved_memory_mb,
        record.state,
    )


def _backdate(control, sandbox_id: str, *, seconds: int = 1200) -> None:
    """Make a record look idle (E9.1 test pattern; no real waiting)."""
    from datetime import timedelta

    from gateway_common.timeutil import utcnow

    record = control.state.registry.get(sandbox_id)
    record.last_active_at = utcnow() - timedelta(seconds=seconds)
    control.state.registry.save(record)


def _eviction_settings(**overrides) -> ControlSettings:
    merged = dict(
        eviction_prefer_pause=True,
        sandbox_idle_threshold_s=600,
        eviction_min_interval_s=0,
        # Bound the rollback retry loop: one explicit worker rejection is
        # enough to pin the rollback semantics.
        eviction_max_per_create=1,
    )
    merged.update(overrides)
    return _settings(**merged)


async def test_pause_remote_204_delivers_and_keeps_local_state(remote_harness):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    created = await _create(control, "sbx_remote_pause")
    assert created.status_code == 201
    assert _ledgers(control, "sbx_remote_pause") == (512, 512, "running")

    paused = await _call(control, "sbx_remote_pause", "pause")

    assert paused.status_code == 204
    assert stub.pause_calls == ["sbx_remote_pause"]
    assert _ledgers(control, "sbx_remote_pause") == (0, 0, "paused")


async def test_pause_remote_404_is_success_for_bookkeeping(remote_harness):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    await _create(control, "sbx_remote_pause_404")
    stub.pause_status = 404

    paused = await _call(control, "sbx_remote_pause_404", "pause")

    assert paused.status_code == 204
    assert stub.pause_calls == ["sbx_remote_pause_404"]
    assert _ledgers(control, "sbx_remote_pause_404") == (0, 0, "paused")


async def test_pause_remote_explicit_error_rolls_back_and_raises_502(
    remote_harness,
):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    await _create(control, "sbx_remote_pause_500")
    stub.pause_status = 500

    paused = await _call(control, "sbx_remote_pause_500", "pause")

    assert paused.status_code == 502
    assert paused.json()["code"] == 502
    assert stub.pause_calls == ["sbx_remote_pause_500"]
    assert _ledgers(control, "sbx_remote_pause_500") == (512, 512, "running")


async def test_pause_remote_transport_loss_is_best_effort_204(remote_harness):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    server = remote_harness["server"]
    await _create(control, "sbx_remote_pause_xport")
    assert stub.pause_calls == []
    server.stop()

    paused = await _call(control, "sbx_remote_pause_xport", "pause")

    assert paused.status_code == 204
    assert _ledgers(control, "sbx_remote_pause_xport") == (0, 0, "paused")


async def test_resume_remote_204_delivers_and_rebooks(remote_harness):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    await _create(control, "sbx_remote_resume")
    assert (await _call(control, "sbx_remote_resume", "pause")).status_code == 204
    assert stub.pause_calls == ["sbx_remote_resume"]

    resumed = await _call(control, "sbx_remote_resume", "resume")

    assert resumed.status_code == 204
    assert stub.resume_calls == ["sbx_remote_resume"]
    assert _ledgers(control, "sbx_remote_resume") == (512, 512, "running")


async def test_resume_remote_404_is_success(remote_harness):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    await _create(control, "sbx_remote_resume_404")
    assert (await _call(control, "sbx_remote_resume_404", "pause")).status_code == 204
    stub.resume_status = 404

    resumed = await _call(control, "sbx_remote_resume_404", "resume")

    assert resumed.status_code == 204
    assert stub.resume_calls == ["sbx_remote_resume_404"]
    assert _ledgers(control, "sbx_remote_resume_404") == (512, 512, "running")


async def test_resume_remote_explicit_error_rolls_back_and_raises_502(
    remote_harness,
):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    await _create(control, "sbx_remote_resume_500")
    assert (await _call(control, "sbx_remote_resume_500", "pause")).status_code == 204
    stub.resume_status = 500

    resumed = await _call(control, "sbx_remote_resume_500", "resume")

    assert resumed.status_code == 502
    assert resumed.json()["code"] == 502
    assert stub.resume_calls == ["sbx_remote_resume_500"]
    assert _ledgers(control, "sbx_remote_resume_500") == (0, 0, "paused")


async def test_resume_remote_transport_loss_is_best_effort_204(remote_harness):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    server = remote_harness["server"]
    await _create(control, "sbx_remote_resume_xport")
    assert (await _call(control, "sbx_remote_resume_xport", "pause")).status_code == 204
    server.stop()

    resumed = await _call(control, "sbx_remote_resume_xport", "resume")

    assert resumed.status_code == 204
    assert _ledgers(control, "sbx_remote_resume_xport") == (512, 512, "running")


async def test_connect_auto_resume_pushes_resume_to_remote_worker(remote_harness):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    await _create(control, "sbx_remote_connect")
    assert (await _call(control, "sbx_remote_connect", "pause")).status_code == 204
    assert stub.resume_calls == []

    connected = await _connect(control, "sbx_remote_connect")

    assert connected.status_code == 200
    assert stub.resume_calls == ["sbx_remote_connect"]
    assert _ledgers(control, "sbx_remote_connect") == (512, 512, "running")


async def test_connect_auto_resume_explicit_error_rolls_back_and_raises_502(
    remote_harness,
):
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    await _create(control, "sbx_remote_connect_500")
    assert (await _call(control, "sbx_remote_connect_500", "pause")).status_code == 204
    stub.resume_status = 500

    connected = await _connect(control, "sbx_remote_connect_500")

    assert connected.status_code == 502
    assert connected.json()["code"] == 502
    assert stub.resume_calls == ["sbx_remote_connect_500"]
    assert _ledgers(control, "sbx_remote_connect_500") == (0, 0, "paused")


async def test_eviction_prefer_pause_delivers_remote_victim_freeze(
    make_remote_harness,
):
    """An eviction-paused remote victim receives the agent pause push."""
    harness = make_remote_harness(control_settings=_eviction_settings())
    control = harness["control"]
    stub = harness["stub"]
    assert (await _create(control, "sbx_evict_victim")).status_code == 201
    assert (await _create(control, "sbx_evict_filler")).status_code == 201
    _backdate(control, "sbx_evict_victim")

    created = await _create(control, "sbx_evict_new")

    assert created.status_code == 201
    assert stub.pause_calls == ["sbx_evict_victim"]
    assert control.state.registry.count() == 3
    victim = control.state.registry.get("sbx_evict_victim")
    assert victim.state == "paused"
    assert victim.quota_released is True
    assert control.state.registry._reserved_memory == 1024


async def test_eviction_remote_pause_explicit_error_rolls_back_victim(
    make_remote_harness,
):
    """An explicit worker rejection rolls the evicted victim back to
    running so a later kill pass cannot destroy a live runtime."""
    harness = make_remote_harness(control_settings=_eviction_settings())
    control = harness["control"]
    stub = harness["stub"]
    assert (await _create(control, "sbx_evict_victim_500")).status_code == 201
    assert (await _create(control, "sbx_evict_filler_500")).status_code == 201
    _backdate(control, "sbx_evict_victim_500")
    stub.pause_status = 500

    created = await _create(control, "sbx_evict_new_500")

    assert created.status_code == 503
    assert stub.pause_calls == ["sbx_evict_victim_500"]
    victim = control.state.registry.get("sbx_evict_victim_500")
    assert victim.state == "running"
    assert victim.quota_released is False
    assert control.state.registry._reserved_memory == 1024
