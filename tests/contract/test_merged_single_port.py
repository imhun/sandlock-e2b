"""Merged control plane + gateway single-port contract (SLB regression).

The merged image serves the control plane API and the envd gateway on ONE
port: API paths must win over the gateway catch-all, health endpoints must
answer GET+HEAD (an SLB health probe), and sandbox-scoped requests must fall
through to the gateway proxy.
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings
from envd_service.gateway import create_gateway


@pytest.fixture()
def merged_app():
    app = create_control_app(
        settings=Settings(api_keys=("local-key",), enable_local_node=True)
    )
    gateway = create_gateway(
        control_plane_url="http://control-plane:3000",
        internal_api_key="internal-key",
    )
    app.mount("/", gateway)
    return app


@pytest.fixture()
async def client(merged_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=merged_app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_health_endpoints_answer_get_and_head(client):
    """SLB regression: the default health probe is HEAD /, which previously
    returned 405 and took the backend out of the load balancer."""
    for path in ("/", "/healthz"):
        assert (await client.get(path)).status_code == 200
        assert (await client.request("HEAD", path)).status_code == 200


@pytest.mark.asyncio
async def test_api_routes_win_over_gateway_catch_all(client):
    """Control plane routes (registered first) must not be swallowed by the
    gateway's /{path:path} proxy."""
    # No API key -> the control plane's official 401 JSON, not the gateway's
    # "Missing E2b-Sandbox-Id header" text.
    resp = await client.post("/sandboxes", json={})
    assert resp.status_code == 401
    assert resp.json() == {"code": 401, "message": "Unauthorized"}

    listed = await client.get("/sandboxes", headers={"X-API-Key": "local-key"})
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_gateway_catch_all_handles_sandbox_traffic(client):
    """Requests without a control-plane route fall through to the gateway:
    missing sandbox id -> gateway 401 text; invalidate endpoint works."""
    resp = await client.get("/health")
    assert resp.status_code == 401
    assert "E2b-Sandbox-Id" in resp.text

    invalidate = await client.post(
        "/internal/routes/sbx_x/invalidate",
        headers={"X-Internal-Key": "internal-key"},
    )
    assert invalidate.status_code == 204
