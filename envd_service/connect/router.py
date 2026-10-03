"""Connect-RPC dispatch for the process and filesystem services."""

from __future__ import annotations

import asyncio
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
from envd_service.runtime.registry import RuntimeRegistry, state_clause
from gateway_common.errors import (
    CONNECT_HTTP_STATUS,
    ConnectError,
    failed_precondition,
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

#: E9.1: while a stream RPC is open, re-stamp its sandbox's activity on this
#: cadence. It matches ``RuntimeRegistry.ACTIVITY_COALESCE_S`` on purpose --
#: the registry coalesces marks inside that window, so one open stream costs at
#: most one activity write per window regardless of how long the command runs.
#:
#: Why it has to exist at all: ``_find_sandbox`` marks the sandbox active once,
#: when the request arrives. A stream that then stays silent -- ``sleep 300``,
#: a slow ``make``, an exec waiting on a remote API -- produces no further
#: chunks and (for ``sleep``) no measurable CPU, so ``last_active_at`` would
#: age out while the client is still holding the stream, and the idle->pause
#: sweep would freeze the command underneath it.
ACTIVITY_KEEPALIVE_S: float = RuntimeRegistry.ACTIVITY_COALESCE_S


def _find_sandbox(request: Request) -> Any:
    """Look up the runtime record for ``E2b-Sandbox-Id`` + ``X-Access-Token``."""
    sandbox_id = request.headers.get("E2b-Sandbox-Id")
    if not sandbox_id:
        raise unauthenticated("Missing E2b-Sandbox-Id header")
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        raise unauthenticated(f"Sandbox {sandbox_id} not found")
    token = request.headers.get("X-Access-Token")
    # SEC-K0S-005 (2026-10-01): same unconditional token requirement as
    # ``envd_service/http/auth.py`` -- this is the RPC half of the control
    # surface (``process.Process/Start``, filesystem ops), and waiving it on
    # ``allowPublicTraffic`` was an unauthenticated command-execution hole.
    if runtime.access_token and token != runtime.access_token:
        raise unauthenticated("Invalid access token")
    # E9.1: an authenticated call is activity; the worker reports it to the
    # control plane on its next heartbeat (idle detection / eviction).
    request.app.state.runtime_registry.mark_active(sandbox_id)
    return runtime


async def _keep_active_while_streaming(registry, sandbox_id: str) -> None:
    """Keep ``sandbox_id`` active for as long as a stream RPC is open (E9.1).

    Started by :func:`handle_stream` and cancelled when the stream closes
    (including a client disconnect, which closes the response generator).
    Cancellation is the normal exit. A failing activity report is the
    registry's problem, not the caller's: it is logged and the stream keeps
    running -- breaking an exec because a heartbeat could not be stamped would
    be a far worse trade.
    """
    while True:
        try:
            registry.mark_active(sandbox_id)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "stream activity keepalive failed for sandbox %s",
                sandbox_id,
                exc_info=True,
            )
        await asyncio.sleep(ACTIVITY_KEEPALIVE_S)


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


# -- pause gating ----------------------------------------------------------
#
#: RPCs that make the sandbox do work or change its workspace.
#:
#: A paused sandbox has given its admission reservation back (E9.2), so letting
#: it keep executing lets it consume capacity nobody is counting -- and, for the
#: file APIs, write into a workspace whose size is no longer being gated. Reads
#: stay allowed on purpose: they consume nothing and an operator needs them to
#: look at a paused sandbox before resuming it.
MUTATING_RPCS = frozenset(
    {
        "process.Process/Start",
        "filesystem.Filesystem/MakeDir",
        "filesystem.Filesystem/Move",
        "filesystem.Filesystem/Remove",
    }
)


def _require_running(sandbox: Any, path: str) -> None:
    """Refuse a mutating RPC while the sandbox is not running."""
    if sandbox is None or path not in MUTATING_RPCS:
        return
    state = getattr(sandbox, "state", "running")
    if state == "running":
        return
    raise failed_precondition(
        (
            f"{state_clause(sandbox, state)}; "
            f"{'run a command' if path.startswith('process.') else 'modify files'} "
            "only while it is running (resume it first)"
        )
    )


async def handle_unary(
    request: Request,
    path: str,
    handler: UnaryHandler,
    *,
    require_sandbox: bool,
) -> Response:
    try:
        sandbox = _find_sandbox(request) if require_sandbox else None
        _require_running(sandbox, path)
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
        _require_running(sandbox, path)
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
        # E9.1: an open stream is activity for as long as it is open -- see
        # ``ACTIVITY_KEEPALIVE_S``. The task is cancelled in ``finally``, so
        # both a clean end and a client disconnect stop the marks.
        sandbox_id = getattr(sandbox, "sandbox_id", None)
        keepalive = None
        if sandbox_id:
            registry = request.app.state.runtime_registry
            keepalive = asyncio.create_task(
                _keep_active_while_streaming(registry, sandbox_id)
            )
        try:
            async for event in events:
                yield encode_message(event)
            yield encode_end_stream(None)
        except ConnectError as e:
            yield encode_end_stream(e)
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("stream RPC %s failed mid-stream", path)
            yield encode_end_stream(internal(str(e)))
        finally:
            if keepalive is not None:
                keepalive.cancel()

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
