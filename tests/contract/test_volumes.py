"""Volume CRUD + volumecontent + sandbox mount contract tests."""

from __future__ import annotations

import httpx


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

