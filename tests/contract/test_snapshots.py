"""Fork and snapshot contract tests."""

from __future__ import annotations


async def _create(control_client, **overrides):
    body = {"templateID": "base", "timeout": 300, "envVars": {"K": "v"}}
    body.update(overrides)
    response = await control_client.post(
        "/sandboxes", headers={"X-API-Key": "local-key"}, json=body
    )
    assert response.status_code == 201
    return response.json()


async def test_snapshot_create_list_delete(control_client, envd_client):
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]

    # Put a file in the sandbox so the snapshot carries content.
    upload = await envd_client.post(
        "/files",
        headers={
            "E2b-Sandbox-Id": sid,
            "X-Access-Token": sandbox["envdAccessToken"],
            "Content-Type": "application/octet-stream",
        },
        params={"path": "workspace/snap.txt"},
        content=b"snapshot-content",
    )
    assert upload.status_code == 200

    created = await control_client.post(
        f"/sandboxes/{sid}/snapshots",
        headers={"X-API-Key": "local-key"},
        json={"name": "release"},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["snapshotID"].startswith("snap_")
    assert payload["names"] == ["release"]

    listed = await control_client.get(
        "/snapshots", headers={"X-API-Key": "local-key"}
    )
    assert listed.status_code == 200
    assert any(s["snapshotID"] == payload["snapshotID"] for s in listed.json())

    deleted = await control_client.delete(
        f"/templates/{payload['snapshotID']}", headers={"X-API-Key": "local-key"}
    )
    assert deleted.status_code == 204
    gone = await control_client.delete(
        f"/templates/{payload['snapshotID']}", headers={"X-API-Key": "local-key"}
    )
    assert gone.status_code == 404


async def test_create_sandbox_from_snapshot(control_client, envd_client):
    source = await _create(control_client, envVars={"FROM_SNAP": "yes"})
    await envd_client.post(
        "/files",
        headers={
            "E2b-Sandbox-Id": source["sandboxID"],
            "X-Access-Token": source["envdAccessToken"],
            "Content-Type": "application/octet-stream",
        },
        params={"path": "workspace/kept.txt"},
        content=b"kept",
    )
    snapshot = await control_client.post(
        f"/sandboxes/{source['sandboxID']}/snapshots",
        headers={"X-API-Key": "local-key"},
        json={},
    )
    snap_id = snapshot.json()["snapshotID"]

    clone = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": snap_id, "timeout": 300},
    )
    assert clone.status_code == 201
    clone_sid = clone.json()["sandboxID"]
    envs = await envd_client.get(
        "/envs",
        headers={
            "E2b-Sandbox-Id": clone_sid,
            "X-Access-Token": clone.json()["envdAccessToken"],
        },
    )
    assert envs.json() == {"FROM_SNAP": "yes"}
    read = await envd_client.get(
        "/files",
        headers={
            "E2b-Sandbox-Id": clone_sid,
            "X-Access-Token": clone.json()["envdAccessToken"],
        },
        params={"path": "workspace/kept.txt"},
    )
    assert read.status_code == 200
    assert read.content == b"kept"


async def test_fork_creates_independent_sandboxes(control_client, envd_client):
    source = await _create(control_client)
    await envd_client.post(
        "/files",
        headers={
            "E2b-Sandbox-Id": source["sandboxID"],
            "X-Access-Token": source["envdAccessToken"],
            "Content-Type": "application/octet-stream",
        },
        params={"path": "workspace/forked.txt"},
        content=b"fork-data",
    )
    response = await control_client.post(
        f"/sandboxes/{source['sandboxID']}/fork",
        headers={"X-API-Key": "local-key"},
        json={"timeout": 300, "count": 2},
    )
    assert response.status_code == 201
    results = response.json()
    assert len(results) == 2
    sandboxes = [r["sandbox"] for r in results if "sandbox" in r]
    assert len(sandboxes) == 2
    assert sandboxes[0]["sandboxID"] != sandboxes[1]["sandboxID"]

    for fork in sandboxes:
        read = await envd_client.get(
            "/files",
            headers={
                "E2b-Sandbox-Id": fork["sandboxID"],
                "X-Access-Token": fork["envdAccessToken"],
            },
            params={"path": "workspace/forked.txt"},
        )
        assert read.content == b"fork-data"

    # Source sandbox still runs.
    source_info = await control_client.get(
        f"/sandboxes/{source['sandboxID']}", headers={"X-API-Key": "local-key"}
    )
    assert source_info.json()["state"] == "running"


async def test_fork_missing_sandbox_404(control_client):
    response = await control_client.post(
        "/sandboxes/sbx_missing/fork",
        headers={"X-API-Key": "local-key"},
        json={"timeout": 300, "count": 1},
    )
    assert response.status_code == 404


async def test_snapshot_missing_sandbox_404(control_client):
    response = await control_client.post(
        "/sandboxes/sbx_missing/snapshots",
        headers={"X-API-Key": "local-key"},
        json={},
    )
    assert response.status_code == 404
