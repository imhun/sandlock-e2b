"""Official error JSON and Connect error mapping.

Control-plane errors use the official shape::

    {"code": 404, "message": "Sandbox sbx_xxx not found"}

Envd Connect errors use Connect-RPC error JSON::

    {"code": "not_found", "message": "..."}

"""

from __future__ import annotations

import json
from typing import Any

# Connect-RPC error codes (connectrpc.code.Code values).
CONNECT_CODE_CANCELED = "canceled"
CONNECT_CODE_UNKNOWN = "unknown"
CONNECT_CODE_INVALID_ARGUMENT = "invalid_argument"
CONNECT_CODE_DEADLINE_EXCEEDED = "deadline_exceeded"
CONNECT_CODE_NOT_FOUND = "not_found"
CONNECT_CODE_ALREADY_EXISTS = "already_exists"
CONNECT_CODE_PERMISSION_DENIED = "permission_denied"
CONNECT_CODE_RESOURCE_EXHAUSTED = "resource_exhausted"
CONNECT_CODE_FAILED_PRECONDITION = "failed_precondition"
CONNECT_CODE_ABORTED = "aborted"
CONNECT_CODE_OUT_OF_RANGE = "out_of_range"
CONNECT_CODE_UNIMPLEMENTED = "unimplemented"
CONNECT_CODE_INTERNAL = "internal"
CONNECT_CODE_UNAVAILABLE = "unavailable"
CONNECT_CODE_DATA_LOSS = "data_loss"
CONNECT_CODE_UNAUTHENTICATED = "unauthenticated"

# HTTP status for each Connect error code (Connect protocol mapping).
CONNECT_HTTP_STATUS: dict[str, int] = {
    CONNECT_CODE_CANCELED: 499,
    CONNECT_CODE_UNKNOWN: 500,
    CONNECT_CODE_INVALID_ARGUMENT: 400,
    CONNECT_CODE_DEADLINE_EXCEEDED: 504,
    CONNECT_CODE_NOT_FOUND: 404,
    CONNECT_CODE_ALREADY_EXISTS: 409,
    CONNECT_CODE_PERMISSION_DENIED: 403,
    CONNECT_CODE_RESOURCE_EXHAUSTED: 429,
    CONNECT_CODE_FAILED_PRECONDITION: 400,
    CONNECT_CODE_ABORTED: 409,
    CONNECT_CODE_OUT_OF_RANGE: 400,
    CONNECT_CODE_UNIMPLEMENTED: 501,
    CONNECT_CODE_INTERNAL: 500,
    CONNECT_CODE_UNAVAILABLE: 503,
    CONNECT_CODE_DATA_LOSS: 500,
    CONNECT_CODE_UNAUTHENTICATED: 401,
}


class ConnectError(Exception):
    """A Connect-RPC error carried through the wire layer."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        details: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status or CONNECT_HTTP_STATUS.get(code, 500)
        self.details = tuple(details)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = list(self.details)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def connect_error(
    code: str,
    message: str,
    *,
    http_status: int | None = None,
    details: tuple[dict[str, Any], ...] = (),
) -> ConnectError:
    return ConnectError(
        code=code,
        message=message,
        http_status=http_status or CONNECT_HTTP_STATUS[code],
        details=details,
    )


def invalid_argument(message: str) -> ConnectError:
    return connect_error(CONNECT_CODE_INVALID_ARGUMENT, message)


def unauthenticated(message: str = "Invalid access token") -> ConnectError:
    return connect_error(CONNECT_CODE_UNAUTHENTICATED, message)


def not_found(message: str) -> ConnectError:
    return connect_error(CONNECT_CODE_NOT_FOUND, message)


def already_exists(message: str) -> ConnectError:
    return connect_error(CONNECT_CODE_ALREADY_EXISTS, message)


def resource_exhausted(message: str) -> ConnectError:
    return connect_error(CONNECT_CODE_RESOURCE_EXHAUSTED, message)


def failed_precondition(message: str) -> ConnectError:
    return connect_error(CONNECT_CODE_FAILED_PRECONDITION, message)


def unimplemented(message: str) -> ConnectError:
    return connect_error(CONNECT_CODE_UNIMPLEMENTED, message)


def internal(message: str) -> ConnectError:
    return connect_error(CONNECT_CODE_INTERNAL, message)


def official_error(code: int, message: str) -> str:
    """Official control-plane error body (``{"code": <int>, "message": ...}``)."""
    return json.dumps({"code": code, "message": message}, separators=(",", ":"))
