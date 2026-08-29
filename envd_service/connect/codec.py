"""Connect protocol envelope codec (JSON flavor)."""

from __future__ import annotations

import json
import struct
from typing import Any

from gateway_common.errors import ConnectError

FLAG_MESSAGE = 0x00
FLAG_END_STREAM = 0x02

CONTENT_TYPE_UNARY = "application/json"
CONTENT_TYPE_STREAM = "application/connect+json"


def encode_message(payload: dict[str, Any] | list[Any]) -> bytes:
    """Encode one streaming message: ``1 byte flags + 4 byte length + JSON``."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">BI", FLAG_MESSAGE, len(body)) + body


def encode_end_stream(error: ConnectError | None = None) -> bytes:
    """Encode the final EndStreamResponse envelope (flags=2)."""
    if error is None:
        payload: dict[str, Any] = {}
    else:
        payload = {"error": error.to_dict()}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">BI", FLAG_END_STREAM, len(body)) + body


def decode_envelopes(data: bytes) -> list[dict[str, Any]]:
    """Decode a sequence of ``flags + length + JSON`` envelopes."""
    messages: list[dict[str, Any]] = []
    for _flags, payload in decode_envelopes_with_flags(data):
        messages.append(payload)
    return messages


def decode_envelopes_with_flags(data: bytes) -> list[tuple[int, dict[str, Any]]]:
    """Decode envelopes returning ``(flags, payload)`` pairs."""
    messages: list[tuple[int, dict[str, Any]]] = []
    offset = 0
    while offset < len(data):
        if offset + 5 > len(data):
            raise ValueError("truncated connect envelope header")
        flags, length = struct.unpack(">BI", data[offset : offset + 5])
        offset += 5
        if offset + length > len(data):
            raise ValueError("truncated connect envelope payload")
        body = data[offset : offset + length]
        offset += length
        if not body:
            continue
        messages.append((flags, json.loads(body.decode("utf-8"))))
    return messages


def decode_stream_request(data: bytes) -> dict[str, Any]:
    """Decode the (possibly enveloped) request body of a streaming call."""
    if not data:
        raise ValueError("empty request body")
    messages = decode_envelopes(data)
    if not messages:
        raise ValueError("no request message in stream")
    payload = messages[0]
    if not isinstance(payload, dict):
        raise ValueError("request message must be a JSON object")
    return payload


def is_stream_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == CONTENT_TYPE_STREAM


def is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == CONTENT_TYPE_UNARY
