"""Network configuration API: create echo, atomic update, explicit rejects."""

from __future__ import annotations

import httpx

from e2b import Sandbox


def _opts(harness):
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


async def _detail(harness, sandbox_id) -> dict:
    async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
        resp = await client.get(
            f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
        )
    assert resp.status_code == 200
    return resp.json()


async def _put_network(harness, sandbox_id, body) -> httpx.Response:
    async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
        return await client.put(
            f"/sandboxes/{sandbox_id}/network",
            headers={"X-API-Key": "local-key"},
            json=body,
        )


async def _create_with_network(harness, network) -> httpx.Response:
    async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
        return await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={
                "templateID": "base",
                "timeout": 300,
                "metadata": {},
                "envVars": {},
                "secure": True,
                "allow_internet_access": False,
                "network": network,
            },
        )


async def test_network_create_echo_and_atomic_update(multinode_two_workers):
    harness = multinode_two_workers
    created = await _create_with_network(
        harness,
        {"allowOut": ["8.8.8.8", "example.com"], "allowPublicTraffic": True},
    )
    assert created.status_code == 201
    sandbox_id = created.json()["sandboxID"]
    try:
        detail = await _detail(harness, sandbox_id)
        assert detail["network"] == {
            "allowOut": ["8.8.8.8", "example.com"],
            "allowPublicTraffic": True,
        }

        # Atomic replace: omitted allow_out is cleared, allow_public_traffic
        # (create-only) is kept, allow_internet_access updates the record.
        updated = await _put_network(
            harness,
            sandbox_id,
            {"denyOut": ["10.0.0.0/8"], "allowInternetAccess": True},
        )
        assert updated.status_code == 204
        detail = await _detail(harness, sandbox_id)
        assert detail["network"] == {
            "denyOut": ["10.0.0.0/8"],
            "allowInternetAccess": True,
            "allowPublicTraffic": True,
        }
        assert detail["allowInternetAccess"] is True
    finally:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            await client.delete(
                f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
            )


async def test_network_update_unknown_sandbox_404(multinode_two_workers):
    harness = multinode_two_workers
    resp = await _put_network(harness, "sbx_missing_net", {})
    assert resp.status_code == 404


async def test_network_rejects_unsupported_parts(multinode_two_workers):
    harness = multinode_two_workers
    for network in [
        {"egressProxy": {"address": "proxy.example.com:1080"}},
        {"maskRequestHost": "internal.example.com"},
        {"rules": {"api.example.com": [{"transform": {"headers": {"X-A": "1"}}}]}},
        {"allowOut": ["*.example.com"]},
        {"denyOut": ["example.com"]},
    ]:
        resp = await _create_with_network(harness, network)
        assert resp.status_code == 400, network

    sandbox = Sandbox.create(**_opts(harness))
    try:
        rejected = await _put_network(
            harness, sandbox.sandbox_id, {"egressProxy": None}
        )
        # Explicit null egress_proxy means "no proxy" and is accepted.
        assert rejected.status_code == 204
        rejected = await _put_network(
            harness,
            sandbox.sandbox_id,
            {"egressProxy": {"address": "p:1080"}},
        )
        assert rejected.status_code == 400
    finally:
        sandbox.kill()


async def test_network_sdk_round_trip(multinode_two_workers):
    """The official SDK accepts the network config and update round-trips."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(
        network={"allow_out": ["1.1.1.1"]}, **_opts(harness)
    )
    try:
        detail = await _detail(harness, sandbox.sandbox_id)
        assert detail["network"]["allowOut"] == ["1.1.1.1"]
        sandbox.update_network({"allow_internet_access": False})
        detail = await _detail(harness, sandbox.sandbox_id)
        assert detail["network"] == {
            "allowPublicTraffic": True,  # SDK always sends the default
            "allowInternetAccess": False,
        }
        assert detail["allowInternetAccess"] is False
    finally:
        sandbox.kill()


async def test_network_allow_public_traffic_skips_token(multinode_two_workers):
    """allowPublicTraffic=true lets envd HTTP endpoints serve without the
    access token; the default keeps token auth."""
    harness = multinode_two_workers

    async def _create(network) -> dict:
        created = await _create_with_network(harness, network)
        assert created.status_code == 201
        return created.json()

    async def _route_address(sandbox_id) -> str:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            route = await client.get(
                f"/internal/routes/{sandbox_id}",
                headers={"X-Internal-Key": "internal-key"},
            )
        assert route.status_code == 200
        return route.json()["address"]

    public = await _create({"allowPublicTraffic": True})
    private = await _create({})
    try:
        public_addr = await _route_address(public["sandboxID"])
        private_addr = await _route_address(private["sandboxID"])
        async with httpx.AsyncClient() as client:
            public_resp = await client.get(
                f"{public_addr}/envs",
                headers={"E2b-Sandbox-Id": public["sandboxID"]},
            )
            private_resp = await client.get(
                f"{private_addr}/envs",
                headers={"E2b-Sandbox-Id": private["sandboxID"]},
            )
        assert public_resp.status_code == 200
        assert private_resp.status_code == 401
    finally:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            for sandbox_id in (public["sandboxID"], private["sandboxID"]):
                await client.delete(
                    f"/sandboxes/{sandbox_id}",
                    headers={"X-API-Key": "local-key"},
                )
