"""Volume CRUD + volumecontent + sandbox mount contract tests."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings
from control_plane.registry.volumes import VolumeRegistry
from envd_service.runtime.registry import RuntimeRegistry


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_volume_crud_and_content(control_client):
    created = await control_client.post(
        "/volumes", headers={"X-API-Key": "local-key"}, json={"name": "data"}
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["volumeID"].startswith("vol_")
    assert payload["name"] == "data"
    assert payload["token"].startswith("tok_")
    token = payload["token"]
    vid = payload["volumeID"]
    headers = {"Authorization": f"Bearer {token}"}

    upload = await control_client.put(
        f"/volumecontent/{vid}/file",
        headers=headers,
        params={"path": "a.txt"},
        content=b"volume-data",
    )
    assert upload.status_code == 201
    assert upload.json()["name"] == "a.txt"
    assert upload.json()["type"] == "file"

    read = await control_client.get(
        f"/volumecontent/{vid}/file", headers=headers, params={"path": "a.txt"}
    )
    assert read.status_code == 200
    assert read.content == b"volume-data"

    listed = await control_client.get(
        f"/volumecontent/{vid}/dir", headers=headers, params={"path": "", "depth": 1}
    )
    assert listed.status_code == 200
    assert [e["name"] for e in listed.json()] == ["a.txt"]

    info = await control_client.get(f"/volumes/{vid}", headers={"X-API-Key": "local-key"})
    assert info.status_code == 200
    assert info.json()["volumeID"] == vid

    deleted = await control_client.delete(
        f"/volumes/{vid}", headers={"X-API-Key": "local-key"}
    )
    assert deleted.status_code == 204
    gone = await control_client.get(f"/volumes/{vid}", headers={"X-API-Key": "local-key"})
    assert gone.status_code == 404


async def test_volume_create_per_sandbox_quota_metadata(control_client):
    created = await control_client.post(
        "/volumes",
        headers={"X-API-Key": "local-key"},
        json={"name": "data", "perSandboxQuotaMb": 1024},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["perSandboxQuotaMb"] == 1024
    vid = payload["volumeID"]

    info = await control_client.get(
        f"/volumes/{vid}", headers={"X-API-Key": "local-key"}
    )
    assert info.status_code == 200
    assert info.json()["perSandboxQuotaMb"] == 1024

    default = await control_client.post(
        "/volumes",
        headers={"X-API-Key": "local-key"},
        json={"name": "plain"},
    )
    assert default.status_code == 201
    assert default.json()["perSandboxQuotaMb"] == 0

    bad = await control_client.post(
        "/volumes",
        headers={"X-API-Key": "local-key"},
        json={"name": "bad", "perSandboxQuotaMb": -5},
    )
    assert bad.status_code == 400


async def test_volume_bad_token(control_client):
    created = await control_client.post(
        "/volumes", headers={"X-API-Key": "local-key"}, json={"name": "data"}
    )
    vid = created.json()["volumeID"]
    response = await control_client.get(
        f"/volumecontent/{vid}/path",
        headers={"Authorization": "Bearer wrong"},
        params={"path": "a.txt"},
    )
    assert response.status_code == 401


async def test_volume_token_revoked_returns_401(make_apps):
    control, _ = make_apps()
    async with _client(control) as client:
        created = await client.post(
            "/volumes", headers={"X-API-Key": "local-key"}, json={"name": "data"}
        )
        assert created.status_code == 201
        vid = created.json()["volumeID"]
        token = created.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        upload = await client.put(
            f"/volumecontent/{vid}/file",
            headers=headers,
            params={"path": "a.txt"},
            content=b"data",
        )
        assert upload.status_code == 201

        control.state.volumes.revoke_token(vid)
        read = await client.get(
            f"/volumecontent/{vid}/file",
            headers=headers,
            params={"path": "a.txt"},
        )
        assert read.status_code == 401
        # The volume itself still exists and stays listable.
        info = await client.get(
            f"/volumes/{vid}", headers={"X-API-Key": "local-key"}
        )
        assert info.status_code == 200
        assert info.json()["volumeID"] == vid


async def test_legacy_disk_volume_visible_and_accessible_after_redis_upgrade(
    workspace,
):
    """E3.3 review I1: pre-Redis volumes survive an upgrade to Redis mode.

    A volume created by a disk-only registry (no E2B_REDIS_URL) must stay
    visible, listable, and usable with its original token once the control
    plane runs with a Redis-backed VolumeRegistry.
    """
    fakeredis = pytest.importorskip("fakeredis")
    volume_root = workspace / "_volumes"
    legacy = VolumeRegistry(volume_root)
    record = legacy.create("pre-upgrade")
    # Current create() writes the E3.3 token fields even when unset; rewrite
    # the meta file as a genuine pre-E3.3 payload (keys absent).
    payload = record.to_storage_dict()
    payload.pop("token_expires_at", None)
    payload.pop("token_revoked", None)
    legacy._record_path(record.volume_id).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    vid = record.volume_id
    token = record.token

    server = fakeredis.FakeServer()
    upgraded = VolumeRegistry(
        volume_root, redis_client=fakeredis.FakeRedis(server=server)
    )
    control = create_control_app(
        settings=Settings(api_keys=("local-key",)),
        runtime_registry=RuntimeRegistry(workspace),
        workspace_base=workspace,
        volumes_registry=upgraded,
    )
    async with _client(control) as client:
        info = await client.get(
            f"/volumes/{vid}", headers={"X-API-Key": "local-key"}
        )
        assert info.status_code == 200
        assert info.json()["volumeID"] == vid

        headers = {"Authorization": f"Bearer {token}"}
        write = await client.put(
            f"/volumecontent/{vid}/file",
            headers=headers,
            params={"path": "a.txt"},
            content=b"legacy-data",
        )
        assert write.status_code == 201
        read = await client.get(
            f"/volumecontent/{vid}/file",
            headers=headers,
            params={"path": "a.txt"},
        )
        assert read.status_code == 200
        assert read.content == b"legacy-data"

        listed = await client.get("/volumes", headers={"X-API-Key": "local-key"})
        assert listed.status_code == 200
        assert [v["volumeID"] for v in listed.json()] == [vid]


async def test_deleted_volume_not_resurrected_by_replica_restart(workspace):
    """E3.3 fix2: replica A deletes a volume while replica B holds a stale
    disk copy; after B restarts the volume stays invisible (404) and its
    token stays invalid (401)."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    base_a = workspace / "_volumes-a"
    base_b = workspace / "_volumes-b"

    registry_a = VolumeRegistry(
        base_a, redis_client=fakeredis.FakeRedis(server=server)
    )
    registry_b = VolumeRegistry(
        base_b, redis_client=fakeredis.FakeRedis(server=server)
    )
    record = registry_a.create("shared")
    # Replica B saved the shared record, leaving its own disk copy.
    registry_b.save(registry_b.get(record.volume_id))

    control_a = create_control_app(
        settings=Settings(api_keys=("local-key",)),
        runtime_registry=RuntimeRegistry(workspace),
        workspace_base=workspace,
        volumes_registry=registry_a,
    )
    async with _client(control_a) as client:
        deleted = await client.delete(
            f"/volumes/{record.volume_id}", headers={"X-API-Key": "local-key"}
        )
        assert deleted.status_code == 204

    # Replica B restarts with its stale disk copy.
    restarted_b = VolumeRegistry(
        base_b, redis_client=fakeredis.FakeRedis(server=server)
    )
    control_b = create_control_app(
        settings=Settings(api_keys=("local-key",)),
        runtime_registry=RuntimeRegistry(workspace),
        workspace_base=workspace,
        volumes_registry=restarted_b,
    )
    async with _client(control_b) as client:
        info = await client.get(
            f"/volumes/{record.volume_id}", headers={"X-API-Key": "local-key"}
        )
        assert info.status_code == 404
        read = await client.get(
            f"/volumecontent/{record.volume_id}/file",
            headers={"Authorization": f"Bearer {record.token}"},
            params={"path": "a.txt"},
        )
        assert read.status_code == 401
        listed = await client.get("/volumes", headers={"X-API-Key": "local-key"})
        assert listed.status_code == 200
        assert listed.json() == []


async def test_volume_token_expires_after_ttl(make_apps):
    control, _ = make_apps(
        control_settings=Settings(api_keys=("local-key",), volume_token_ttl_s=1)
    )
    async with _client(control) as client:
        created = await client.post(
            "/volumes", headers={"X-API-Key": "local-key"}, json={"name": "data"}
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["tokenExpiresAt"].endswith("Z")
        vid = payload["volumeID"]
        token = payload["token"]
        headers = {"Authorization": f"Bearer {token}"}
        write = await client.put(
            f"/volumecontent/{vid}/file",
            headers=headers,
            params={"path": "a.txt"},
            content=b"data",
        )
        assert write.status_code == 201

        await asyncio.sleep(1.2)
        read = await client.get(
            f"/volumecontent/{vid}/file",
            headers=headers,
            params={"path": "a.txt"},
        )
        assert read.status_code == 401


async def test_volume_mount_persists_across_sandboxes(make_apps, workspace):
    control, envd = make_apps()
    async with _client(control) as client:
        volume = await client.post(
            "/volumes", headers={"X-API-Key": "local-key"}, json={"name": "data"}
        )
        vid = volume.json()["volumeID"]
        token = volume.json()["token"]
        await client.put(
            f"/volumecontent/{vid}/file",
            headers={"Authorization": f"Bearer {token}"},
            params={"path": "persist.txt"},
            content=b"persist",
        )
        first = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "volumeMounts": [{"name": vid, "path": "mnt/data"}],
            },
        )
        assert first.status_code == 201
        await client.delete(
            f"/sandboxes/{first.json()['sandboxID']}",
            headers={"X-API-Key": "local-key"},
        )
        second = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "volumeMounts": [{"name": vid, "path": "mnt/data"}],
            },
        )
        assert second.status_code == 201
        assert (workspace / second.json()["sandboxID"] / "mnt" / "data" / "persist.txt").is_symlink() or (
            workspace / second.json()["sandboxID"] / "mnt" / "data" / "persist.txt"
        ).exists()
        await client.delete(
            f"/sandboxes/{second.json()['sandboxID']}",
            headers={"X-API-Key": "local-key"},
        )


async def test_missing_volume_mount_404(control_client):
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={
            "templateID": "base",
            "volumeMounts": [{"name": "vol_missing", "path": "mnt/data"}],
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == 404
