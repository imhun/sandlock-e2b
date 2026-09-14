"""Control-plane remote network update rejection semantics (FUP #7).

A remote worker's explicit HTTP rejection is a *decision*, not a transport
loss: the update must fail closed (no 204, no persisted record) unless the
worker actually applied it. A worker 409 keeps HTTP 409; any other explicit
status >= 400 (404/500/...) surfaces as HTTP 502; only transport exceptions
keep the best-effort persist-and-warn caveat. The stub reproduces the real
worker shapes: a bare 404 (no live runtime on the node) and a JSON 409.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from control_plane.config import Settings as ControlSettings
from tests.conftest import _ServerThread, _bind_low_port

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
    """Minimal worker agent recording agent network-update calls."""

    def __init__(self) -> None:
        self.network_status = 204
        self.network_calls: list[str] = []
        self.app = FastAPI()
        self._wire_routes()

    def _wire_routes(self) -> None:
        stub = self

        @self.app.post("/agent/sandboxes")
        async def _agent_create(request: Request) -> Response:
            await request.json()
            return Response(status_code=201)

        @self.app.post("/agent/sandboxes/{sandbox_id}/network")
        async def _agent_network(sandbox_id: str, request: Request) -> Response:
            await request.json()
            stub.network_calls.append(sandbox_id)
            if stub.network_status == 409:
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": 409,
                        "message": (
                            "network egress model cannot change on a launched "
                            "sandbox (allowOut -> denyOut)"
                        ),
                    },
                )
            if stub.network_status == 404:
                # Real worker shape: no live runtime on this node.
                return Response(status_code=404)
            if stub.network_status >= 400:
                return Response(
                    status_code=stub.network_status,
                    content=f"worker exploded with HTTP {stub.network_status}",
                )
            return Response(status_code=204)

@pytest.fixture()
def make_remote_harness(make_apps):
    """Factory: control app + one registered remote worker stub agent."""
    servers: list[_ServerThread] = []

    def _make(**overrides) -> dict:
        control, _envd = make_apps(control_settings=_settings(**overrides))
        stub = _StubWorker()
        port, sock = _bind_low_port()
        server = _ServerThread(stub.app, port, sock=sock)
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


def _client(control):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    )


async def _create(control, sandbox_id: str):
    async with _client(control) as client:
        return await client.post(
            "/sandboxes",
            headers={**API, "X-Sandbox-Id": sandbox_id},
            json={
                "templateID": "base",
                "timeout": 300,
                "allow_internet_access": True,
            },
        )


async def _put_network(control, sandbox_id: str, body: dict):
    async with _client(control) as client:
        return await client.put(
            f"/sandboxes/{sandbox_id}/network", headers=API, json=body
        )


def _snapshot(control, sandbox_id: str) -> tuple[dict | None, bool | None]:
    record = control.state.registry.get(sandbox_id)
    return (
        dict(record.network) if record.network else None,
        record.allow_internet_access,
    )


async def test_remote_worker_404_rejection_surfaces_502_and_never_persists(
    remote_harness,
):
    """A bare worker 404 (no live runtime) is an explicit rejection: HTTP
    502 and no record change -- not a silent 204."""
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    created = await _create(control, "sbx_net_404")
    assert created.status_code == 201
    stub.network_status = 404
    before = _snapshot(control, "sbx_net_404")

    resp = await _put_network(
        control, "sbx_net_404", {"allowInternetAccess": False}
    )

    assert resp.status_code == 502
    assert resp.json() == {
        "code": 502,
        "message": "node worker-1 returned HTTP 404 for the network update",
    }
    assert stub.network_calls == ["sbx_net_404"]
    assert _snapshot(control, "sbx_net_404") == before


async def test_remote_worker_500_rejection_surfaces_502_and_never_persists(
    remote_harness,
):
    """A worker 500 is an explicit rejection: HTTP 502, record unchanged."""
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    created = await _create(control, "sbx_net_500")
    assert created.status_code == 201
    stub.network_status = 500
    before = _snapshot(control, "sbx_net_500")

    resp = await _put_network(
        control, "sbx_net_500", {"allowInternetAccess": False}
    )

    assert resp.status_code == 502
    assert resp.json() == {
        "code": 502,
        "message": "node worker-1 returned HTTP 500 for the network update",
    }
    assert stub.network_calls == ["sbx_net_500"]
    assert _snapshot(control, "sbx_net_500") == before


async def test_remote_worker_409_surfaces_409_and_never_persists(
    remote_harness,
):
    """A worker conflict keeps HTTP 409 with the worker's message; the
    record is not persisted."""
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    created = await _create(control, "sbx_net_409")
    assert created.status_code == 201
    stub.network_status = 409
    before = _snapshot(control, "sbx_net_409")

    resp = await _put_network(
        control, "sbx_net_409", {"allowInternetAccess": False}
    )

    assert resp.status_code == 409
    assert resp.json() == {
        "code": 409,
        "message": (
            "network egress model cannot change on a launched sandbox "
            "(allowOut -> denyOut)"
        ),
    }
    assert stub.network_calls == ["sbx_net_409"]
    assert _snapshot(control, "sbx_net_409") == before


async def test_remote_transport_loss_persists_with_warning_caveat(
    remote_harness,
):
    """Transport loss keeps the documented best-effort 204: the record is
    persisted even though the worker never confirmed the update."""
    control = remote_harness["control"]
    stub = remote_harness["stub"]
    server = remote_harness["server"]
    created = await _create(control, "sbx_net_xport")
    assert created.status_code == 201
    server.stop()
    before = _snapshot(control, "sbx_net_xport")
    assert before == (None, True)

    resp = await _put_network(
        control, "sbx_net_xport", {"allowInternetAccess": False}
    )

    assert resp.status_code == 204
    assert stub.network_calls == []
    assert _snapshot(control, "sbx_net_xport") == (
        {"allowInternetAccess": False},
        False,
    )
