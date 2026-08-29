"""Connect envelope encoding/decoding."""

from __future__ import annotations

import struct

from envd_service.connect.codec import (
    decode_envelopes,
    decode_stream_request,
    encode_end_stream,
    encode_message,
)
from gateway_common.errors import invalid_argument


def test_encode_message_format():
    payload = {"event": {"start": {"pid": 42}}}
    data = encode_message(payload)
    assert data[0] == 0  # flags
    (length,) = struct.unpack(">I", data[1:5])
    assert length == len(data) - 5
    assert data[5:] == b'{"event":{"start":{"pid":42}}}'


def test_decode_roundtrip():
    payload = {"event": {"start": {"pid": 42}}}
    assert decode_envelopes(encode_message(payload)) == [payload]


def test_decode_stream_request_takes_first_message():
    messages = [{"process": {"cmd": "x"}}, {"extra": 1}]
    data = b"".join(encode_message(m) for m in messages)
    assert decode_stream_request(data) == messages[0]


def test_end_stream_success_is_empty_object():
    data = encode_end_stream(None)
    assert data[0] == 2
    assert decode_envelopes(data) == [{}]


def test_end_stream_error_has_connect_error_shape():
    data = encode_end_stream(invalid_argument("bad"))
    messages = decode_envelopes(data)
    assert messages == [{"error": {"code": "invalid_argument", "message": "bad"}}]


def test_truncated_envelope_raises():
    import pytest

    data = encode_message({"a": 1})[:-3]
    with pytest.raises(ValueError):
        decode_envelopes(data)

