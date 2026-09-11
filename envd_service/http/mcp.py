"""``/mcp`` proxy: forward MCP streamable-HTTP traffic to the sandbox's
mcp-gateway (per-sandbox port inside the worker container)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from envd_service.http.auth import HttpAuthError

router = APIRouter()


@router.api_route(
    "/mcp",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
@router.api_route(
    "/mcp/{path:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def mcp_proxy(request: Request, path: str = "") -> Response:
    sandbox_id = request.headers.get("E2b-Sandbox-Id")
    if not sandbox_id:
        raise HttpAuthError(401, "Missing E2b-Sandbox-Id header")
    context = request.app.state.runtimes.get(sandbox_id)
    if context is None:
        raise HttpAuthError(404, f"Sandbox {sandbox_id} not found")
    port = context.mcp_port
    token = context.mcp_token
    if port is None or token is None:
        raise HttpAuthError(404, "MCP is not enabled for this sandbox")

    auth = request.headers.get("Authorization", "")
    x_token = request.headers.get("x-mcp-access-token")
    if not (auth == f"Bearer {token}" or x_token == token):
        raise HttpAuthError(401, "Invalid MCP access token")
    # FUP #4 (Task D1): a gateway the watcher already saw die is not reachable,
    # so an MCP client gets the recorded reason (exit code + the gateway's own
    # stderr tail) instead of a bare connection error through the proxy. The
    # text is the same string the command path returns verbatim.
    failure = getattr(context, "mcp_gateway_failure", None)
    if failure is not None:
        raise HttpAuthError(503, failure.text)
    # E9.1: this route authenticates inline (it targets the per-sandbox
    # mcp-gateway port, not a runtime), so it must mark activity itself --
    # otherwise an MCP-only sandbox would keep looking idle and could be
    # evicted while it is serving requests.
    request.app.state.runtime_registry.mark_active(sandbox_id)

    url = f"http://127.0.0.1:{port}/mcp"
    if path:
        url += "/" + path
    if request.url.query:
        url += f"?{request.url.query}"
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in ("host", "content-length", "connection", "e2b-sandbox-id")
    }
    client = httpx.AsyncClient(timeout=None)
    upstream = await client.request(
        request.method,
        url,
        headers=headers,
        content=await request.body(),
    )

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
        if k.lower() not in ("content-length", "transfer-encoding", "connection")
    }
    return StreamingResponse(
        _body(),
        status_code=upstream.status_code,
        headers=response_headers,
    )
