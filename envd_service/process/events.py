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


def keepalive_event() -> dict[str, Any]:
    """The protocol's empty ``ProcessEvent.KeepAlive`` (N37).

    It carries no payload and the SDK ignores it; a streaming command whose
    output has gone quiet is kept alive by it, because the reverse proxy in
    front of the API cuts a response body that has been silent for 60 s
    (measured -- see ``gateway_common/keepalive.py``). Without it a long
    silent command dies mid-flight with the SDK's ``unexpected EOF during
    chunk size line``.
    """
    return {"event": {"keepalive": {}}}
