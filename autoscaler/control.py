"""Thin control-plane client: fleet metrics and node drain lifecycle."""

from __future__ import annotations

from typing import Any

import httpx


class ControlPlaneClient:
    def __init__(
        self,
        *,
        base_url: str,
        internal_api_key: str,
        timeout: float = 15.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Internal-Key": internal_api_key}
        self._timeout = timeout

    def metrics(self) -> dict[str, Any]:
        resp = httpx.get(
            f"{self._base}/internal/fleet/metrics",
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def drain(self, node_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self._base}/internal/nodes/{node_id}/drain",
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()
