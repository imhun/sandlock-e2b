#!/usr/local/bin/python3
"""E2B-compatible ``mcp-gateway`` executable.

The official SDK starts ``mcp-gateway --config <json>`` inside the sandbox
after ``Sandbox.create(mcp=...)`` and exposes ``http://<sandbox>:50005/mcp``
to MCP clients. This implementation spawns the configured stdio MCP server
as a sandbox child process and proxies the MCP protocol over streamable HTTP.

Auth: requests must carry ``Authorization: Bearer <GATEWAY_ACCESS_TOKEN>``
(or ``x-mcp-access-token``); the token comes from the SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import Server

PORT = 50005


def _authorized(scope: dict, token: str) -> bool:
    if not token:
        return True
    headers = {k.lower(): v for k, v in scope.get("headers", [])}
    bearer = headers.get(b"authorization", b"")
    mcp_token = headers.get(b"x-mcp-access-token", b"")
    expected = f"Bearer {token}".encode("utf-8")
    return bearer == expected or mcp_token == token.encode("utf-8")


class AuthMiddleware:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not _authorized(scope, self.token):
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {"jsonrpc": "2.0", "error": {"code": -32001, "message": "unauthorized"}},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _serve(config: dict[str, Any], token: str) -> None:
    params = StdioServerParameters(
        command=config["command"],
        args=list(config.get("args") or []),
        env=dict(config.get("envs") or {}) or None,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            async def _list_tools(ctx, request_params):
                return await session.list_tools()

            async def _call_tool(ctx, request_params):
                return await session.call_tool(
                    request_params.name, request_params.arguments
                )

            server = Server(
                "e2b-mcp-gateway",
                on_list_tools=_list_tools,
                on_call_tool=_call_tool,
            )

            # Mount the Starlette streamable-HTTP app under /mcp and guard
            # every request with the gateway access token. The app carries
            # its own lifespan (session manager), so it must stay the root app.
            app = AuthMiddleware(server.streamable_http_app(), token)
            config_uv = uvicorn.Config(
                app, host="0.0.0.0", port=PORT, log_level="warning"
            )
            await uvicorn.Server(config_uv).serve()


def _daemonize() -> None:
    """Fork into the background so the SDK's foreground ``run`` returns.

    The child stays in the parent's process group: sandlock kills the whole
    group when the sandbox is killed, which tears the gateway down too.
    """
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass
    if devnull > 2:
        os.close(devnull)


def main() -> None:
    parser = argparse.ArgumentParser(description="E2B MCP gateway")
    parser.add_argument("--config", required=True, help="JSON MCP server config")
    parser.add_argument(
        "--foreground", action="store_true", help="stay in the foreground (debug)"
    )
    args = parser.parse_args()
    config = json.loads(args.config)
    token = os.environ.get("GATEWAY_ACCESS_TOKEN", "")
    if not args.foreground:
        _daemonize()
    asyncio.run(_serve(config, token))


if __name__ == "__main__":
    main()
