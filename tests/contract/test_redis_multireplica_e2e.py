"""Phase 3 e2e: two control-plane replicas share one real Redis server.

The test starts ``redis:7`` through the Docker daemon (skip when Docker is
unavailable), then drives two in-process control-plane replicas against the
shared ledger to verify atomic quotas, cross-replica visibility and TTL
reaping.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time

import httpx
import pytest

redis = pytest.importorskip("redis")

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings
from envd_service.runtime.registry import RuntimeRegistry


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def redis_server():
    port = _free_port()
    client = redis.Redis(host="127.0.0.1", port=port, socket_connect_timeout=2)
    proc = None
    cleanup = None

    if shutil.which("redis-server"):
        # Preferred inside the Linux test runner: a plain redis-server
        # process listens on the container's own localhost.
        proc = subprocess.Popen(
            [
                "redis-server",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--bind",
                "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cleanup = lambda: proc.terminate()  # noqa: E731
    elif shutil.which("docker"):
        start = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "-p",
                f"127.0.0.1:{port}:6379",
                "redis:7",
            ],
            capture_output=True,
            text=True,
        )
        if start.returncode != 0:
            pytest.skip(f"cannot start redis container: {start.stderr.strip()}")
        container_id = start.stdout.strip()
        cleanup = lambda: subprocess.run(  # noqa: E731
            ["docker", "rm", "-f", container_id], capture_output=True
        )
    else:
        pytest.skip("neither redis-server nor docker is available")

    deadline = time.time() + 120
    ready = False
    while time.time() < deadline:
        try:
            if client.ping():
                ready = True
                break
        except redis.RedisError:
            time.sleep(0.5)
    if not ready:
        cleanup()
        pytest.skip("redis did not become ready in time")
    yield client, f"redis://127.0.0.1:{port}/0"
    cleanup()


@pytest.fixture()
def redis_url(redis_server):
    """Isolated database per test so quota state never leaks across cases."""
    client, url = redis_server
    client.flushdb()
    yield url


def _replica(workspace, redis_url: str) -> object:
    """Build one control-plane replica app with a tight shared quota."""
    return create_control_app(
        settings=Settings(
            api_keys=("local-key",),
            redis_url=redis_url,
            max_sandboxes=100,
            default_memory_mb=512,
            default_cpu_percent=100,
            default_disk_mb=1024,
            default_max_processes=64,
            max_total_memory_mb=1024,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
        ),
        runtime_registry=RuntimeRegistry(workspace),
        workspace_base=workspace,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create(client, **overrides) -> httpx.Response:
    body = {
        "templateID": "base",
        "timeout": 300,
        "metadata": {},
        "envVars": {},
        "secure": True,
        "allow_internet_access": False,
    }
    body.update(overrides)
    return await client.post(
        "/sandboxes", headers={"X-API-Key": "local-key"}, json=body
    )


async def test_redis_multireplica_quota_and_visibility(workspace, redis_url):
    replica_a = _replica(workspace / "a", redis_url)
    replica_b = _replica(workspace / "b", redis_url)
    async with _client(replica_a) as ca, _client(replica_b) as cb:
        first = await _create(ca)
        assert first.status_code == 201
        first_id = first.json()["sandboxID"]

        # Replica B sees the record created on replica A, including the
        # persisted node_id (the local node).
        seen = await cb.get(
            f"/sandboxes/{first_id}", headers={"X-API-Key": "local-key"}
        )
        assert seen.status_code == 200
        assert seen.json()["sandboxID"] == first_id
        route = await cb.get(
            f"/internal/routes/{first_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
        assert route.status_code == 200
        assert route.json()["address"] == "local://"

        second = await _create(cb)
        assert second.status_code == 201

        # Shared ledger: 512 + 512 fills the 1024 MB quota on either replica.
        third = await _create(ca)
        assert third.status_code == 503

        # Replica B kills a sandbox created on replica A; A sees it gone.
        killed = await cb.delete(
            f"/sandboxes/{first_id}", headers={"X-API-Key": "local-key"}
        )
        assert killed.status_code == 204
        gone = await ca.get(
            f"/sandboxes/{first_id}", headers={"X-API-Key": "local-key"}
        )
        assert gone.status_code == 404

        await ca.delete(f"/sandboxes/{second.json()['sandboxID']}")


async def test_redis_multireplica_concurrent_create_never_over_commits(
    workspace, redis_url
):
    replica_a = _replica(workspace / "ca", redis_url)
    replica_b = _replica(workspace / "cb", redis_url)
    async with _client(replica_a) as ca, _client(replica_b) as cb:
        results = await asyncio.gather(
            *[_create(ca) for _ in range(2)],
            *[_create(cb) for _ in range(2)],
        )
        codes = sorted(r.status_code for r in results)
        # 1024 MB total / 512 MB per sandbox -> exactly 2 succeed.
        assert codes == [201, 201, 503, 503]
        for response in results:
            if response.status_code == 201:
                sandbox_id = response.json()["sandboxID"]
                await ca.delete(f"/sandboxes/{sandbox_id}")


async def test_connect_resume_persists_state(workspace, redis_url):
    """Regression: connect() on a paused sandbox must persist state=running.
    Redis-backed get() reconstructs from the store on every read, so the
    resume mutation has to be saved before connect() re-reads the record."""
    replica = _replica(workspace / "a", redis_url)
    async with _client(replica) as client:
        sandbox = await _create(client)
        sid = sandbox.json()["sandboxID"]

        paused = await client.post(
            f"/sandboxes/{sid}/pause",
            headers={"X-API-Key": "local-key"},
            json={},
        )
        assert paused.status_code == 204

        connected = await client.post(
            f"/sandboxes/{sid}/connect",
            headers={"X-API-Key": "local-key"},
            json={},
        )
        assert connected.status_code == 200

        info = await client.get(
            f"/sandboxes/{sid}", headers={"X-API-Key": "local-key"}
        )
        assert info.json()["state"] == "running"


async def test_fork_persists_node_id(workspace, redis_url):
    """Regression: a forked sandbox's node assignment must be persisted so
    the gateway can route to it (Redis get() reconstructs from the store)."""
    replica = _replica(workspace / "f", redis_url)
    async with _client(replica) as client:
        sandbox = await _create(client)
        sid = sandbox.json()["sandboxID"]

        snap = await client.post(
            f"/sandboxes/{sid}/snapshots",
            headers={"X-API-Key": "local-key"},
            json={"name": "s"},
        )
        assert snap.status_code == 201

        forked = await client.post(
            f"/sandboxes/{sid}/fork",
            headers={"X-API-Key": "local-key"},
            json={"count": 1},
        )
        assert forked.status_code == 201
        results = forked.json()
        assert results and "sandbox" in results[0], results
        fork_id = results[0]["sandbox"]["sandboxID"]

        record = replica.state.registry.get(fork_id)
        assert record.node_id is not None


async def test_node_quota_restored_after_restart(workspace, redis_url):
    """Regression: reservations survive a control-plane restart in Redis and
    must be restored into the fresh in-memory node record on re-registration
    (otherwise in-memory/Redis drift causes spurious 503s)."""
    replica = _replica(workspace / "q", redis_url)
    replica.state.nodes.register(
        node_id="worker-1",
        address="http://worker-1:49983",
        total_memory_mb=2048,
        total_cpu_percent=200,
        total_disk_mb=4096,
        total_processes=256,
    )
    node = replica.state.nodes.select_and_reserve(
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    assert node is not None and node.node_id == "worker-1"
    assert node.reserved_cpu_percent == 100

    # Simulate a restart: a brand-new replica (same Redis) re-registers the
    # worker; its in-memory record must start from the shared ledger.
    restarted = _replica(workspace / "q2", redis_url)
    node2 = restarted.state.nodes.register(
        node_id="worker-1",
        address="http://worker-1:49983",
        total_memory_mb=2048,
        total_cpu_percent=200,
        total_disk_mb=4096,
        total_processes=256,
    )
    assert node2.reserved_memory_mb == 512
    assert node2.reserved_cpu_percent == 100
    assert node2.reserved_disk_mb == 1024
    assert node2.reserved_processes == 64


async def test_redis_multireplica_ttl_reap_releases_quota(workspace, redis_url):
    replica_a = _replica(workspace / "ta", redis_url)
    replica_b = _replica(workspace / "tb", redis_url)
    async with _client(replica_a) as ca, _client(replica_b) as cb:
        created = await _create(ca, timeout=1)
        assert created.status_code == 201
        sandbox_id = created.json()["sandboxID"]

        time.sleep(1.5)
        expired = replica_b.state.registry.remove_expired()
        assert [r.sandbox_id for r in expired] == [sandbox_id]

        # The quota released by replica B is visible to replica A.
        refill = await _create(ca)
        assert refill.status_code == 201
        await ca.delete(f"/sandboxes/{refill.json()['sandboxID']}")
