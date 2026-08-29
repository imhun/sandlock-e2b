"""Command stdout/stderr output merged into GET /sandboxes/{id}/logs."""

from __future__ import annotations

import httpx

from envd_service.connect.codec import encode_message

from e2b import Sandbox


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


async def _run(envd_client, sandbox, cmd: list[str], args: list[str]):
    request = {
        "process": {
            "cmd": cmd[0],
            "args": args,
            "envs": {},
            "cwd": "/",
        },
        "stdin": False,
    }
    response = await envd_client.post(
        "/process.Process/Start",
        headers={**_headers(sandbox), "Content-Type": "application/connect+json"},
        content=encode_message(request),
    )
    assert response.status_code == 200


async def _logs(control_client, sandbox_id) -> list[dict]:
    response = await control_client.get(
        f"/sandboxes/{sandbox_id}/logs", headers={"X-API-Key": "local-key"}
    )
    assert response.status_code == 200
    return response.json()


async def test_command_stdout_in_logs(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    await _run(envd_client, sandbox, ["/bin/echo"], ["hello"])

    logs = await _logs(control_client, sandbox["sandboxID"])
    lines = [log["line"] for log in logs]
    assert "> /bin/echo hello" in lines
    assert "hello" in lines
    assert "exit: 0" in lines


async def test_command_stderr_in_logs(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    await _run(envd_client, sandbox, ["/bin/sh"], ["-c", "echo err >&2"])

    logs = await _logs(control_client, sandbox["sandboxID"])
    lines = [log["line"] for log in logs]
    assert "stderr: err" in lines
    assert "exit: 0" in lines


async def test_command_multiline_and_limit(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    await _run(
        envd_client,
        sandbox,
        ["/bin/sh"],
        ["-c", "printf 'alpha\\nbeta\\ngamma\\n'"],
    )

    logs = await _logs(control_client, sandbox["sandboxID"])
    lines = [log["line"] for log in logs]
    assert lines[-4:] == [
        "alpha",
        "beta",
        "gamma",
        "exit: 0",
    ]

    limited = await control_client.get(
        f"/sandboxes/{sandbox['sandboxID']}/logs?limit=2",
        headers={"X-API-Key": "local-key"},
    )
    assert limited.status_code == 200
    assert [log["line"] for log in limited.json()] == ["gamma", "exit: 0"]


async def test_remote_command_output_in_logs(multinode_servers):
    """Command logs recorded on the worker are merged through the agent API."""
    sandbox = Sandbox.create(
        api_url=multinode_servers["api_url"],
        sandbox_url=multinode_servers["sandbox_url"],
        api_key="local-key",
    )
    try:
        assert sandbox.commands.run("echo remote-log").stdout == "remote-log\n"
        async with httpx.AsyncClient(
            base_url=multinode_servers["api_url"]
        ) as client:
            response = await client.get(
                f"/sandboxes/{sandbox.sandbox_id}/logs",
                headers={"X-API-Key": "local-key"},
            )
        assert response.status_code == 200
        lines = [log["line"] for log in response.json()]
        assert "remote-log" in lines
        assert any(line.startswith("> ") for line in lines)
    finally:
        sandbox.kill()
