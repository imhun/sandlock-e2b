"""A silent command must not look like a dead connection (N37).

Measured on the fleet 2026-09-27: the reverse proxy in front of the API cuts a
streaming response whose body has been silent for **60.0 s**, and the SDK
reports that as ``TimeoutException: ... unexpected EOF during chunk size
line``. A 4000-file write on the NFS workspace takes ~94 s and prints nothing
until it is done, so it died; the *same* work split into 2000-file commands
(47 s each) survived, which is how the size of the tree came to look like the
variable when the real one was the length of the silence.

The official SDK asks for pings on this stream -- ``Keepalive-Ping-Interval``
on every ``Start`` -- so these tests drive the real app and assert the stream
carries the protocol's ``keepalive`` event while the command is quiet.
``tests/unit/test_process_stream_keepalive.py`` pins the relay itself; this
file pins the wiring and the numbers against the *installed* SDK.
"""

from __future__ import annotations

from envd_service.connect.codec import (
    decode_envelopes,
    decode_envelopes_with_flags,
    encode_message,
)
from gateway_common.keepalive import (
    EDGE_IDLE_CUT_S,
    SDK_KEEPALIVE_PING_INTERVAL_S,
    STREAM_KEEPALIVE_MAX_S,
)

#: A 3 s silence at a 1 s ping interval. The count can only be 2 or 3: the
#: pings land at t=1 and t=2 for certain, the one at t=3 races the command's
#: own end (a timer cannot promise which side of a boundary it falls on, so
#: the assertion below states the bound instead of inventing a number).
_SILENT_COMMAND = {"cmd": "/bin/sh", "args": ["-c", "sleep 3; echo done"]}
_PING_INTERVAL_S = 1


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


def _headers(sandbox: dict, ping_interval: int | None = _PING_INTERVAL_S) -> dict:
    headers = {
        "E2b-Sandbox-Id": sandbox["sandboxID"],
        "X-Access-Token": sandbox["envdAccessToken"],
        "Content-Type": "application/connect+json",
    }
    if ping_interval is not None:
        headers["Keepalive-Ping-Interval"] = str(ping_interval)
    return headers


async def _start(envd_client, sandbox: dict, request: dict, ping_interval=_PING_INTERVAL_S):
    return await envd_client.post(
        "/process.Process/Start",
        headers=_headers(sandbox, ping_interval),
        content=encode_message(request),
    )


def test_the_installed_sdk_asks_for_the_interval_we_assume() -> None:
    """The header and its value are the SDK's, so read them from the SDK.

    Exact equality (not a range): a client that starts asking for something
    else -- or renames the header -- invalidates the clamp in
    ``gateway_common.keepalive`` and must fail here rather than on the fleet.
    """
    from e2b.connection_config import KEEPALIVE_PING_HEADER, KEEPALIVE_PING_INTERVAL_SEC

    assert KEEPALIVE_PING_HEADER == "Keepalive-Ping-Interval"
    assert KEEPALIVE_PING_INTERVAL_SEC == SDK_KEEPALIVE_PING_INTERVAL_S
    assert STREAM_KEEPALIVE_MAX_S < EDGE_IDLE_CUT_S


async def test_a_silent_command_carries_keepalive_events(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await _start(
        envd_client,
        sandbox,
        {"process": {**_SILENT_COMMAND, "envs": {}, "cwd": "/"}, "stdin": False},
    )
    assert response.status_code == 200
    messages = decode_envelopes(response.content)

    # The last envelope carries the end-stream flag and an empty body, so the
    # events are everything before it: start, pings, one data event, end.
    assert messages[-1] == {}
    events = [message["event"] for message in messages[:-1]]
    assert "pid" in events[0]["start"]
    assert events[-1] == {
        "end": {
            "exitCode": 0,
            "exited": True,
            "status": "exited",
            "error": None,
        }
    }
    body = events[1:-1]
    assert body[-1] == {"data": {"stdout": "ZG9uZQo="}}  # "done\n"
    pings = body[:-1]
    assert all(ping == {"keepalive": {}} for ping in pings)
    assert len(pings) in (2, 3)


async def test_a_busy_command_pays_one_ping_per_interval_at_most(
    control_client, envd_client
):
    """Output is relayed as it comes; a ping never displaces an event.

    With a 5 s interval and a command that finishes in well under it, the
    stream is exactly start/data/end -- the ping is a fallback for silence,
    not a heartbeat that interleaves with real output.
    """
    sandbox = await _create_sandbox(control_client)
    response = await _start(
        envd_client,
        sandbox,
        {
            "process": {
                "cmd": "/bin/echo",
                "args": ["hello"],
                "envs": {},
                "cwd": "/",
            },
            "stdin": False,
        },
        ping_interval=5,
    )
    messages = decode_envelopes(response.content)
    assert messages[-1] == {}
    events = [message["event"] for message in messages[:-1]]
    assert "pid" in events[0]["start"]
    assert events[1:-1] == [{"data": {"stdout": "aGVsbG8K"}}]  # "hello\n"
    assert events[-1]["end"]["exitCode"] == 0


async def test_no_keepalive_is_sent_after_the_end_event(control_client, envd_client):
    """The relay stops at the end event, so nothing trails the exit status.

    A ping after ``end`` would arrive on a stream the SDK has already
    finished, and the envelope after it would be unread.
    """
    sandbox = await _create_sandbox(control_client)
    response = await _start(
        envd_client,
        sandbox,
        {"process": {**_SILENT_COMMAND, "envs": {}, "cwd": "/"}, "stdin": False},
    )
    flagged = decode_envelopes_with_flags(response.content)
    end_positions = [
        index
        for index, (_flags, payload) in enumerate(flagged)
        if "end" in payload.get("event", {})
    ]
    assert len(end_positions) == 1
    assert flagged[-1] == (2, {})
    assert [
        payload
        for _flags, payload in flagged[end_positions[0] + 1 :]
        if payload != {}
    ] == []
