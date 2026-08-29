"""Multi-node internal API contract tests (registration, routing, agent)."""

from __future__ import annotations


async def test_worker_registered_and_healthy(multinode_servers):
    nodes = multinode_servers["nodes"].list()
    assert len(nodes) == 1
    worker = nodes[0]
    assert worker.address == multinode_servers["worker_url"]
    assert worker.status == "healthy"
    assert worker.labels.get("node-type") in ("container", "physical")


async def test_route_lookup(multinode_servers, control_client):
    import httpx

    # control_client is the single-node ASGI client; build one for the real
    # control plane instead.
    async with httpx.AsyncClient(
        base_url=multinode_servers["api_url"]
    ) as client:
        created = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert created.status_code == 201
        sandbox_id = created.json()["sandboxID"]

        route = await client.get(
            f"/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
        assert route.status_code == 200
        assert route.json()["address"] == multinode_servers["worker_url"]

        await client.delete(
            f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
        )


async def test_worker_agent_api(multinode_servers):
    import httpx

    headers = {"X-Internal-Key": "internal-key"}
    async with httpx.AsyncClient(
        base_url=multinode_servers["worker_url"]
    ) as client:
        health = await client.get("/agent/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["totalCPUPercent"] > 0

        created = await client.post(
            "/agent/sandboxes",
            headers=headers,
            json={
                "sandboxID": "sbx_agent_test",
                "accessToken": "tok",
                "envVars": {"A": "1"},
                "baseImage": None,
                "memoryMB": 512,
                "cpuPercent": 100,
                "diskMB": 1024,
                "maxProcesses": 64,
                "allowInternetAccess": False,
                "maxCommandTimeout": 3600,
            },
        )
        assert created.status_code == 201

        deleted = await client.delete("/agent/sandboxes/sbx_agent_test", headers=headers)
        assert deleted.status_code == 204

        bad_key = await client.get("/agent/health", headers={"X-Internal-Key": "nope"})
        assert bad_key.status_code == 401


async def test_unhealthy_node_route_returns_502(multinode_servers):
    import httpx
    import time

    nodes = multinode_servers["nodes"]
    worker = nodes.list()[0]

    async with httpx.AsyncClient(
        base_url=multinode_servers["api_url"]
    ) as client:
        created = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )
        assert created.status_code == 201
        sandbox_id = created.json()["sandboxID"]
        try:
            worker.heartbeat_at = time.time() - 60
            route = await client.get(
                f"/internal/routes/{sandbox_id}",
                headers={"X-Internal-Key": "internal-key"},
            )
            assert route.status_code == 502
        finally:
            worker.heartbeat_at = time.time()
            await client.delete(
                f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
            )


async def test_node_management_endpoints(multinode_servers):
    import httpx

    nodes = multinode_servers["nodes"]
    original = nodes.list()[0]
    async with httpx.AsyncClient(
        base_url=multinode_servers["api_url"]
    ) as client:
        listed = await client.get("/nodes", headers={"X-API-Key": "local-key"})
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        node_id = listed.json()[0]["nodeID"]

        try:
            removed = await client.delete(
                f"/nodes/{node_id}", headers={"X-API-Key": "local-key"}
            )
            assert removed.status_code == 204
            gone = await client.delete(
                f"/nodes/{node_id}", headers={"X-API-Key": "local-key"}
            )
            assert gone.status_code == 404
        finally:
            # Restore the worker so later tests in the shared session can
            # schedule sandboxes again.
            nodes.register(
                node_id=original.node_id,
                address=original.address,
                total_memory_mb=original.total_memory_mb,
                total_cpu_percent=original.total_cpu_percent,
                total_disk_mb=original.total_disk_mb,
                total_processes=original.total_processes,
                images=original.images,
                labels=original.labels,
            )
