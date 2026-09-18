"""N28/A: a paused sandbox refuses writes and commands, and still answers reads.

Pause used to mean one thing only: ``SIGSTOP`` the exec children that were
already running (M4 D5). Everything else kept working while paused -- a new
command started, ``sb.files.write`` wrote, ``MakeDir``/``Move``/``Remove``
moved the tree around. That left a paused sandbox doing work nobody had
reserved capacity for, and it made the pause a *partial* freeze that the SDK
had no way to reason about: the same call succeeded or failed depending on
whether the caller happened to already have a process running.

The rule this file pins is the narrow one that makes pause mean something:
while the sandbox is not ``running``,

* every write is refused, and refused as a *state* problem the caller can fix
  by resuming (409 over HTTP, ``failed_precondition`` over Connect-RPC), not
  as a missing path or a permissions accident;
* every read still answers -- an operator has to be able to look at a paused
  sandbox before resuming it, and a read consumes nothing;
* restoring the state makes exactly the same call succeed.

The state is set through the shared runtime registry's own ``set_state``,
which is the same call the control plane's pause path lands on (over the
worker agent's ``/agent/sandboxes/{id}/pause`` route in the separated shape).
"""

from __future__ import annotations

import json

from envd_service.connect.codec import decode_envelopes_with_flags, encode_message

PAUSED_WRITE_MESSAGE = (
    "Sandbox is paused; its files can only be modified while it is running "
    "(resume it first)"
)
PAUSED_COMMAND_MESSAGE = (
    "Sandbox is paused; run a command only while it is running (resume it first)"
)


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


def _headers(sandbox: dict, content_type: str | None = None) -> dict:
    headers = {
        "E2b-Sandbox-Id": sandbox["sandboxID"],
        "X-Access-Token": sandbox["envdAccessToken"],
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def _set_state(apps, sandbox_id: str, state: str) -> None:
    """The worker-side half of a pause: the record the request path reads."""
    apps[1].state.runtime_registry.set_state(sandbox_id, state)


async def _unary(envd_client, sandbox, method: str, payload: dict):
    return await envd_client.post(
        f"/filesystem.Filesystem/{method}",
        headers=_headers(sandbox, "application/json"),
        content=json.dumps(payload).encode(),
    )


async def _start_command(envd_client, sandbox, cmd: str):
    request = {
        "process": {"cmd": cmd, "args": [], "envs": {}, "cwd": "/"},
        "stdin": False,
    }
    return await envd_client.post(
        "/process.Process/Start",
        headers=_headers(sandbox, "application/connect+json"),
        content=encode_message(request),
    )


def _end_stream_error(response) -> dict | None:
    """The ``error`` of the last envelope in a Connect stream response."""
    envelopes = decode_envelopes_with_flags(response.content)
    assert envelopes, "the stream carried no envelope at all"
    flags, payload = envelopes[-1]
    assert flags == 0x02, f"the stream did not end with an EndStream (got {flags})"
    return payload.get("error")


async def _upload(envd_client, sandbox, path: str, data: bytes):
    return await envd_client.post(
        "/files",
        headers=_headers(sandbox, "application/octet-stream"),
        params={"path": path},
        content=data,
    )


# -- writes are refused ------------------------------------------------------


async def test_a_paused_sandbox_refuses_an_upload(control_client, envd_client, apps):
    sandbox = await _create_sandbox(control_client)
    _set_state(apps, sandbox["sandboxID"], "paused")

    response = await _upload(envd_client, sandbox, "a.txt", b"payload")

    assert response.status_code == 409
    assert response.json() == {"message": PAUSED_WRITE_MESSAGE}


async def test_a_paused_sandbox_refuses_every_mutating_rpc(
    control_client, envd_client, apps
):
    sandbox = await _create_sandbox(control_client)
    assert (await _upload(envd_client, sandbox, "seen.txt", b"x")).status_code == 200
    _set_state(apps, sandbox["sandboxID"], "paused")

    for method, payload in (
        ("MakeDir", {"path": "dir"}),
        ("Move", {"source": "seen.txt", "destination": "moved.txt"}),
        ("Remove", {"path": "seen.txt"}),
    ):
        response = await _unary(envd_client, sandbox, method, payload)
        assert response.status_code == 400, method
        assert response.json() == {
            "code": "failed_precondition",
            "message": (
                f"Sandbox is paused; modify files only while it is running "
                "(resume it first)"
            ),
        }, method

    # And nothing moved: the refusal is before the operation, not a rollback.
    # (``workspace/`` is the template the control plane provisions.)
    listed = await _unary(envd_client, sandbox, "ListDir", {"path": "", "depth": 1})
    assert [e["path"] for e in listed.json()["entries"]] == ["seen.txt", "workspace"]


async def test_a_paused_sandbox_refuses_a_new_command(
    control_client, envd_client, apps
):
    sandbox = await _create_sandbox(control_client)
    _set_state(apps, sandbox["sandboxID"], "paused")

    response = await _start_command(envd_client, sandbox, "/bin/echo")

    assert response.status_code == 200
    assert _end_stream_error(response) == {
        "code": "failed_precondition",
        "message": PAUSED_COMMAND_MESSAGE,
    }


async def test_an_orphaned_sandbox_is_gated_the_same_way(
    control_client, envd_client, apps
):
    """The gate is "not running", not "paused" (E6.1 orphans a lost node)."""
    sandbox = await _create_sandbox(control_client)
    _set_state(apps, sandbox["sandboxID"], "orphaned")

    response = await _upload(envd_client, sandbox, "a.txt", b"payload")

    assert response.status_code == 409
    assert response.json() == {
        "message": (
            "Sandbox is orphaned; its files can only be modified while it is "
            "running (resume it first)"
        )
    }


# -- reads are not ----------------------------------------------------------


async def test_a_paused_sandbox_still_answers_reads(control_client, envd_client, apps):
    sandbox = await _create_sandbox(control_client)
    assert (await _upload(envd_client, sandbox, "seen.txt", b"payload")).status_code == 200
    _set_state(apps, sandbox["sandboxID"], "paused")

    downloaded = await envd_client.get(
        "/files", headers=_headers(sandbox), params={"path": "seen.txt"}
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"payload"

    stat = await _unary(envd_client, sandbox, "Stat", {"path": "seen.txt"})
    assert stat.status_code == 200
    assert stat.json()["entry"]["size"] == "7"

    listed = await _unary(envd_client, sandbox, "ListDir", {"path": "", "depth": 1})
    assert [e["path"] for e in listed.json()["entries"]] == ["seen.txt", "workspace"]


# -- and resuming gives the call straight back ------------------------------


async def test_resuming_lets_the_same_write_through(control_client, envd_client, apps):
    sandbox = await _create_sandbox(control_client)
    _set_state(apps, sandbox["sandboxID"], "paused")
    assert (await _upload(envd_client, sandbox, "a.txt", b"late")).status_code == 409

    _set_state(apps, sandbox["sandboxID"], "running")

    assert (await _upload(envd_client, sandbox, "a.txt", b"late")).status_code == 200
    downloaded = await envd_client.get(
        "/files", headers=_headers(sandbox), params={"path": "a.txt"}
    )
    assert downloaded.content == b"late"


# -- the delivery route is the one that moves the state ---------------------


async def _agent_pause(envd_client, sandbox, *, verb: str, reason: str | None = None):
    return await envd_client.post(
        f"/agent/sandboxes/{sandbox['sandboxID']}/{verb}",
        headers={"X-Internal-Key": "internal-key"},
        json={"reason": reason} if reason else None,
    )


async def test_the_pause_push_is_what_gates_a_remote_worker(
    control_client, envd_client, apps
):
    """The delivery route must move the state the gate reads (N28/A).

    This is the plumbing a unit test of the gate alone cannot see: the agent
    pause route used to freeze the sandbox's process groups *without* touching
    the worker's own runtime record, so on a separated node the record still
    said ``running`` and both gates waved the caller through. The route is
    driven here exactly as the control plane drives it (``POST`` with the
    internal key, optional ``reason``).
    """
    sandbox = await _create_sandbox(control_client)
    assert (await _upload(envd_client, sandbox, "a.txt", b"x")).status_code == 200

    assert (await _agent_pause(envd_client, sandbox, verb="pause")).status_code == 204
    refused = await _upload(envd_client, sandbox, "b.txt", b"y")
    assert refused.status_code == 409
    assert refused.json() == {"message": PAUSED_WRITE_MESSAGE}

    assert (await _agent_pause(envd_client, sandbox, verb="resume")).status_code == 204
    assert (await _upload(envd_client, sandbox, "b.txt", b"y")).status_code == 200


async def test_a_platform_pause_says_why(control_client, envd_client, apps):
    """A reason pushed with the pause is what the refusal quotes back (N28/D)."""
    sandbox = await _create_sandbox(control_client)
    reason = "its workspace grew past its budget (1340 MiB used of 1024 MiB)"

    await _agent_pause(envd_client, sandbox, verb="pause", reason=reason)

    refused = await _upload(envd_client, sandbox, "a.txt", b"x")
    assert refused.status_code == 409
    assert refused.json() == {
        "message": (
            f"Sandbox is paused: {reason}; its files can only be modified "
            "while it is running (resume it first)"
        )
    }
    command = await _start_command(envd_client, sandbox, "/bin/echo")
    assert _end_stream_error(command) == {
        "code": "failed_precondition",
        "message": (
            f"Sandbox is paused: {reason}; run a command only while it is "
            "running (resume it first)"
        ),
    }

    # Resume drops it: the reason belongs to the pause that produced it.
    await _agent_pause(envd_client, sandbox, verb="resume")
    assert (await _upload(envd_client, sandbox, "a.txt", b"x")).status_code == 200
    await _agent_pause(envd_client, sandbox, verb="pause")
    assert (await _upload(envd_client, sandbox, "a.txt", b"x")).json() == {
        "message": PAUSED_WRITE_MESSAGE
    }
