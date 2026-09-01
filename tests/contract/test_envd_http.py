"""Envd HTTP endpoint contract tests."""

from __future__ import annotations

from pathlib import Path

import httpx

from envd_service.config import Settings as EnvdSettings


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={
            "templateID": "base",
            "timeout": 300,
            "envVars": {"FOO": "bar"},
        },
    )
    assert response.status_code == 201
    return response.json()


def _headers(sandbox: dict) -> dict:
    return {
        "E2b-Sandbox-Id": sandbox["sandboxID"],
        "X-Access-Token": sandbox["envdAccessToken"],
    }


async def test_health_204(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.get("/health", headers=_headers(sandbox))
    assert response.status_code == 204
    assert response.content == b""


async def test_health_missing_sandbox_502(control_client, envd_client):
    response = await envd_client.get(
        "/health",
        headers={"E2b-Sandbox-Id": "sbx_gone", "X-Access-Token": "tok"},
    )
    assert response.status_code == 502


async def test_envs(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.get("/envs", headers=_headers(sandbox))
    assert response.status_code == 200
    assert response.json() == {"FOO": "bar"}


async def test_metrics(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.get("/metrics", headers=_headers(sandbox))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"cpu", "memory", "disk"}
    assert payload["memory"]["totalBytes"] == 512 * 1024 * 1024


async def test_file_download_exact(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    upload = await envd_client.post(
        "/files",
        headers={**_headers(sandbox), "Content-Type": "application/octet-stream"},
        params={"path": "workspace/script.py"},
        content=b"print(1+1)\n",
    )
    assert upload.status_code == 200
    assert upload.json() == [
        {
            "name": "script.py",
            "type": "file",
            "path": "workspace/script.py",
            "metadata": None,
        }
    ]
    download = await envd_client.get(
        "/files", headers=_headers(sandbox), params={"path": "workspace/script.py"}
    )
    assert download.status_code == 200
    assert download.content == b"print(1+1)\n"
    assert download.headers["content-type"].startswith("application/octet-stream")


async def test_file_multipart_upload(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.post(
        "/files",
        headers=_headers(sandbox),
        files={"file": ("workspace/a.txt", b"hello")},
    )
    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "a.txt",
            "type": "file",
            "path": "workspace/a.txt",
            "metadata": None,
        }
    ]


async def test_file_octet_stream_over_limit_413(make_apps):
    """E4.2: oversized worker files.write is rejected with 413 and leaves no
    partial file."""
    control, envd = make_apps(
        envd_settings=EnvdSettings(executor="local", max_file_write_mb=1)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as control_client:
        sandbox = await _create_sandbox(control_client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as envd_client:
        response = await envd_client.post(
            "/files",
            headers={
                **_headers(sandbox),
                "Content-Type": "application/octet-stream",
            },
            params={"path": "workspace/big.bin"},
            content=b"\0" * (1024 * 1024 + 1),
        )
        assert response.status_code == 413
        assert response.json() == {
            "message": "File exceeds maximum upload size"
        }

        missing = await envd_client.get(
            "/files", headers=_headers(sandbox), params={"path": "workspace/big.bin"}
        )
        assert missing.status_code == 404
        runtime = envd.state.runtime_registry.get(sandbox["sandboxID"])
        leftovers = list((Path(runtime.workspace_dir) / "workspace").glob(".*.tmp"))
        assert leftovers == []


async def test_file_multipart_over_limit_413(make_apps):
    """E4.2: oversized multipart file part is rejected with 413."""
    control, envd = make_apps(
        envd_settings=EnvdSettings(executor="local", max_file_write_mb=1)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as control_client:
        sandbox = await _create_sandbox(control_client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as envd_client:
        response = await envd_client.post(
            "/files",
            headers=_headers(sandbox),
            files={"file": ("workspace/multi.bin", b"\0" * (1024 * 1024 + 1))},
        )
        assert response.status_code == 413
        assert response.json() == {
            "message": "File exceeds maximum upload size"
        }

        missing = await envd_client.get(
            "/files", headers=_headers(sandbox), params={"path": "workspace/multi.bin"}
        )
        assert missing.status_code == 404


async def test_file_missing_404(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.get(
        "/files", headers=_headers(sandbox), params={"path": "nope.txt"}
    )
    assert response.status_code == 404
    assert response.json()["message"] == "Path nope.txt not found"


async def test_file_path_traversal_rejected(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.get(
        "/files", headers=_headers(sandbox), params={"path": "../outside"}
    )
    assert response.status_code == 400


async def test_envd_unauthorized(control_client, envd_client):
    sandbox = await _create_sandbox(control_client)
    response = await envd_client.get(
        "/envs",
        headers={
            "E2b-Sandbox-Id": sandbox["sandboxID"],
            "X-Access-Token": "bad",
        },
    )
    assert response.status_code == 401


async def test_warm_peek_requires_internal_key(envd_client):
    response = await envd_client.get("/agent/images/127.0.0.1:1/nope:latest/warm")
    assert response.status_code == 401


async def test_warm_peek_returns_uncached_for_unknown(make_apps):
    _, envd = make_apps(envd_settings=EnvdSettings(executor="sandlock"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        response = await client.get(
            "/agent/images/127.0.0.1:1/nope:latest/warm",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["cached"] is False
    assert body["digest"] is None


async def test_warm_now_fails_fast_for_unreachable_registry(make_apps):
    _, envd = make_apps(envd_settings=EnvdSettings(executor="sandlock"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agent/images/127.0.0.1:1/nope:latest/warm",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert response.status_code == 500
