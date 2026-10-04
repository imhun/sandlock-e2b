"""SEC-R3-01: envd's access-token guard must fail CLOSED (runs on any platform).

The defect
----------
``require_http_sandbox`` (envd_service/http/auth.py) and ``_find_sandbox``
(envd_service/connect/router.py) both read::

    if runtime.access_token and token != runtime.access_token:
        refuse

The leading ``runtime.access_token and`` makes an EMPTY token satisfy the
condition, so the guard never refuses. ``secure`` was a client-supplied field on
``POST /sandboxes``: ``{"secure": false}`` stored an empty token, and the
externally reachable gateway authenticates nothing while forwarding any
caller-chosen ``E2b-Sandbox-Id``. Measured end to end against the online k0s
deployment through the public entry, with no credentials at all:
``POST /process.Process/Start`` returned 200 and ran ``id`` as uid 0.

The property under test
-----------------------
For every sandbox, and for both halves of the control surface, a request is
served only when the presented token equals a non-empty expected token. "No
token", "empty token" and "wrong token" are all refusals.

The two halves are separate functions on separate routes, so they are tested
separately: a fix applied to only one of them leaves the other as a bypass.
That is not hypothetical -- the same split is what SEC-K0S-005 had to fix.
"""

from __future__ import annotations

import json

import pytest

#: The Connect RPC that executes a command. It is the sharpest end of the
#: surface, so it is the one the exploit used and the one asserted here.
START_RPC = "/process.Process/Start"


def _connect_body(cmd: str = "/bin/sh", args: list[str] | None = None) -> bytes:
    """One ``application/connect+json`` message: 1 flag byte + 4 length + JSON."""
    payload = {"process": {"cmd": cmd, "args": args if args is not None else ["-c", "true"]}}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return (b"\x00" + len(raw).to_bytes(4, "big")) + raw


def _connect_envelopes(response) -> list[dict]:
    """Every decoded message in a Connect response, framed or not."""
    body = response.content
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        document = None
    if isinstance(document, dict):
        return [document]
    messages: list[dict] = []
    offset = 0
    while offset + 5 <= len(body):
        length = int.from_bytes(body[offset + 1 : offset + 5], "big")
        offset += 5
        if offset + length > len(body):
            break
        chunk = body[offset : offset + length]
        offset += length
        if chunk:
            messages.append(json.loads(chunk))
    return messages


def _decode_connect_error(response) -> tuple[str, str]:
    """Pull ``(code, message)`` out of whatever shape the refusal came in.

    The connect layer answers either a plain JSON body or a framed envelope
    depending on the content type it was called with, and a refusal must be
    asserted on its *content*, not on which of the two shapes carried it.
    """
    for message in _connect_envelopes(response):
        if isinstance(message, dict) and "error" in message:
            return message["error"]["code"], message["error"]["message"]
    raise AssertionError(f"no connect error payload in response: {response.content!r}")


async def _create(control_client, **extra):
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _blank_the_token(apps, sandbox_id: str) -> None:
    """Put the runtime into the state the vulnerability needed.

    Done through the registry rather than through ``secure=false`` on purpose:
    the API now refuses that field (asserted separately below), so the only way
    to reach an empty-token runtime is the persisted-record path this guard is
    the last line for.
    """
    _, envd_app = apps
    runtime = envd_app.state.runtime_registry.get(sandbox_id)
    assert runtime is not None, "sandbox runtime was not registered"
    runtime.access_token = ""


# --------------------------------------------------------------------------
# The API boundary: secure=false must be refused outright
# --------------------------------------------------------------------------


async def test_secure_false_is_refused(control_client):
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300, "secure": False},
    )
    assert response.status_code == 400
    assert response.json()["message"] == (
        "secure=false is no longer supported: envd always requires an access "
        "token. Use allowPublicTraffic for reachability, not for "
        "authentication."
    )


async def test_secure_true_still_mints_a_non_empty_token(control_client):
    sandbox = await _create(control_client, secure=True)
    assert sandbox["envdAccessToken"] != ""


# --------------------------------------------------------------------------
# HTTP half: /files
# --------------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "not-the-token"])
async def test_files_rejects_every_wrong_token(apps, control_client, envd_client, token):
    sandbox = await _create(control_client, secure=True)
    await _blank_the_token(apps, sandbox["sandboxID"])

    headers = {"E2b-Sandbox-Id": sandbox["sandboxID"]}
    if token is not None:
        headers["X-Access-Token"] = token
    response = await envd_client.get("/files", headers=headers, params={"path": "x"})
    assert response.status_code == 401
    assert response.json() == {"message": "Invalid access token"}


async def test_files_accepts_the_exact_token(control_client, envd_client):
    """The control for the test above: the fixture is not refusing everything."""
    sandbox = await _create(control_client, secure=True)
    response = await envd_client.get(
        "/files",
        headers={
            "E2b-Sandbox-Id": sandbox["sandboxID"],
            "X-Access-Token": sandbox["envdAccessToken"],
        },
        params={"path": "does-not-exist.txt"},
    )
    # 404 (the file is absent) rather than 401 (the token was refused).
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Connect half: process.Process/Start -- the RCE surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "not-the-token"])
async def test_process_start_rejects_every_wrong_token(
    apps, control_client, envd_client, token
):
    sandbox = await _create(control_client, secure=True)
    await _blank_the_token(apps, sandbox["sandboxID"])

    headers = {
        "E2b-Sandbox-Id": sandbox["sandboxID"],
        "Content-Type": "application/connect+json",
    }
    if token is not None:
        headers["X-Access-Token"] = token
    response = await envd_client.post(
        START_RPC, headers=headers, content=_connect_body()
    )
    code, message = _decode_connect_error(response)
    assert code == "unauthenticated"
    assert message == "Invalid access token"


async def test_process_start_rejects_a_missing_sandbox_id(envd_client):
    """No sandbox id at all is still a refusal, and a different reason."""
    response = await envd_client.post(
        START_RPC,
        headers={"Content-Type": "application/connect+json"},
        content=_connect_body(),
    )
    code, message = _decode_connect_error(response)
    assert code == "unauthenticated"
    assert message == "Missing E2b-Sandbox-Id header"


async def test_process_start_accepts_the_exact_token(control_client, envd_client):
    """The control: with the real token the RPC is served, not refused.

    Asserted on the success shape (a start event and a clean end) rather than on
    the absence of a refusal: "no error" would also pass for a response the
    handler never produced. The command's own output is not asserted -- the
    executor in this lane is the local one and its output is not this test's
    subject; the token decision is.
    """
    sandbox = await _create(control_client, secure=True)
    response = await envd_client.post(
        START_RPC,
        headers={
            "E2b-Sandbox-Id": sandbox["sandboxID"],
            "X-Access-Token": sandbox["envdAccessToken"],
            "Content-Type": "application/connect+json",
        },
        content=_connect_body(),
    )
    envelopes = _connect_envelopes(response)
    starts = [e for e in envelopes if "start" in e.get("event", {})]
    ends = [e for e in envelopes if "end" in e.get("event", {})]
    assert len(starts) == 1
    assert ends[0]["event"]["end"]["exited"] is True
    assert ends[0]["event"]["end"]["error"] is None