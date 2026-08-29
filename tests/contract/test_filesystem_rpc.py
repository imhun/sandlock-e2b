"""Filesystem Connect-RPC contract tests."""

from __future__ import annotations

import json


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


async def _unary(envd_client, sandbox, method: str, payload: dict):
    return await envd_client.post(
        f"/filesystem.Filesystem/{method}",
        headers={**_headers(sandbox), "Content-Type": "application/json"},
        content=json.dumps(payload).encode(),
    )


async def test_make_dir_stat_list(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    made = await _unary(
        envd_client, sandbox, "MakeDir", {"path": "workspace/dir/a"}
    )
    assert made.status_code == 200
    assert made.json()["entry"]["type"] == "FILE_TYPE_DIRECTORY"
    assert made.json()["entry"]["path"] == "workspace/dir/a"

    duplicate = await _unary(
        envd_client, sandbox, "MakeDir", {"path": "workspace/dir/a"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "code": "already_exists",
        "message": "Path workspace/dir/a already exists",
    }

    stat = await _unary(envd_client, sandbox, "Stat", {"path": "workspace/dir/a"})
    assert stat.status_code == 200
    assert stat.json()["entry"]["name"] == "a"

    listed = await _unary(envd_client, sandbox, "ListDir", {"path": "workspace", "depth": 0})
    assert listed.status_code == 200
    paths = {e["path"] for e in listed.json()["entries"]}
    assert {"workspace/dir", "workspace/dir/a"} <= paths


async def test_stat_missing_not_found(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    stat = await _unary(envd_client, sandbox, "Stat", {"path": "nope.txt"})
    assert stat.status_code == 404
    assert stat.json()["code"] == "not_found"


async def test_move_and_remove(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    await envd_client.post(
        "/files",
        headers={**_headers(sandbox), "Content-Type": "application/octet-stream"},
        params={"path": "workspace/src.txt"},
        content=b"data",
    )
    moved = await _unary(
        envd_client,
        sandbox,
        "Move",
        {"source": "workspace/src.txt", "destination": "workspace/dst.txt"},
    )
    assert moved.status_code == 200
    assert moved.json()["entry"]["path"] == "workspace/dst.txt"

    removed = await _unary(envd_client, sandbox, "Remove", {"path": "workspace/dst.txt"})
    assert removed.status_code == 200
    assert removed.json() == {}

    gone = await _unary(envd_client, sandbox, "Stat", {"path": "workspace/dst.txt"})
    assert gone.status_code == 404


async def test_watcher_create_get_remove(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    created = await _unary(
        envd_client, sandbox, "CreateWatcher", {"path": "workspace", "recursive": False}
    )
    assert created.status_code == 200
    wid = created.json()["watcherId"]
    assert wid.startswith("watch_")

    events = await _unary(envd_client, sandbox, "GetWatcherEvents", {"watcherId": wid})
    assert events.status_code == 200
    assert events.json() == {"events": []}

    removed = await _unary(envd_client, sandbox, "RemoveWatcher", {"watcherId": wid})
    assert removed.status_code == 200
    assert removed.json() == {}

    gone = await _unary(envd_client, sandbox, "GetWatcherEvents", {"watcherId": wid})
    assert gone.status_code == 404

