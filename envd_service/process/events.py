"""Builders for ProcessEvent JSON payloads (protobuf JSON mapping)."""

from __future__ import annotations

import base64
from typing import Any


def start_event(pid: int) -> dict[str, Any]:
    return {"event": {"start": {"pid": pid}}}


def data_event(kind: str, chunk: bytes) -> dict[str, Any]:
    # protobuf JSON maps ``bytes`` fields to base64 strings.
    return {"event": {"data": {kind: base64.b64encode(chunk).decode("ascii")}}}


def end_event(exit_code: int, status: str = "exited") -> dict[str, Any]:
    return {
        "event": {
            "end": {
                "exitCode": int(exit_code),
                "exited": True,
                "status": status,
                "error": None,
            }
        }
    }

