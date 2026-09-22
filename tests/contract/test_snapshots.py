"""Fork and snapshot contract tests."""

from __future__ import annotations

import shutil
import uuid


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


async def test_a_snapshot_retry_with_the_same_key_answers_the_first_one(
    control_client, envd_client
):
    """N29: the endpoint the client retries must not do the work twice.

    Measured on the cluster (2026-09-21): a 2000-file tree outlives the entry
    proxy's 60 s, so the client's retry arrives while the first copy is still
    running -- and without a key the control plane minted a *new* snapshot id
    and copied the tree again. With `Idempotency-Key` the retry names the same
    request: the second call answers 200 with the same snapshot, the list still
    has one entry, and its payload is the one the first call made.
    """
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]
    upload = await envd_client.post(
        "/files",
        headers={
            "E2b-Sandbox-Id": sid,
            "X-Access-Token": sandbox["envdAccessToken"],
            "Content-Type": "application/octet-stream",
        },
        params={"path": "workspace/retry.txt"},
        content=b"retry-content",
    )
    assert upload.status_code in (200, 201)

    key = "snap-idem-0123456789abcdef"
    first = await control_client.post(
        f"/sandboxes/{sid}/snapshots",
        headers={"X-API-Key": "local-key", "Idempotency-Key": key},
        json={"name": "retry-once"},
    )
    assert first.status_code == 201
    created = first.json()
    assert created["snapshotID"] == key

    second = await control_client.post(
        f"/sandboxes/{sid}/snapshots",
        headers={"X-API-Key": "local-key", "Idempotency-Key": key},
        json={"name": "retry-once"},
    )
    assert second.status_code == 200
    assert second.json() == {
        "snapshotID": key,
        "names": ["retry-once"],
        "status": "completed",
        "alreadyExists": True,
    }

    listed = await control_client.get(
        "/snapshots", headers={"X-API-Key": "local-key"}
    )
    assert [s["snapshotID"] for s in listed.json()] == [key]


async def test_a_snapshot_retry_without_a_key_is_a_new_snapshot(
    control_client, envd_client
):
    """The SDK sends only `name`, so a retry from it is a *second* snapshot.

    Pinned rather than wished away: it is why the docs tell a retrying caller
    to send `Idempotency-Key` (or to list snapshots first), and it is the
    behaviour a caller who genuinely wants two snapshots with the same name
    still gets.
    """
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]

    first = await control_client.post(
        f"/sandboxes/{sid}/snapshots",
        headers={"X-API-Key": "local-key"},
        json={"name": "same-name"},
    )
    second = await control_client.post(
        f"/sandboxes/{sid}/snapshots",
        headers={"X-API-Key": "local-key"},
        json={"name": "same-name"},
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["snapshotID"] != second.json()["snapshotID"]

    listed = await control_client.get(
        "/snapshots", headers={"X-API-Key": "local-key"}
    )
    assert len([s for s in listed.json() if s["names"] == ["same-name"]]) == 2


async def test_the_worker_answers_a_finished_payload_as_completed(apps, envd_client):
    """The worker half of the same rule: 200 for "done", 409 for "half done".

    The control plane's retry has to know whether a payload for that id is
    *finished*. The `.complete` marker is what separates the two cases: a
    directory without it is a crashed attempt (a concurrent duplicate is still
    refused rather than interleaved), so a retry costs nothing only when the
    copy really finished.

    The tree is built under the *worker's* base on purpose: this route reads
    `settings.workspace_base`, which in this fixture is not the control
    plane's `workspace` -- the existing tests all went through the control
    plane's in-process copy and never touched this route.
    """
    from pathlib import Path

    _, envd_app = apps
    base = Path(envd_app.state.settings.workspace_base)
    # Unique per run: the worker's base is the *repository's* `tmp/sandboxes`
    # on this lane, so a fixed id would make the test depend on what a previous
    # run left behind (measured: the second run of this file answered 409 for
    # its own leftover payload).
    suffix = uuid.uuid4().hex[:12]
    sandbox_id = f"sbx_worker_{suffix}"
    (base / sandbox_id / "workspace").mkdir(parents=True, exist_ok=True)
    (base / sandbox_id / "workspace" / "f.txt").write_text("x", encoding="utf-8")

    headers = {"X-Internal-Key": "internal-key"}
    snapshot_id = f"snap_worker_{suffix}"
    half = f"snap_worker_half_{suffix}"
    try:
        first = await envd_client.post(
            "/agent/snapshots",
            headers=headers,
            json={"snapshotID": snapshot_id, "sandboxID": sandbox_id},
        )
        assert first.status_code == 201

        payload = base / "_snapshots" / snapshot_id
        assert (payload / ".complete").is_file()

        again = await envd_client.post(
            "/agent/snapshots",
            headers=headers,
            json={"snapshotID": snapshot_id, "sandboxID": sandbox_id},
        )
        assert again.status_code == 200
        assert again.json() == {
            "snapshotID": snapshot_id,
            "sandboxID": sandbox_id,
            "status": "completed",
            "alreadyExists": True,
        }

        # A half payload (no marker) is *not* "completed": the same id stays
        # refused instead of being reported as a snapshot nobody finished.
        (base / "_snapshots" / half / "fs").mkdir(parents=True)
        refused = await envd_client.post(
            "/agent/snapshots",
            headers=headers,
            json={"snapshotID": half, "sandboxID": sandbox_id},
        )
        assert refused.status_code == 409
    finally:
        shutil.rmtree(base / "_snapshots" / snapshot_id, ignore_errors=True)
        shutil.rmtree(base / "_snapshots" / half, ignore_errors=True)
        shutil.rmtree(base / sandbox_id, ignore_errors=True)
