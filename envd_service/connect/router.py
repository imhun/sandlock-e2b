"""Connect-RPC dispatch for the process and filesystem services."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from envd_service.connect.codec import (
    CONTENT_TYPE_STREAM,
    CONTENT_TYPE_UNARY,
    decode_stream_request,
    encode_end_stream,
    encode_message,
)
from gateway_common.errors import (
    CONNECT_HTTP_STATUS,
    ConnectError,
    internal,
    unauthenticated,
)

logger = logging.getLogger(__name__)

UnaryHandler = Callable[
    [Request, dict[str, Any], Any], Awaitable[dict[str, Any] | None]
]
StreamHandler = Callable[
    [Request, dict[str, Any], Any],
    Awaitable[AsyncIterator[dict[str, Any]]],
]


def _find_sandbox(request: Request) -> Any:
    """Look up the runtime record for ``E2b-Sandbox-Id`` + ``X-Access-Token``."""
    sandbox_id = request.headers.get("E2b-Sandbox-Id")
    if not sandbox_id:
        raise unauthenticated("Missing E2b-Sandbox-Id header")
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        raise unauthenticated(f"Sandbox {sandbox_id} not found")
    token = request.headers.get("X-Access-Token")
    if (
        not runtime.allow_public_traffic
        and runtime.access_token
        and token != runtime.access_token
    ):
        raise unauthenticated("Invalid access token")
    return runtime


def connect_error_response(error: ConnectError) -> JSONResponse:
    return JSONResponse(
        status_code=error.http_status or CONNECT_HTTP_STATUS.get(error.code, 500),
        content=error.to_dict(),
        media_type=CONTENT_TYPE_UNARY,
    )


async def _read_json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.body()
    except Exception as e:  # pragma: no cover - defensive
        raise internal(f"failed to read request body: {e}") from e
    if not data:
        return {}
    try:
        payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise internal(f"invalid JSON body: {e}") from e
    if not isinstance(payload, dict):
        raise internal("request body must be a JSON object")
    return payload


async def handle_unary(
    request: Request,
    path: str,
    handler: UnaryHandler,
    *,
    require_sandbox: bool,
) -> Response:
    try:
        sandbox = _find_sandbox(request) if require_sandbox else None
        payload = await _read_json_body(request)
        result = await handler(request, payload, sandbox)
    except ConnectError as e:
        return connect_error_response(e)
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("unary RPC %s failed", path)
        return connect_error_response(internal(str(e)))
    return JSONResponse(
        content=result if result is not None else {},
        media_type=CONTENT_TYPE_UNARY,
    )


async def handle_stream(
    request: Request,
    path: str,
    handler: StreamHandler,
    *,
    require_sandbox: bool,
) -> Response:
    try:
        sandbox = _find_sandbox(request) if require_sandbox else None
        raw = await request.body()
        payload = decode_stream_request(raw)
        events = await handler(request, payload, sandbox)
    except ConnectError as e:
        logger.info("stream RPC %s rejected: %s", path, e.code)
        return StreamingResponse(
            iter([encode_end_stream(e)]),
            media_type=CONTENT_TYPE_STREAM,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("stream RPC %s failed before start", path)
        return StreamingResponse(
            iter([encode_end_stream(internal(str(e)))]),
            media_type=CONTENT_TYPE_STREAM,
        )

    async def body() -> AsyncIterator[bytes]:
        try:
            async for event in events:
                yield encode_message(event)
            yield encode_end_stream(None)
        except ConnectError as e:
            yield encode_end_stream(e)
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("stream RPC %s failed mid-stream", path)
            yield encode_end_stream(internal(str(e)))

    return StreamingResponse(body(), media_type=CONTENT_TYPE_STREAM)


def register_routes(
    app: Any,
    *,
    unary: dict[str, UnaryHandler],
    stream: dict[str, StreamHandler],
    require_sandbox: bool = True,
) -> None:
    """Register explicit ``/service/Method`` endpoints.

    Unary methods accept plain JSON bodies; streaming methods use the framed
    Connect request envelope. Explicit registration (rather than a catch-all)
    keeps the HTTP routes like ``/files`` and ``/health`` unambiguous.
    """
    router = APIRouter()

    for path, handler in unary.items():

        @router.post("/" + path, include_in_schema=False)
        async def _unary_route(
            request: Request, _path: str = path, _handler: UnaryHandler = handler
        ):
            return await handle_unary(
                request, _path, _handler, require_sandbox=require_sandbox
            )

    for path, handler in stream.items():

        @router.post("/" + path, include_in_schema=False)
        async def _stream_route(
            request: Request, _path: str = path, _handler: StreamHandler = handler
        ):
            return await handle_stream(
                request, _path, _handler, require_sandbox=require_sandbox
            )

    app.include_router(router)
