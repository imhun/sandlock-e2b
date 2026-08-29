"""Official error type for the control plane."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class OfficialError(Exception):
    """An error carrying the official ``{"code": int, "message": str}`` body."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def official_error_handler(_: Request, exc: OfficialError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.code,
        content={"code": exc.code, "message": exc.message},
    )

