"""API key authentication for the control plane."""

from __future__ import annotations

from fastapi import Header, Request

from control_plane.api.errors import OfficialError


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    settings = request.app.state.settings
    key = x_api_key
    if key is None:
        # Some SDK versions emit X-API-KEY; headers are case-insensitive but
        # FastAPI dependency resolution already normalized this one.
        key = request.headers.get("X-API-KEY")
    if key is None or key not in settings.all_api_keys:
        raise OfficialError(401, "Unauthorized")
    return key
