"""Authentication dependency for envd HTTP endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from envd_service.runtime.registry import state_clause


class HttpAuthError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        #: Extra response headers, e.g. ``Retry-After`` on a *transient* 503
        #: (the caller can succeed by retrying) as opposed to a permanent one
        #: (retrying would be a lie).
        self.headers = dict(headers or {})


def http_error_response(request: Request, exc: HttpAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.message},
        headers=exc.headers or None,
    )


def require_http_sandbox(
    request: Request, health: bool = False, mutating: bool = False
) -> Any:
    """Look up the sandbox runtime for an HTTP request.

    ``/health`` reports a missing sandbox as 502 so the SDK's
    ``is_running()`` returns ``False`` after kill; other endpoints use 404.

    ``mutating`` marks an endpoint that changes the workspace. A sandbox that
    is not running refuses those: a paused sandbox has given its admission
    reservation back (E9.2), so a write into it would land in a workspace
    nothing is accounting for -- and the sandbox's own processes could not have
    made it. Reads stay allowed, so an operator can look before resuming.
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
    state = getattr(runtime, "state", "running")
    if mutating and state != "running":
        raise HttpAuthError(
            409,
            f"{state_clause(runtime, state)}; its files can only be modified "
            "while it is running (resume it first)",
        )
    # E9.1: an authenticated call is activity; the worker reports it to the
    # control plane on its next heartbeat (idle detection / eviction).
    request.app.state.runtime_registry.mark_active(sandbox_id)
    return runtime
