"""C3 Task 2 (D5 follow-up): a worker says *locally* when it never joined.

The control plane now verifies a node-scoped request against the node's
resolved address, so a *separated* worker must declare ``E2B_NODE_ID`` (and a
registered-but-unresolvable node is refused). Before this, a refused register
produced no line on the worker at all -- the round just retried forever, and
the only trace was on the control plane. The refusal stays a retry (no hard
startup failure); this lane pins the named line that makes it diagnosable from
the node.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

import envd_service.agent as agent_mod
from envd_service.agent import NodeAgent
from envd_service.config import Settings
from envd_service.runtime.registry import RuntimeRegistry


def _stub_control_plane(status: int) -> FastAPI:
    """A control plane that refuses every node-scoped exchange with ``status``."""
    app = FastAPI()

    @app.post("/internal/nodes/register")
    async def register(request: Request) -> Response:
        return Response(status_code=status)

    @app.post("/internal/nodes/{node_id}/heartbeat")
    async def heartbeat(node_id: str, request: Request) -> Response:
        return Response(status_code=status)

    return app


def _point_the_agent_at(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(
            transport=httpx.ASGITransport(app=app),
            base_url="http://control",
            **kwargs,
        )

    monkeypatch.setattr(agent_mod.httpx, "AsyncClient", factory)


def _agent(tmp_path) -> NodeAgent:
    settings = Settings(executor="local", workspace_base=str(tmp_path))
    return NodeAgent(
        settings=settings,
        runtime_registry=RuntimeRegistry(tmp_path),
        control_plane_url="http://control",
        node_address="http://127.0.0.1:49983",
        node_id="worker-1",
    )


@pytest.mark.asyncio
async def test_a_refused_register_is_named_on_the_worker(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _point_the_agent_at(monkeypatch, _stub_control_plane(503))
    agent = _agent(tmp_path)
    with caplog.at_level(logging.WARNING, logger="envd_service.agent"):
        await agent._pulse()

    assert agent._node_id is None  # still retrying; no hard failure
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "envd_service.agent"
    ] == [
        "node agent: registration rejected by the control plane (HTTP 503): "
        "this worker will not join; a separated worker must declare E2B_NODE_ID"
    ]


@pytest.mark.asyncio
async def test_a_refused_heartbeat_is_named_on_the_worker(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _point_the_agent_at(monkeypatch, _stub_control_plane(403))
    agent = _agent(tmp_path)
    agent._node_id = "worker-1"  # already joined once; the CP now refuses
    with caplog.at_level(logging.WARNING, logger="envd_service.agent"):
        await agent._pulse()

    assert agent._node_id == "worker-1"
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "envd_service.agent"
    ] == [
        "node agent: heartbeat for node worker-1 rejected by the control plane "
        "(HTTP 403)"
    ]
