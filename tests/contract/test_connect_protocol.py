"""Connect-RPC wire contract tests (raw envelopes)."""

from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest

from envd_service.connect.codec import (
    decode_envelopes,
    decode_envelopes_with_flags,
    encode_message,
)


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


def _headers(sandbox: dict) -> dict:
    return {
        "E2b-Sandbox-Id": sandbox["sandboxID"],
        "X-Access-Token": sandbox["envdAccessToken"],
    }


async def test_unary_list(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.post(
        "/process.Process/List",
        headers={**_headers(sandbox), "Content-Type": "application/json"},
        content=b"{}",
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"processes": []}


async def test_start_stream_events_and_end_flags(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    request = {
        "process": {
            "cmd": "/bin/echo",
            "args": ["hello"],
            "envs": {},
            "cwd": "/",
        },
        "stdin": False,
    }
    response = await envd_client.post(
        "/process.Process/Start",
        headers={
            **_headers(sandbox),
            "Content-Type": "application/connect+json",
        },
        content=encode_message(request),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/connect+json")
    data = response.content
    messages = decode_envelopes(data)
    assert messages[0] == {"event": {"start": {"pid": messages[0]["event"]["start"]["pid"]}}}
    # stdout data event (base64)
    data_events = [m for m in messages if "data" in m.get("event", {})]
    assert len(data_events) == 1
    assert base64.b64decode(data_events[0]["event"]["data"]["stdout"]) == b"hello\n"
    end = messages[-2]
    assert end == {
        "event": {
            "end": {
                "exitCode": 0,
                "exited": True,
                "status": "exited",
                "error": None,
            }
        }
    }
    # Final EndStreamResponse envelope has flags=2.
    flagged = decode_envelopes_with_flags(data)
    assert flagged[-1] == (2, {})
    assert messages[-1] == {}


async def test_start_error_uses_end_envelope(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    request = {
        "process": {"cmd": "/nonexistent-binary-xyz", "args": [], "envs": {}},
        "stdin": False,
    }
    response = await envd_client.post(
        "/process.Process/Start",
        headers={
            **_headers(sandbox),
            "Content-Type": "application/connect+json",
        },
        content=encode_message(request),
    )
    assert response.status_code == 200
    data = response.content
    messages = decode_envelopes(data)
    assert "pid" in messages[0]["event"]["start"]
    stderr_events = [
        m
        for m in messages
        if "data" in m.get("event", {}) and "stderr" in m["event"]["data"]
    ]
    assert len(stderr_events) == 1
    assert b"command not found" in base64.b64decode(
        stderr_events[0]["event"]["data"]["stderr"]
    )
    assert messages[-2]["event"]["end"]["exitCode"] == 127
    assert messages[-1] == {}


async def test_unauthenticated_rpc(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.post(
        "/process.Process/List",
        headers={
            "E2b-Sandbox-Id": sandbox["sandboxID"],
            "X-Access-Token": "wrong-token",
            "Content-Type": "application/json",
        },
        content=b"{}",
    )
    assert response.status_code == 401
    assert response.json() == {"code": "unauthenticated", "message": "Invalid access token"}


async def test_send_input_uses_base64(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    req = {
        "process": {"pid": 1},
        "input": {"stdin": base64.b64encode(b"abc").decode()},
    }
    resp = await envd_client.post(
        "/process.Process/SendInput",
        headers={**_headers(sandbox), "Content-Type": "application/json"},
        content=json.dumps(req).encode(),
    )
    # The request is decoded and routed; the unknown pid yields NOT_FOUND.
    assert resp.status_code == 404
    assert resp.json() == {"code": "not_found", "message": "Process 1 not found"}


async def test_send_signal_missing_process_not_found(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    req = {"process": {"pid": 99999}, "signal": "SIGNAL_SIGKILL"}
    resp = await envd_client.post(
        "/process.Process/SendSignal",
        headers={**_headers(sandbox), "Content-Type": "application/json"},
        content=json.dumps(req).encode(),
    )
    assert resp.status_code == 404
    assert resp.json() == {"code": "not_found", "message": "Process 99999 not found"}
