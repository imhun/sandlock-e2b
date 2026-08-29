"""Envd Gateway: single entry point routing SDK traffic to the node holding
each sandbox (``E2b-Sandbox-Id`` -> node), preserving Connect streaming and
HTTP semantics."""

from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)


class RouteCache:
    def __init__(self, ttl: float = 30.0) -> None:
        self._routes: dict[str, tuple[float, str]] = {}
        self._ttl = ttl

    def get(self, sandbox_id: str) -> str | None:
        entry = self._routes.get(sandbox_id)
        if entry is None:
            return None
        fetched_at, address = entry
        if time.time() - fetched_at > self._ttl:
            self._routes.pop(sandbox_id, None)
            return None
        return address

    def put(self, sandbox_id: str, address: str) -> None:
        self._routes[sandbox_id] = (time.time(), address)


def create_gateway(
    *,
    control_plane_url: str | None = None,
    internal_api_key: str | None = None,
) -> FastAPI:
    control_url = (
        control_plane_url or os.getenv("E2B_CONTROL_PLANE_URL", "")
    ).rstrip("/")
    internal_key = internal_api_key or os.getenv(
        "E2B_INTERNAL_API_KEY", "internal-key"
    )
    routes = RouteCache()

    app = FastAPI(title="E2B Sandlock Gateway - Envd Router")

    @app.post("/internal/routes/{sandbox_id}/invalidate")
    async def invalidate_route(sandbox_id: str, request: Request) -> Response:
        if request.headers.get("X-Internal-Key") != internal_key:
            return Response(status_code=401)
        routes._routes.pop(sandbox_id, None)
        return Response(status_code=204)

    async def _resolve_address(sandbox_id: str) -> str:
        cached = routes.get(sandbox_id)
        if cached:
            return cached
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{control_url}/internal/routes/{sandbox_id}",
                headers={"X-Internal-Key": internal_key},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"route lookup failed: {resp.status_code}")
            address = resp.json().get("address")
        if not address or address == "local://":
            raise RuntimeError("sandbox has no remote node address")
        routes.put(sandbox_id, address)
        return address

    _FORWARD_HEADERS = {
        "content-type",
        "authorization",
        "x-access-token",
        "e2b-sandbox-id",
        "e2b-sandbox-port",
        "accept",
        "connect-protocol-version",
        "x-mcp-access-token",
    }

    def _forward_headers(request: Request) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            lower = key.lower()
            if lower == "host" or lower == "content-length":
                continue
            if lower.startswith("x-metadata-") or lower in _FORWARD_HEADERS:
                headers[key] = value
        return headers

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def proxy(path: str, request: Request) -> Response:
        sandbox_id = request.headers.get("E2b-Sandbox-Id")
        if not sandbox_id:
            return Response(
                status_code=401, content="Missing E2b-Sandbox-Id header"
            )
        try:
            address = await _resolve_address(sandbox_id)
        except Exception as e:
            logger.info("route lookup failed for %s: %s", sandbox_id, e)
            # 502 matches the envd /health contract so the SDK's
            # ``is_running()`` reports False when the node is gone.
            return Response(status_code=502, content="Sandbox route not found")

        url = f"{address}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = _forward_headers(request)
        try:
            client = httpx.AsyncClient(timeout=None)
            upstream = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=headers,
                    content=request.stream(),
                ),
                stream=True,
            )
        except httpx.HTTPError as e:
            logger.info("proxy to %s failed: %s", address, e)
            return Response(status_code=502, content=f"Node unavailable: {e}")

        async def _body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        response_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower()
            not in ("content-length", "transfer-encoding", "connection")
        }
        return StreamingResponse(
            _body(),
            status_code=upstream.status_code,
            headers=response_headers,
        )

    return app
