"""Path traversal and malicious sandbox IDs (runs on any platform)."""

from __future__ import annotations

import httpx


async def _create(control_client):
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


async def test_file_traversal_rejected(control_client, envd_client):
    sandbox = await _create(control_client)
    for bad in ("../secret", "workspace/../../secret", ".."):
        response = await envd_client.get(
            "/files", headers=_headers(sandbox), params={"path": bad}
        )
        assert response.status_code == 400
        upload = await envd_client.post(
            "/files",
            headers={**_headers(sandbox), "Content-Type": "application/octet-stream"},
            params={"path": bad},
            content=b"x",
        )
        assert upload.status_code == 400


async def test_filesystem_rpc_traversal_rejected(control_client, envd_client):
    import json

    sandbox = await _create(control_client)
    response = await envd_client.post(
        "/filesystem.Filesystem/Stat",
        headers={**_headers(sandbox), "Content-Type": "application/json"},
        content=json.dumps({"path": "../escape"}).encode(),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_argument"


async def test_malicious_sandbox_id_rejected(control_client, envd_client):
    await _create(control_client)
    for bad in ("../etc", "a/b", "a b", "sbx_.."):
        response = await envd_client.get(
            "/health",
            headers={"E2b-Sandbox-Id": bad, "X-Access-Token": "tok"},
        )
        assert response.status_code in (401, 502)
        info = await control_client.get(
            f"/sandboxes/{bad}", headers={"X-API-Key": "local-key"}
        )
        assert info.status_code == 404

