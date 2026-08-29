"""Secret CRUD + env-var injection contract tests."""

from __future__ import annotations


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

