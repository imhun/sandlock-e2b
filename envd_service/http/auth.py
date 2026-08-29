"""Authentication dependency for envd HTTP endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class HttpAuthError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def http_error_response(request: Request, exc: HttpAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.message},
    )


def require_http_sandbox(request: Request, health: bool = False) -> Any:
    """Look up the sandbox runtime for an HTTP request.

    ``/health`` reports a missing sandbox as 502 so the SDK's
    ``is_running()`` returns ``False`` after kill; other endpoints use 404.
    """
    sandbox_id = request.headers.get("E2b-Sandbox-Id")
    if not sandbox_id:
        raise HttpAuthError(401, "Missing E2b-Sandbox-Id header")
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        if health:
            raise HttpAuthError(502, f"Sandbox {sandbox_id} not found")
        raise HttpAuthError(404, f"Sandbox {sandbox_id} not found")
    token = request.headers.get("X-Access-Token")
    if (
        not runtime.allow_public_traffic
        and runtime.access_token
        and token != runtime.access_token
    ):
        raise HttpAuthError(401, "Invalid access token")
    return runtime
