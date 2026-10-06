"""E3: the control-plane checkpoint query is a *proxy*, and a read-only one.

The image and the last restore are facts about what the sandbox's own node did
(``docs/checkpoint-restore-e2b-half.md``; the trade is written into
:mod:`envd_service.runtime.checkpoint_store`), so the public endpoint asks that
node. What is pinned here is the delivery shape of that proxy, with a real
uvicorn stub worker:

* the node's answer is what the caller gets -- verbatim, no reshaping on the way
  through;
* a node that cannot be reached answers "nothing to report" plus
  ``unreachable``, and never turns a diagnostic question into an error (the
  endpoint must not become a new failure mode next to ``pause``/``resume``);
* nothing here writes: the sandbox record's state is the same before and after.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from control_plane.config import Settings as ControlSettings
from tests.conftest import _ServerThread, _bind_low_port

API = {"X-API-Key": "local-key"}

_WORKER_ANSWER = {
    "sandboxID": "sbx_proxy",
    "hasImage": True,
    "imageMB": 7,
    "capturedAt": 1780000000,
    "lastRestore": {
        "restored": True,
        "reason": "",
        "pid": 31337,
        "unrecoveredFdCount": 2,
        "at": "2026-09-26T00:00:00+00:00",
    },
}


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
    """Minimal worker agent: it answers the checkpoint query, nothing else."""

    def __init__(self) -> None:
        self.checkpoint_status = 200
        self.checkpoint_calls: list[str] = []
        self.app = FastAPI()
        self._wire_routes()

    def _wire_routes(self) -> None:
        stub = self

        @self.app.post("/agent/sandboxes")
        async def _agent_create(request: Request) -> Response:
            await request.json()
            return Response(status_code=201)

        @self.app.get("/agent/sandboxes/{sandbox_id}/checkpoint")
        async def _agent_checkpoint(sandbox_id: str, request: Request) -> Response:
            assert request.headers.get("X-Internal-Key") == "internal-key"
            stub.checkpoint_calls.append(sandbox_id)
            if stub.checkpoint_status != 200:
                return Response(status_code=stub.checkpoint_status)
            return JSONResponse(content={**_WORKER_ANSWER, "sandboxID": sandbox_id})


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
                # N83 phase 2 (R12): a worker on this build reports the
                # per-sandbox ceiling its creates are checked against (the
                # node's own totals, the D5 default).
                sandbox_ceiling={
                    "cpuPercent": 400,
                    "memoryMB": 4096,
                    "processes": 512,
                },
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


async def _create(control, sandbox_id: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        return await client.post(
            "/sandboxes",
            headers={**API, "X-Sandbox-Id": sandbox_id},
            json={"templateID": "base", "timeout": 300},
        )


async def _checkpoint(control, sandbox_id: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://c"
    ) as client:
        return await client.get(
            f"/sandboxes/{sandbox_id}/checkpoint", headers=API
        )


async def test_the_nodes_answer_is_what_the_caller_gets(make_remote_harness) -> None:
    """The worker wrote it, the worker answers it -- the proxy adds nothing."""
    harness = make_remote_harness()
    control = harness["control"]
    stub = harness["stub"]
    created = await _create(control, "sbx_proxy")
    assert created.status_code == 201
    before = control.state.registry.get("sbx_proxy").state

    resp = await _checkpoint(control, "sbx_proxy")

    assert resp.status_code == 200
    # 整份相等（不做子串判据）：控制面不改写这张表。
    assert resp.json() == {**_WORKER_ANSWER, "sandboxID": "sbx_proxy"}
    assert stub.checkpoint_calls == ["sbx_proxy"]
    assert control.state.registry.get("sbx_proxy").state == before, (
        "这条端点是只读的：问一次不能动记录"
    )


async def test_an_unreachable_node_says_so_instead_of_failing(
    make_remote_harness,
) -> None:
    """通道不通时给"不知道"（带 unreachable），而不是 5xx。

    ``pause``/``resume`` 的投递契约是控制面把 worker 的错误当回滚依据，这条只读查询
    一旦报错就把"看一眼"变成了新的失败点。
    """
    harness = make_remote_harness()
    control = harness["control"]
    created = await _create(control, "sbx_proxy_dead")
    assert created.status_code == 201
    # The node is still registered, but nothing is listening on its address.
    harness["server"].stop()

    resp = await _checkpoint(control, "sbx_proxy_dead")

    assert resp.status_code == 200
    assert resp.json() == {
        "sandboxID": "sbx_proxy_dead",
        "hasImage": False,
        "imageMB": 0,
        "capturedAt": None,
        "lastRestore": None,
        "unreachable": True,
    }
