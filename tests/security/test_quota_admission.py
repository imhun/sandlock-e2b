"""Total-resource admission: 503, no runtime start, release on kill/TTL."""

from __future__ import annotations

import httpx

from control_plane.config import Settings


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_total_memory_503_no_runtime(make_apps, workspace):
    control, envd = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            # Admission-only test: never require a base image on the local
            # node (executor=auto would warm-peek E2B_BASE_IMAGE and 428 on
            # a cold image cache).
            executor="local",
            default_memory_mb=512,
            max_total_memory_mb=512,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        )
    )
    async with _client(control) as client:
        first = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert first.status_code == 201
        second = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert second.status_code == 503
        assert second.json() == {"code": 503, "message": "No resources available"}

        # No runtime/workspace was created for the rejected sandbox.
        assert len(envd.state.runtime_registry.list()) == 1

        killed = await client.delete(
            f"/sandboxes/{first.json()['sandboxID']}",
            headers={"X-API-Key": "local-key"},
        )
        assert killed.status_code == 204
        third = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert third.status_code == 201


async def test_total_cpu_503(make_apps):
    control, envd = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            executor="local",
            default_cpu_percent=100,
            max_total_cpu_percent=100,
            max_total_memory_mb=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        )
    )
    async with _client(control) as client:
        first = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base"},
        )
        assert first.status_code == 201
        second = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base"},
        )
        assert second.status_code == 503


async def test_ttl_release_restores_creation(make_apps):
    control, envd = make_apps(
        control_settings=Settings(
            api_keys=("local-key",),
            executor="local",
            default_memory_mb=512,
            max_total_memory_mb=512,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        )
    )
    import datetime

    async with _client(control) as client:
        first = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert first.status_code == 201
        record = control.state.registry.get(first.json()["sandboxID"])
        record.end_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        control.state.registry.remove_expired()
        second = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert second.status_code == 201
