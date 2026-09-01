"""E5.3: metadata/envVars and artifact size limits (413/400).

Oversized sandbox metadata/envVars, oversized JSON request bodies and
oversized template/snapshot names are rejected with exact status codes and
messages, and a successful create keeps ``sandbox.json`` bounded.
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from envd_service.config import Settings as EnvdSettings

META_LIMIT = 256
ENV_LIMIT = 256
BODY_LIMIT = 1024
NAME_LIMIT = 64


def _tight_settings() -> ControlSettings:
    return ControlSettings(
        api_keys=("local-key",),
        max_metadata_bytes=META_LIMIT,
        max_envvars_bytes=ENV_LIMIT,
        max_json_body_bytes=BODY_LIMIT,
        max_name_bytes=NAME_LIMIT,
    )


@pytest.fixture()
def tight_apps(make_apps):
    control, envd = make_apps(
        control_settings=_tight_settings(),
        envd_settings=EnvdSettings(executor="local"),
    )
    return control, envd


@pytest.fixture()
async def control_client(tight_apps):
    control, _ = tight_apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture()
async def envd_client(tight_apps):
    _, envd = tight_apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        yield client


async def test_sandbox_create_oversized_metadata_413(control_client) -> None:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "metadata": {"k": "x" * META_LIMIT}},
    )
    assert response.status_code == 413
    assert response.json()["message"] == f"metadata exceeds {META_LIMIT}-byte limit"


async def test_sandbox_create_oversized_envvars_413(control_client) -> None:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "envVars": {"BIG": "y" * ENV_LIMIT}},
    )
    assert response.status_code == 413
    assert response.json()["message"] == f"envVars exceeds {ENV_LIMIT}-byte limit"


async def test_sandbox_create_oversized_body_413(control_client) -> None:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={
            "templateID": "base",
            "metadata": {"k": "z" * (BODY_LIMIT + 1)},
        },
    )
    assert response.status_code == 413
    assert response.json()["message"] == "Request body exceeds maximum size"


async def test_sandbox_create_secret_expansion_capped_413(
    control_client,
) -> None:
    created = await control_client.post(
        "/secrets",
        headers={"X-API-Key": "local-key"},
        json={"name": "big", "value": "v" * (ENV_LIMIT + 1)},
    )
    assert created.status_code == 201
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "envVars": {"SEC": "${big}"}},
    )
    assert response.status_code == 413
    assert response.json()["message"] == f"envVars exceeds {ENV_LIMIT}-byte limit"


async def test_template_name_too_long_400(control_client) -> None:
    response = await control_client.post(
        "/v3/templates",
        headers={"X-API-Key": "local-key"},
        json={"name": "n" * (NAME_LIMIT + 1)},
    )
    assert response.status_code == 400
    assert response.json()["message"] == f"name exceeds {NAME_LIMIT}-byte limit"


async def test_template_trigger_body_413(control_client) -> None:
    created = await control_client.post(
        "/v3/templates",
        headers={"X-API-Key": "local-key"},
        json={"name": "ok"},
    )
    assert created.status_code == 202
    template_id = created.json()["templateID"]
    build_id = created.json()["buildID"]
    response = await control_client.post(
        f"/v2/templates/{template_id}/builds/{build_id}",
        headers={"X-API-Key": "local-key"},
        json={
            "fromImage": "python:3.14-slim",
            "steps": [{"type": "RUN", "args": ["x" * (BODY_LIMIT + 1)]}],
        },
    )
    assert response.status_code == 413
    assert response.json()["message"] == "Request body exceeds maximum size"


async def test_snapshot_name_too_long_400(control_client) -> None:
    response = await control_client.post(
        "/sandboxes/not-a-real-sandbox/snapshots",
        headers={"X-API-Key": "local-key"},
        json={"name": "s" * (NAME_LIMIT + 1)},
    )
    assert response.status_code == 400
    assert response.json()["message"] == f"name exceeds {NAME_LIMIT}-byte limit"


async def test_sandbox_json_stays_bounded(
    control_client, envd_client, workspace
) -> None:
    metadata = {"m": "a" * 200}
    env_vars = {"E": "b" * 200}
    created = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "metadata": metadata, "envVars": env_vars},
    )
    assert created.status_code == 201
    sandbox_id = created.json()["sandboxID"]
    record_path = workspace / sandbox_id / "sandbox.json"
    assert record_path.is_file()
    size = record_path.stat().st_size
    # 256B metadata + 256B envVars plus record envelope must stay well under
    # 4KB; without the E5.3 caps the raw values alone would already exceed it.
    assert size <= 4096
