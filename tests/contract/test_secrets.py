"""Secret CRUD + env-var injection contract tests."""

from __future__ import annotations

import json

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.secrets import SecretRegistry


async def _create_secret(control_client, name="api_key", value="v1"):
    return await control_client.post(
        "/secrets",
        headers={"X-API-Key": "local-key"},
        json={"name": name, "value": value, "metadata": {"env": "test"}},
    )


async def test_secret_crud(control_client):
    created = await _create_secret(control_client)
    assert created.status_code == 201
    payload = created.json()
    assert payload["secretID"].startswith("sec_")
    assert payload["name"] == "api_key"
    assert payload["currentVersion"] == 1
    assert payload["metadata"] == {"env": "test"}
    assert "value" not in payload

    fetched = await control_client.get(
        f"/secrets/{payload['secretID']}", headers={"X-API-Key": "local-key"}
    )
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "api_key"

    updated = await control_client.post(
        f"/secrets/{payload['secretID']}",
        headers={"X-API-Key": "local-key"},
        json={"value": "v2"},
    )
    assert updated.status_code == 200
    assert updated.json()["currentVersion"] == 2

    listed = await control_client.get("/secrets", headers={"X-API-Key": "local-key"})
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["api_key"]

    deleted = await control_client.delete(
        f"/secrets/{payload['secretID']}", headers={"X-API-Key": "local-key"}
    )
    assert deleted.status_code == 204
    gone = await control_client.get(
        f"/secrets/{payload['secretID']}", headers={"X-API-Key": "local-key"}
    )
    assert gone.status_code == 404


async def test_duplicate_secret_400(control_client):
    await _create_secret(control_client, name="dup")
    response = await _create_secret(control_client, name="dup")
    assert response.status_code == 400


async def test_secret_injected_into_sandbox_env(control_client, envd_client):
    await _create_secret(control_client, name="tokensecret", value="42")
    created = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "envVars": {"TOKEN": "${tokensecret}"}},
    )
    assert created.status_code == 201
    envs = await envd_client.get(
        "/envs",
        headers={
            "E2b-Sandbox-Id": created.json()["sandboxID"],
            "X-Access-Token": created.json()["envdAccessToken"],
        },
    )
    assert envs.status_code == 200
    assert envs.json() == {"TOKEN": "42"}


async def test_secret_persists_across_restart_with_master_key_and_redis(
    workspace,
) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    redis_client = fakeredis.FakeRedis(server=server)
    settings = ControlSettings(api_keys=("local-key",), secret_master_key="m1")

    registry_a = SecretRegistry(
        workspace / "_secrets",
        redis_client=redis_client,
        master_key="m1",
    )
    control_a = create_control_app(
        settings=settings,
        secrets_registry=registry_a,
        workspace_base=workspace,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_a), base_url="http://test"
    ) as client:
        created = await client.post(
            "/secrets",
            headers={"X-API-Key": "local-key"},
            json={"name": "tok", "value": "s3cr3t"},
        )
        assert created.status_code == 201
        secret_id = created.json()["secretID"]

    raw = redis_client.get(f"e2b:secret:{secret_id}")
    payload = json.loads(raw)
    assert payload["encrypted"] is True
    assert payload["value"] != "s3cr3t"

    # Restart: a brand-new registry + control plane against the same Redis
    # and master key must still resolve the secret.
    registry_b = SecretRegistry(
        workspace / "_secrets_restarted",
        redis_client=redis_client,
        master_key="m1",
    )
    assert registry_b.get_by_name("tok").value == "s3cr3t"
    control_b = create_control_app(
        settings=settings,
        secrets_registry=registry_b,
        workspace_base=workspace,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_b), base_url="http://test"
    ) as client:
        listed = await client.get("/secrets", headers={"X-API-Key": "local-key"})
        assert listed.status_code == 200
        assert [s["name"] for s in listed.json()] == ["tok"]
