"""Fork and snapshot contract tests."""

from __future__ import annotations

import asyncio
import http.server
import shutil
import threading
import time
import uuid

import pytest


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
        #: How many copies were actually requested -- the async shape's whole
        #: point is that a retry does not add a second one.
        self.copies = 0
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
                slow.copies += 1
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


async def _poll_snapshot(control_client, snapshot_id: str, *, timeout=15.0):
    """Poll one snapshot until it stops being `creating`, and return it."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = await control_client.get(
            f"/snapshots/{snapshot_id}", headers={"X-API-Key": "local-key"}
        )
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] != "creating":
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"snapshot never settled: {last}")


async def test_async_snapshot_answers_immediately_then_completes(
    apps, control_client
):
    """N29 ①: the long copy must not have to fit in one HTTP request.

    The sync shape is what makes a 2000-file tree (measured ~75 s of copying)
    exceed the entry's 60 s read timeout, leaving the client unable to tell
    "failed" from "still running" -- and producing a second full copy when it
    retries. With ``Prefer: respond-async`` the answer comes back in
    milliseconds with the id, and the record's status is the completion signal.
    """
    app, _ = apps
    copy_s = 1.0
    slow = _SlowWorker(copy_s)
    try:
        sandbox = await _create(control_client)
        sid = sandbox["sandboxID"]
        record = app.state.registry.get(sid)
        node = app.state.nodes.get(record.node_id)
        node.address = slow.url

        started = time.monotonic()
        response = await control_client.post(
            f"/sandboxes/{sid}/snapshots",
            headers={"X-API-Key": "local-key", "Prefer": "respond-async"},
            json={"name": "async-one"},
        )
        answered = time.monotonic() - started
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "creating"
        assert body["names"] == ["async-one"]
        assert answered < copy_s / 2, (
            f"the 202 took {answered:.2f}s of a {copy_s}s copy: the request is "
            "waiting for the bytes"
        )

        # Still copying: a poll says so rather than pretending it is usable.
        during = await control_client.get(
            f"/snapshots/{body['snapshotID']}",
            headers={"X-API-Key": "local-key"},
        )
        assert during.status_code == 200
        assert during.json()["status"] == "creating"

        done = await _poll_snapshot(control_client, body["snapshotID"])
        assert done["status"] == "completed"
        assert slow.copies == 1
    finally:
        slow.stop()


async def test_an_async_retry_with_the_same_key_does_not_copy_again(
    apps, control_client
):
    """The idempotency rule has to hold *while* the copy is in flight.

    The client's retry arrives during the copy -- that is the whole reason the
    key exists -- so the second request must answer with the state of the first
    one (202 + ``creating``, ``alreadyExists``) and start nothing.
    """
    app, _ = apps
    copy_s = 1.0
    slow = _SlowWorker(copy_s)
    try:
        sandbox = await _create(control_client)
        sid = sandbox["sandboxID"]
        node = app.state.nodes.get(app.state.registry.get(sid).node_id)
        node.address = slow.url
        key = f"snap_async_{uuid.uuid4().hex[:10]}"
        headers = {
            "X-API-Key": "local-key",
            "Prefer": "respond-async",
            "Idempotency-Key": key,
        }
        first = await control_client.post(
            f"/sandboxes/{sid}/snapshots", headers=headers, json={"name": "async-key"}
        )
        assert first.status_code == 202
        assert first.json()["snapshotID"] == key

        again = await control_client.post(
            f"/sandboxes/{sid}/snapshots", headers=headers, json={"name": "async-key"}
        )
        assert again.status_code == 202, again.text
        assert again.json() == {
            "snapshotID": key,
            "names": ["async-key"],
            "status": "creating",
            "alreadyExists": True,
        }
        # The count is asserted once the copy has finished: mid-flight the
        # first copy may not even have reached the worker yet, and what this
        # test is about is that the *retry* never becomes a second one.
        done = await _poll_snapshot(control_client, key)
        assert done["status"] == "completed"
        assert slow.copies == 1, "the retry started a second copy"
        # Once finished, the same key is the plain N29 retry answer.
        settled = await control_client.post(
            f"/sandboxes/{sid}/snapshots", headers=headers, json={"name": "async-key"}
        )
        assert settled.status_code == 200
        assert settled.json()["alreadyExists"] is True
        assert settled.json()["status"] == "completed"
        assert slow.copies == 1
    finally:
        slow.stop()


async def test_a_reserved_snapshot_is_settled_at_startup(
    apps, control_client
):
    """A restart must not leave a poller waiting on a copy nobody runs.

    Reserved captures live in an in-process task, so the startup pass resolves
    every ``creating`` record once: resumed (the worker's copy route is
    idempotent) when it can be, failed with the reason when it cannot -- never
    left spinning.
    """
    from control_plane.api.snapshots import reconcile_pending_snapshots

    app, _ = apps
    copy_s = 0.2
    slow = _SlowWorker(copy_s)
    try:
        sandbox = await _create(control_client)
        sid = sandbox["sandboxID"]
        record = app.state.registry.get(sid)
        node = app.state.nodes.get(record.node_id)
        node.address = slow.url

        # Two reserved records as a crashed process would leave them: one that
        # can be resumed, one whose source sandbox was never recorded (the
        # shape a record written by an older/partial run has).
        resumable = app.state.snapshots.reserve_from_sandbox(
            template_id=record.template_id,
            env_vars=record.env_vars,
            metadata=record.metadata,
            volume_mounts=[],
            base_image=record.base_image,
            allow_internet_access=record.allow_internet_access,
            source_sandbox_id=sid,
            node_id=node.node_id,
            name="interrupted-ok",
        )
        assert resumable.status == "creating"
        hopeless = app.state.snapshots.reserve_from_sandbox(
            template_id=record.template_id,
            env_vars=record.env_vars,
            metadata=record.metadata,
            volume_mounts=[],
            base_image=record.base_image,
            allow_internet_access=record.allow_internet_access,
            source_sandbox_id=None,
            node_id=node.node_id,
            name="interrupted-dead",
        )

        resolved = await reconcile_pending_snapshots(app)
        assert resolved == 2
        assert app.state.snapshots.get(resumable.snapshot_id).status == "completed"
        dead = app.state.snapshots.get(hopeless.snapshot_id)
        assert dead.status == "failed"
        assert dead.error == (
            "interrupted by a restart: interrupted by a restart and the source "
            "sandbox was not recorded; make a new snapshot"
        )
    finally:
        slow.stop()


async def test_an_in_flight_unnamed_async_copy_survives_the_reconcile_pass(
    apps, control_client
):
    """N46: a copy that is still running must not read as an orphan.

    The ``creating`` record is the durable half of "somebody is copying this
    id"; the fleet-wide claim is the half that says it *now*. A named request
    takes that claim before the record exists (that is what closes "no record
    yet") and, for the sync shape, keeps it for the whole copy -- so for a
    named id the two together always answer "somebody is on it". An *unnamed*
    async copy used to have only the record: nobody can claim an id nobody has
    chosen yet, so a ``creating`` record with a free id read as exactly one
    thing, "the owner crashed", and the reconcile pass re-drove it. Measured
    shape of the harm: a second copy of the same tree, and the record flipped
    to ``completed`` while the first copy was a fraction of the way through.

    The fix gives the copy its own marker: it holds a refreshed lease for as
    long as it runs, so the pass leaves it alone -- and the lease lapses on its
    own if this replica dies mid-copy (that half is
    ``tests/unit/test_snapshot_copy_lease.py``).
    """
    from control_plane.api.snapshots import reconcile_pending_snapshots

    app, _ = apps
    copy_s = 1.0
    slow = _SlowWorker(copy_s)
    try:
        sandbox = await _create(control_client)
        sid = sandbox["sandboxID"]
        node = app.state.nodes.get(app.state.registry.get(sid).node_id)
        node.address = slow.url

        response = await control_client.post(
            f"/sandboxes/{sid}/snapshots",
            headers={"X-API-Key": "local-key", "Prefer": "respond-async"},
            json={"name": "in-flight"},
        )
        assert response.status_code == 202, response.text
        snapshot_id = response.json()["snapshotID"]
        assert response.json()["status"] == "creating"

        # A peer restarting runs exactly this pass, right now.
        assert await reconcile_pending_snapshots(app) == 0

        during = await control_client.get(
            f"/snapshots/{snapshot_id}", headers={"X-API-Key": "local-key"}
        )
        assert during.status_code == 200
        assert during.json() == {
            "snapshotID": snapshot_id,
            "names": ["in-flight"],
            "status": "creating",
        }

        done = await _poll_snapshot(control_client, snapshot_id)
        assert done["status"] == "completed"
        assert slow.copies == 1, "the reconcile pass started a second copy"
    finally:
        slow.stop()


async def test_an_in_flight_copy_marks_itself_before_the_record_is_written(
    apps, control_client, monkeypatch
):
    """N46: the lease is taken *before* the record exists, not after.

    The window the lease closes is the one between "the record says
    ``creating``" and "somebody can tell it is not an orphan's": a pass that
    reads a record inside that window sees a ``creating`` record nobody claims
    and settles it. Taking the lease first (and handing the same one to the
    copy task) is what makes that window empty, so the order is pinned rather
    than left to whoever reads the function next.
    """
    app, _ = apps
    snapshots = app.state.snapshots
    events: list[tuple[str, str]] = []
    real_claim = snapshots.try_acquire_copy
    real_reserve = snapshots.reserve_from_sandbox

    def claim_spy(snapshot_id, **kwargs):
        if kwargs.get("token") is not None:
            events.append(("lease", snapshot_id))
        return real_claim(snapshot_id, **kwargs)

    def reserve_spy(**kwargs):
        record = real_reserve(**kwargs)
        events.append(("record", record.snapshot_id))
        return record

    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]
    monkeypatch.setattr(snapshots, "try_acquire_copy", claim_spy)
    monkeypatch.setattr(snapshots, "reserve_from_sandbox", reserve_spy)

    response = await control_client.post(
        f"/sandboxes/{sid}/snapshots",
        headers={"X-API-Key": "local-key", "Prefer": "respond-async"},
        json={"name": "ordering"},
    )
    assert response.status_code == 202, response.text
    snapshot_id = response.json()["snapshotID"]
    assert events[:2] == [("lease", snapshot_id), ("record", snapshot_id)]
    done = await _poll_snapshot(control_client, snapshot_id)
    assert done["status"] == "completed"


async def test_an_async_copy_keeps_its_lease_alive_while_it_copies(
    apps, control_client, monkeypatch
):
    """N46: the lease is taken *and* kept, or it is a timer on a live copy.

    A lease that nobody refreshes is not a protection, it is a delayed
    failure: any copy longer than ``COPY_LEASE_TTL_S`` would be settled by the
    next pass while it was still running. So the copy task runs the refresher
    for its own id and token; that this is wired (and with which token) is
    pinned here, and what the refresher does with the store is pinned on a fake
    clock in ``tests/unit/test_snapshot_copy_lease.py``.
    """
    import control_plane.api.snapshots as mod

    app, _ = apps
    refreshes: list[tuple[str, str, float, float]] = []
    real_refresh = mod._refresh_copy_lease

    async def refreshing_spy(snapshots, snapshot_id, token, **kwargs):
        refreshes.append(
            (
                snapshot_id,
                token,
                kwargs.get("ttl_s", mod.COPY_LEASE_TTL_S),
                kwargs.get("refresh_s", mod.COPY_LEASE_REFRESH_S),
            )
        )
        await real_refresh(snapshots, snapshot_id, token, **kwargs)

    monkeypatch.setattr(mod, "_refresh_copy_lease", refreshing_spy)
    slow = _SlowWorker(1.0)
    try:
        sandbox = await _create(control_client)
        sid = sandbox["sandboxID"]
        node = app.state.nodes.get(app.state.registry.get(sid).node_id)
        node.address = slow.url
        response = await control_client.post(
            f"/sandboxes/{sid}/snapshots",
            headers={"X-API-Key": "local-key", "Prefer": "respond-async"},
            json={"name": "kept"},
        )
        assert response.status_code == 202, response.text
        snapshot_id = response.json()["snapshotID"]
        done = await _poll_snapshot(control_client, snapshot_id)
        assert done["status"] == "completed"
    finally:
        slow.stop()

    assert len(refreshes) == 1, "the copy ran without a refresher"
    assert refreshes[0][0] == snapshot_id
    assert refreshes[0][1] != "", "the refresher was given no lease token"
    assert refreshes[0][2:] == (mod.COPY_LEASE_TTL_S, mod.COPY_LEASE_REFRESH_S)


async def test_the_periodic_pass_settles_orphans_and_keeps_off_a_live_copy(
    apps, control_client
):
    """N46: the pass runs on a cadence, and only an orphan is its business.

    Settling at startup only is the wrong *only* place once the copy lease
    exists: a record whose owner died after startup would wait for the next
    restart to be settled -- worse than the hole this closes, where any
    ``creating`` record was fair game. So the pass also runs every
    ``SNAPSHOT_RECONCILE_INTERVAL_S``, single-flighted across replicas like the
    health sweep (that half is pinned in
    ``tests/unit/test_snapshot_copy_lease.py``). Driven here directly: one
    round settles the orphan (nothing holds its id) and the live copy (fresh
    lease, still copying) is left to finish.
    """
    from control_plane.api.snapshots import snapshot_reconcile_loop

    app, _ = apps
    copy_s = 1.0
    slow = _SlowWorker(copy_s)
    rounds = 0
    try:
        sandbox = await _create(control_client)
        sid = sandbox["sandboxID"]
        record = app.state.registry.get(sid)
        node = app.state.nodes.get(record.node_id)
        node.address = slow.url

        response = await control_client.post(
            f"/sandboxes/{sid}/snapshots",
            headers={"X-API-Key": "local-key", "Prefer": "respond-async"},
            json={"name": "live"},
        )
        assert response.status_code == 202, response.text
        live_id = response.json()["snapshotID"]

        orphan = app.state.snapshots.reserve_from_sandbox(
            template_id=record.template_id,
            env_vars=record.env_vars,
            metadata=record.metadata,
            volume_mounts=[],
            base_image=record.base_image,
            allow_internet_access=record.allow_internet_access,
            source_sandbox_id=None,
            node_id=node.node_id,
            name="orphan",
        )

        async def counting_sleep(seconds):
            nonlocal rounds
            rounds += 1
            await asyncio.sleep(0)

        loop = asyncio.create_task(
            snapshot_reconcile_loop(app, interval_s=0.05, sleep=counting_sleep)
        )
        try:
            deadline = time.monotonic() + 10
            while rounds < 3 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        finally:
            loop.cancel()
            try:
                await loop
            except asyncio.CancelledError:
                pass
        assert rounds >= 3, "the pass is not running on a cadence"

        settled = app.state.snapshots.get(orphan.snapshot_id)
        assert settled.status == "failed"
        assert settled.error == (
            "interrupted by a restart: interrupted by a restart and the source "
            "sandbox was not recorded; make a new snapshot"
        )

        during = await control_client.get(
            f"/snapshots/{live_id}", headers={"X-API-Key": "local-key"}
        )
        assert during.json() == {
            "snapshotID": live_id,
            "names": ["live"],
            "status": "creating",
        }
        done = await _poll_snapshot(control_client, live_id)
        assert done["status"] == "completed"
        assert slow.copies == 1, "the periodic pass started a second copy"
    finally:
        slow.stop()


async def test_a_named_copy_held_by_another_replica_is_still_refused(
    apps, control_client, monkeypatch
):
    """N46 pins the old half: the named path's refusal is untouched.

    A named id is claimed fleet-wide before the copy starts, and the loser of
    that race neither copies nor invents a status: it waits briefly for the
    winner's record (``alreadyExists``) and otherwise answers 409 with the same
    sentence it always has. The copy lease is added *under* this shape, not in
    place of it.
    """
    from control_plane.registry.snapshots import UnknownSnapshotError

    app, _ = apps
    sandbox = await _create(control_client)
    sid = sandbox["sandboxID"]
    key = "snap_taken_0000000000000000"
    monkeypatch.setattr(
        app.state.snapshots, "try_acquire_copy", lambda *a, **k: False
    )

    response = await control_client.post(
        f"/sandboxes/{sid}/snapshots",
        headers={"X-API-Key": "local-key", "Idempotency-Key": key},
        json={"name": "taken"},
    )
    assert response.status_code == 409
    assert response.json() == {
        "code": 409,
        "message": (
            f"snapshot {key} is being copied by another control-plane replica"
        ),
    }
    with pytest.raises(UnknownSnapshotError):
        app.state.snapshots.get(key)
