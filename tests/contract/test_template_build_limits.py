"""E3.5: template build concurrency cap + per-key rate limit."""

from __future__ import annotations

import asyncio

import httpx

from control_plane.api import templates as tmpl
from control_plane.config import Settings


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _create_template(client) -> dict:
    resp = await client.post(
        "/v3/templates",
        headers={"X-API-Key": "local-key"},
        json={"name": "limit-tpl"},
    )
    assert resp.status_code == 202
    return resp.json()


def _trigger_body() -> dict:
    return {"fromImage": "python:3.11-slim", "steps": [{"type": "RUN", "args": ["true"]}]}


async def _wait_ready(client, template_id, build_id) -> None:
    for _ in range(100):
        status = await client.get(
            f"/templates/{template_id}/builds/{build_id}/status",
            headers={"X-API-Key": "local-key"},
        )
        assert status.status_code == 200
        if status.json()["status"] in ("ready", "error"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("build never reached a terminal state")


async def test_build_concurrency_cap_rejects_over_limit(
    make_apps, monkeypatch
):
    control, _ = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            template_build_concurrency=1,
        )
    )
    release = asyncio.Event()

    async def _slow_build(app, template, build, dockerfile, workspace_base):
        await release.wait()
        build.status = "ready"

    monkeypatch.setattr(tmpl, "_run_build", _slow_build)
    async with _client(control) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        build_id = info["buildID"]

        first = await client.post(
            f"/v2/templates/{template_id}/builds/{build_id}",
            headers={"X-API-Key": "local-key"},
            json=_trigger_body(),
        )
        assert first.status_code == 202

        second = await client.post(
            f"/v2/templates/{template_id}/builds/{build_id}",
            headers={"X-API-Key": "local-key"},
            json=_trigger_body(),
        )
        assert second.status_code == 429
        assert second.json() == {
            "code": 429,
            "message": "Template build concurrency limit exceeded",
        }

        # Releasing the running build frees the slot; a new trigger passes.
        release.set()
        await _wait_ready(client, template_id, build_id)
        retry = await client.post(
            f"/v2/templates/{template_id}/builds/{build_id}",
            headers={"X-API-Key": "local-key"},
            json=_trigger_body(),
        )
        assert retry.status_code == 202
        release.set()
        await _wait_ready(client, template_id, build_id)


async def test_build_rate_limit_per_key(make_apps, monkeypatch):
    control, _ = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            template_build_rate_limit_per_min=1,
        )
    )

    async def _fast_build(app, template, build, dockerfile, workspace_base):
        build.status = "ready"

    monkeypatch.setattr(tmpl, "_run_build", _fast_build)
    async with _client(control) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        build_id = info["buildID"]

        first = await client.post(
            f"/v2/templates/{template_id}/builds/{build_id}",
            headers={"X-API-Key": "local-key"},
            json=_trigger_body(),
        )
        assert first.status_code == 202
        await _wait_ready(client, template_id, build_id)

        second = await client.post(
            f"/v2/templates/{template_id}/builds/{build_id}",
            headers={"X-API-Key": "local-key"},
            json=_trigger_body(),
        )
        assert second.status_code == 429
        assert second.json() == {
            "code": 429,
            "message": "Template build rate limit exceeded",
        }


async def test_build_concurrency_disabled_when_zero(make_apps, monkeypatch):
    control, _ = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            template_build_concurrency=0,
        )
    )
    release = asyncio.Event()

    async def _blocked_build(app, template, build, dockerfile, workspace_base):
        await release.wait()
        build.status = "ready"

    monkeypatch.setattr(tmpl, "_run_build", _blocked_build)
    async with _client(control) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        build_id = info["buildID"]
        for _ in range(3):
            resp = await client.post(
                f"/v2/templates/{template_id}/builds/{build_id}",
                headers={"X-API-Key": "local-key"},
                json=_trigger_body(),
            )
            assert resp.status_code == 202
        release.set()
        await _wait_ready(client, template_id, build_id)
