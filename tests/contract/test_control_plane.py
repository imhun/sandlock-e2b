"""Control-plane contract tests (official HTTP surface)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from control_plane.config import Settings


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client, headers=None, **overrides):
    body = {
        "templateID": "base",
        "timeout": 300,
        "metadata": {"user": "alice"},
        "envVars": {"MY_VAR": "value"},
        "secure": True,
        "allow_internet_access": False,
    }
    body.update(overrides)
    return await client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key", **(headers or {})},
        json=body,
    )


async def test_create_sandbox_201(control_client):
    response = await _create(control_client)
    assert response.status_code == 201
    payload = response.json()
    assert payload["sandboxID"].startswith("sbx_")
    assert payload["clientID"].startswith("cli_")
    assert payload["envdAccessToken"].startswith("tok_")
    assert payload["envdVersion"] == "0.6.4+sandlock"
    assert payload["templateID"] == "base"
    assert payload["trafficAccessToken"] is None
    assert payload["domain"] == "localhost"


async def test_create_with_template_image(make_apps, workspace):
    """A template mapped to an OCI image creates through the documented path.

    The create carries a client-chosen ``X-Sandbox-Id``: that is what lets the
    control plane take the slow path and warm a cold image on the node, where
    a header-less create fast-fails with ``428 warm_required`` instead
    (covered by ``test_scaling.py``, because that branch needs the sandlock
    executor and so is not portable to the macOS runner).
    """
    control, envd = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            template_images={"python3.12": "python:3.12-slim"},
        )
    )
    sandbox_id = f"sbx_{uuid.uuid4().hex[:16]}"
    async with _client(control) as client:
        response = await _create(
            client, templateID="python3.12", headers={"X-Sandbox-Id": sandbox_id}
        )
    assert response.status_code == 201
    assert response.json()["templateID"] == "python3.12"
    assert response.json()["sandboxID"] == sandbox_id


async def test_create_unknown_template_400(control_client):
    response = await _create(control_client, templateID="does-not-exist")
    assert response.status_code == 400
    assert response.json() == {"code": 400, "message": "Template does-not-exist not found"}


async def test_create_with_iam_accepted(control_client):
    """The SDK workload-identity config (iam) is accepted end to end."""
    response = await _create(
        control_client,
        iam={
            "tokens": {
                "openai": {"audience": "test-aud", "token_type": "JWT-SVID"}
            }
        },
    )
    assert response.status_code == 201


async def test_create_with_invalid_iam_name_rejected(control_client):
    response = await _create(
        control_client,
        iam={"tokens": {"bad{name": {"audience": "a", "token_type": "JWT-SVID"}}},
    )
    assert response.status_code == 400


async def test_create_with_invalid_iam_token_rejected(control_client):
    response = await _create(
        control_client,
        iam={"tokens": {"openai": {"audience": 42}}},
    )
    assert response.status_code == 400


async def test_image_field_rejected_400(control_client):
    response = await _create(control_client, image="python:3.11-slim")
    assert response.status_code == 400
    assert response.json() == {"code": 400, "message": "Unsupported field: image"}


async def test_network_field_accepted(control_client):
    response = await _create(
        control_client, network={"denyOut": ["10.0.0.0/8"]}
    )
    assert response.status_code == 201
    sandbox_id = response.json()["sandboxID"]
    try:
        detail = (
            await control_client.get(
                f"/sandboxes/{sandbox_id}",
                headers={"X-API-Key": "local-key"},
            )
        ).json()
        assert detail["network"]["denyOut"] == ["10.0.0.0/8"]
    finally:
        await control_client.delete(
            f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
        )


async def test_wrong_api_key_401(control_client):
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "wrong"},
        json={"templateID": "base"},
    )
    assert response.status_code == 401
    assert response.json() == {"code": 401, "message": "Unauthorized"}


async def test_missing_api_key_401(control_client):
    response = await control_client.post("/sandboxes", json={"templateID": "base"})
    assert response.status_code == 401
    assert response.json() == {"code": 401, "message": "Unauthorized"}


async def test_get_info_and_kill(control_client):
    created = await _create(control_client)
    sandbox_id = created.json()["sandboxID"]
    info = await control_client.get(
        f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
    )
    assert info.status_code == 200
    detail = info.json()
    assert detail["sandboxID"] == sandbox_id
    assert detail["state"] == "running"
    assert detail["envdAccessToken"].startswith("tok_")

    killed = await control_client.delete(
        f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
    )
    assert killed.status_code == 204

    gone = await control_client.get(
        f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
    )
    assert gone.status_code == 404
    assert gone.json() == {"code": 404, "message": f"Sandbox {sandbox_id} not found"}


async def test_delete_missing_404(control_client):
    response = await control_client.delete(
        "/sandboxes/sbx_missing", headers={"X-API-Key": "local-key"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == 404


async def test_connect_and_timeout(control_client):
    created = await _create(control_client)
    sandbox_id = created.json()["sandboxID"]
    connected = await control_client.post(
        f"/sandboxes/{sandbox_id}/connect",
        headers={"X-API-Key": "local-key"},
        json={"timeout": 600},
    )
    assert connected.status_code == 200
    assert connected.json()["sandboxID"] == sandbox_id

    timeout = await control_client.post(
        f"/sandboxes/{sandbox_id}/timeout",
        headers={"X-API-Key": "local-key"},
        json={"timeout": 900},
    )
    assert timeout.status_code == 204

    missing = await control_client.post(
        "/sandboxes/sbx_missing/connect",
        headers={"X-API-Key": "local-key"},
        json={"timeout": 300},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == 404


async def test_list_pagination(make_apps, workspace):
    control, envd = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            max_sandboxes=10,
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        )
    )
    async with _client(control) as client:
        for i in range(3):
            response = await _create(client)
            assert response.status_code == 201

        first = await client.get(
            "/v2/sandboxes", params={"limit": 2}, headers={"X-API-Key": "local-key"}
        )
        assert first.status_code == 200
        assert len(first.json()) == 2
        next_token = first.headers.get("X-Next-Token")
        assert next_token is not None
        assert next_token == "2"

        second = await client.get(
            "/v2/sandboxes",
            params={"limit": 2, "nextToken": next_token},
            headers={"X-API-Key": "local-key"},
        )
        assert second.status_code == 200
        assert len(second.json()) == 1
        assert "X-Next-Token" not in second.headers


async def test_list_legacy_and_filters(control_client):
    await _create(control_client, metadata={"team": "a"})
    await _create(control_client, metadata={"team": "b"})
    legacy = await control_client.get("/sandboxes", headers={"X-API-Key": "local-key"})
    assert legacy.status_code == 200
    assert len(legacy.json()) == 2

    filtered = await control_client.get(
        "/v2/sandboxes",
        params={"metadata": "team=a", "state": "running"},
        headers={"X-API-Key": "local-key"},
    )
    assert filtered.status_code == 200
    assert len(filtered.json()) == 1
    assert filtered.json()[0]["metadata"] == {"team": "a"}


async def test_resource_exhausted_503(control_client):
    app = control_client._transport.app
    from control_plane.registry.manager import ResourceUnavailableError

    original = app.state.registry.create

    def blocked(**kwargs):
        raise ResourceUnavailableError("No resources available")

    app.state.registry.create = blocked
    try:
        response = await _create(control_client)
        assert response.status_code == 503
        assert response.json() == {"code": 503, "message": "No resources available"}
    finally:
        app.state.registry.create = original


async def test_unsupported_endpoints_return_official_error(control_client):
    sandbox = (await _create(control_client)).json()
    sid = sandbox["sandboxID"]
    # The network API is implemented; only the egress proxy parts are
    # rejected, and those return an explicit 400 (not a fake success).
    response = await control_client.put(
        f"/sandboxes/{sid}/network",
        headers={"X-API-Key": "local-key"},
        json={"egressProxy": {"address": "p:1080"}},
    )
    assert response.status_code == 400
    assert "egressProxy" in response.json()["message"]

    templates = await control_client.post(
        "/templates/anything", headers={"X-API-Key": "local-key"}, json={}
    )
    assert templates.status_code == 501
    assert templates.json() == {"code": 501, "message": "Unsupported: templates"}
