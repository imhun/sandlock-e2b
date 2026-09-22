"""Fork and snapshot contract tests."""

from __future__ import annotations

import asyncio
import http.server
import shutil
import threading
import time
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


class _SlowWorker:
    """A worker that takes its time on the snapshot copy, and nothing else."""

    def __init__(self, copy_s: float) -> None:
        self.copy_s = copy_s
        slow = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # HTTP/1.1 with an explicit Content-Length: the control plane's
            # httpx client keeps the connection alive, and a 1.0-style server
            # that closes under it reads as "disconnected without a response".
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server's own name
                if self.path != "/agent/snapshots":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                # The copy: long enough that a blocked loop cannot hide it.
                time.sleep(slow.copy_s)
                body = b'{"status":"completed","alreadyExists":false}'
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # keep the suite's output clean
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


async def test_a_slow_copy_does_not_block_the_control_plane(
    apps, control_client
):
    """N32: the capture's worker call must not run on the event loop.

    ``_capture_snapshot`` reaches the worker with a *synchronous* HTTP call and
    can take as long as the tree is big -- 76 s for 2000 files on the cluster.
    Run on the loop, that stalls *everything* the control plane serves, and the
    timestamps it is judging nodes by are exactly what stops being recorded:
    the workers' heartbeats gap for one copy, the health sweep then orphaned
    two live nodes, and every later request for those sandboxes answered 409
    "not running" (the observable symptom in the backlog). So the copy is
    offloaded, and this test is the property that matters -- another request is
    served *while* the copy is in flight.
    """
    app, _ = apps
    copy_s = 1.5
    slow = _SlowWorker(copy_s)
    try:
        sandbox = await _create(control_client)
        sid = sandbox["sandboxID"]

        # Point the sandbox's node at the slow worker: the capture is the only
        # worker call this path makes, and it is the one that took 76 s.
        record = app.state.registry.get(sid)
        node = app.state.nodes.get(record.node_id)
        assert node is not None
        node.address = slow.url

        started = time.monotonic()
        snapshot = asyncio.create_task(
            control_client.post(
                f"/sandboxes/{sid}/snapshots",
                headers={"X-API-Key": "local-key"},
                json={"name": "slow"},
                timeout=30,
            )
        )
        # Wait until the capture is certainly in flight, then measure how long
        # the loop takes to come back to us, and then how long an unrelated read
        # takes to be answered. Both are the same question -- is the loop free
        # while the copy runs -- asked from inside and from the outside.
        nap_started = time.monotonic()
        await asyncio.sleep(copy_s / 3)
        nap = time.monotonic() - nap_started
        assert nap < copy_s / 2, (
            f"the event loop was unavailable for {nap:.2f}s of a {copy_s}s copy: "
            "the capture is running on it, so the heartbeats it is judging "
            "nodes by are not being recorded"
        )
        assert not snapshot.done(), "the copy finished before it could be measured"
        probe_started = time.monotonic()
        probe = await control_client.get(
            "/sandboxes", headers={"X-API-Key": "local-key"}, timeout=30
        )
        served = time.monotonic() - probe_started
        assert probe.status_code == 200
        assert served < copy_s / 2, (
            f"the control plane served an unrelated request {served:.2f}s into "
            f"a {copy_s}s copy -- the copy is running on the event loop"
        )

        response = await snapshot
        assert response.status_code == 201
        assert time.monotonic() - started >= copy_s
    finally:
        slow.stop()


async def test_a_slow_snapshot_delete_does_not_block_the_control_plane(
    apps, control_client, monkeypatch
):
    """The other half of the same shape: deleting a snapshot is an `rmtree`.

    The snapshot payload lives on the shared NAS tree (control plane and worker
    mount the same `_snapshots`), so `DELETE /templates/{id}` removes a whole
    sandbox tree: measured 17.1 s for 2000 files on the cluster, and it used to
    run on the event loop -- one `DELETE` whose access-log line only appeared
    17 s after the previous one, i.e. the same stall that makes the node-health
    sweep orphan live nodes (N32). Patched to sleep here so the property is
    testable without a 2000-file tree.
    """
    app, _ = apps
    sandbox = await _create(control_client)
    created = await control_client.post(
        f"/sandboxes/{sandbox['sandboxID']}/snapshots",
        headers={"X-API-Key": "local-key"},
        json={"name": "slowy-delete"},
    )
    assert created.status_code == 201
    snapshot_id = created.json()["snapshotID"]

    delete_s = 1.0
    original = app.state.snapshots.delete

    def slow_delete(sid: str):
        time.sleep(delete_s)
        return original(sid)

    monkeypatch.setattr(app.state.snapshots, "delete", slow_delete)

    deletion = asyncio.create_task(
        control_client.delete(
            f"/templates/{snapshot_id}",
            headers={"X-API-Key": "local-key"},
            timeout=30,
        )
    )
    nap_started = time.monotonic()
    await asyncio.sleep(delete_s / 3)
    nap = time.monotonic() - nap_started
    assert nap < delete_s / 2, (
        f"the event loop was unavailable for {nap:.2f}s of a {delete_s}s "
        "snapshot delete: the rmtree is running on it"
    )
    assert (await deletion).status_code == 204


async def test_a_slow_agent_snapshot_delete_does_not_block_the_worker(
    apps, envd_client, monkeypatch
):
    """The worker's own half: its snapshot delete is the same `rmtree`.

    `DELETE /agent/snapshots/{id}` removes the payload tree from the shared NAS
    on the *worker's* loop. Removing a 2000-file tree takes ~17 s there, and
    for those 17 s the worker sends no heartbeat at all -- measured on the
    cluster: the delete's response logged 17.4 s after its request, and the
    worker's heartbeat line was missing for 21.6 s (the request plus the
    interval). That gap is what the node-health window is compared against, so
    it is the worker-side twin of the control-plane stall (N32).
    """
    _, worker_app = apps
    deleted_s = 1.0
    rmtree_calls: list[str] = []
    real_rmtree = shutil.rmtree

    def slow_rmtree(path, *args, **kwargs):
        rmtree_calls.append(str(path))
        time.sleep(deleted_s)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("envd_service.agent.shutil.rmtree", slow_rmtree)

    snapshot_id = f"snap_agent_slow_{uuid.uuid4().hex[:8]}"
    payload = (
        worker_app.state.settings.workspace_base / "_snapshots" / snapshot_id
    )
    (payload / "fs").mkdir(parents=True, exist_ok=True)

    deletion = asyncio.create_task(
        envd_client.delete(
            f"/agent/snapshots/{snapshot_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
    )
    nap_started = time.monotonic()
    await asyncio.sleep(deleted_s / 3)
    nap = time.monotonic() - nap_started
    assert nap < deleted_s / 2, (
        f"the worker's event loop was unavailable for {nap:.2f}s of a "
        f"{deleted_s}s snapshot delete: the rmtree is running on it"
    )
    health = await envd_client.get(
        "/agent/health", headers={"X-Internal-Key": "internal-key"}
    )
    assert health.status_code == 200
    assert (await deletion).status_code == 204
    assert rmtree_calls


async def test_a_slow_agent_sandbox_delete_does_not_block_the_worker(
    envd_client, monkeypatch
):
    """And the sandbox teardown itself, which is what actually gapped.

    This is the endpoint from the cluster measurement that produced N32's
    largest *remaining* gap after the control plane was fixed: the worker's
    `DELETE /agent/sandboxes/{id}` removed a 2000-file tree on the shared NAS
    on its own loop, so its response was logged 17.4 s after the request and
    no heartbeat went out in between (worker heartbeat line missing for
    21.6 s = that delete plus one interval).
    """
    deleted_s = 1.0
    calls: list[str] = []

    def slow_teardown(settings, registry, sandbox_id, **kwargs) -> None:
        calls.append(sandbox_id)
        time.sleep(deleted_s)

    monkeypatch.setattr("envd_service.agent._delete_sandbox_runtime", slow_teardown)

    sandbox_id = f"sbx_agent_slow_{uuid.uuid4().hex[:8]}"
    deletion = asyncio.create_task(
        envd_client.delete(
            f"/agent/sandboxes/{sandbox_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
    )
    nap_started = time.monotonic()
    await asyncio.sleep(deleted_s / 3)
    nap = time.monotonic() - nap_started
    assert nap < deleted_s / 2, (
        f"the worker's event loop was unavailable for {nap:.2f}s of a "
        f"{deleted_s}s sandbox teardown: the removal is running on it"
    )
    health = await envd_client.get(
        "/agent/health", headers={"X-Internal-Key": "internal-key"}
    )
    assert health.status_code == 200
    assert (await deletion).status_code == 204
    assert calls == [sandbox_id]
