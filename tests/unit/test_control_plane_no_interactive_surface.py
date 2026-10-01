"""SEC-K0S-002: the control plane ships no unauthenticated API inventory.

The tenant entrance -- and, per SEC-K0S-004, a sandbox itself -- can reach the
control plane, so ``/openapi.json`` used to hand an unauthenticated caller the
full internal path list (including the C3 ``file-op`` relay) plus the prose
that spells out the trust model. The privileged C3 agent already closed these
routes for the same reason; this pins the control plane to the same shape and
proves the real routes still answer, so the fix cannot be "closed by hiding
everything".
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.app import create_app
from control_plane.config import Settings


def _app(tmp_path):
    return create_app(
        settings=Settings(
            api_keys=("local-key",),
            create_queue_timeout_s=0,
            workspace_base=tmp_path / "trees",
        ),
        workspace_base=tmp_path / "trees",
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    )


@pytest.mark.asyncio
async def test_the_docs_routes_are_all_closed(tmp_path) -> None:
    app = _app(tmp_path)
    async with _client(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            resp = await client.get(path)
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_the_real_routes_are_unaffected(tmp_path) -> None:
    app = _app(tmp_path)
    async with _client(app) as client:
        root = await client.get("/")
        assert root.status_code == 200
        assert root.json() == {"status": "ok", "service": "e2b-sandlock"}

        healthz = await client.get("/healthz")
        assert healthz.status_code == 200
        assert healthz.json() == {"status": "ok"}

        # The inventory endpoints themselves still exist and still refuse an
        # unauthenticated caller -- closing the docs did not move the auth line.
        unauthenticated = await client.get("/sandboxes")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {"code": 401, "message": "Unauthorized"}
