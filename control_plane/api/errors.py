"""Official error type for the control plane."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class OfficialError(Exception):
    """An error carrying the official ``{"code": int, "message": str}`` body."""

    def __init__(
        self,
        code: int,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # E9.3: optional response headers (e.g. x-e2b-eviction-reason on the
        # evicted-sandbox 404). ``None`` keeps every existing call site byte
        # for byte identical to before.
        self.headers = dict(headers) if headers else None


def official_error_handler(_: Request, exc: OfficialError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.code,
        content={"code": exc.code, "message": exc.message},
        headers=exc.headers,
    )
