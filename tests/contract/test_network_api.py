"""Network configuration API: create echo, atomic update, explicit rejects."""

from __future__ import annotations

import json

import httpx
import pytest

from e2b import Sandbox


def _linux_sandlock_ready() -> bool:
    import os
    import sys

    if sys.platform != "linux":
        return False
    try:
        import sandlock

        return sandlock.landlock_abi_version() >= 6
    except Exception:
        return False


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


async def test_network_prelaunch_create_echo_and_atomic_update(
    multinode_two_workers,
):
    """D4=A (a): before any command launches the instance, updates atomically
    replace the record and stay 204 (they become the future static policy)."""
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


@pytest.mark.skipif(
    not _linux_sandlock_ready(),
    reason="D4=A instance-ceiling 409 semantics need a launched sandlock "
    "instance (run inside the Docker test runner)",
)
async def test_network_postlaunch_model_flip_409_keeps_record_byte_identical(
    multinode_two_workers,
):
    """D4=A (b): once a command has launched the instance, flipping the
    egress model (allowOut -> denyOut) is an HTTP 409 that is raised before
    the control-plane record is persisted, leaving ``detail["network"]``
    byte-identical."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(
        network={"allow_out": ["8.8.8.8"]}, **_opts(harness)
    )
    try:
        launched = sandbox.commands.run("/bin/echo launch-ok")
        assert launched.exit_code == 0

        before = (await _detail(harness, sandbox.sandbox_id))["network"]
        resp = await _put_network(
            harness,
            sandbox.sandbox_id,
            {"denyOut": ["10.0.0.0/8"]},
        )
        assert resp.status_code == 409
        after = (await _detail(harness, sandbox.sandbox_id))["network"]
        assert after == before
        assert json.dumps(
            after, sort_keys=True, separators=(",", ":")
        ) == json.dumps(before, sort_keys=True, separators=(",", ":"))
    finally:
        sandbox.kill()


async def test_network_update_unknown_sandbox_404(multinode_two_workers):
    harness = multinode_two_workers
    resp = await _put_network(harness, "sbx_missing_net", {})
    assert resp.status_code == 404


async def test_network_rejects_unsupported_parts(multinode_two_workers):
    harness = multinode_two_workers
    for network in [
        {"denyOut": ["example.com"]},
        {"maskRequestHost": "bad host"},
        {"rules": {"api.example.com": [{"transform": {"body": {}}}]}},
    ]:
        resp = await _create_with_network(harness, network)
        assert resp.status_code == 400, network

    # Block B: maskRequestHost + rules[].transform.headers are accepted and
    # echoed (requires the fork wheel in the worker).
    created = await _create_with_network(
        harness,
        {
            "maskRequestHost": "localhost:${PORT}",
            "rules": {
                "api.example.com": [
                    {"transform": {"headers": {"X-API-Key": "sk-test"}}}
                ]
            },
        },
    )
    assert created.status_code == 201
    try:
        detail = await _detail(harness, created.json()["sandboxID"])
        assert detail["network"]["maskRequestHost"] == "localhost:${PORT}"
        assert detail["network"]["rules"]["api.example.com"] == [
            {"transform": {"headers": {"X-API-Key": "sk-test"}}}
        ]
    finally:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            await client.delete(
                f"/sandboxes/{created.json()['sandboxID']}",
                headers={"X-API-Key": "local-key"},
            )

    # Wildcard allowOut is accepted without an egress proxy (per-sandbox DNS
    # gateway) and echoed back.
    created = await _create_with_network(harness, {"allowOut": ["*.example.com"]})
    assert created.status_code == 201
    try:
        detail = await _detail(harness, created.json()["sandboxID"])
        assert detail["network"]["allowOut"] == ["*.example.com"]
    finally:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            await client.delete(
                f"/sandboxes/{created.json()['sandboxID']}",
                headers={"X-API-Key": "local-key"},
            )

    # A public egress proxy is now accepted and echoed.
    created = await _create_with_network(
        harness, {"egressProxy": {"address": "1.1.1.1:1080"}}
    )
    assert created.status_code == 201
    try:
        detail = await _detail(harness, created.json()["sandboxID"])
        assert detail["network"]["egressProxy"] == {"address": "1.1.1.1:1080"}
    finally:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            await client.delete(
                f"/sandboxes/{created.json()['sandboxID']}",
                headers={"X-API-Key": "local-key"},
            )

    sandbox = Sandbox.create(**_opts(harness))
    try:
        rejected = await _put_network(
            harness, sandbox.sandbox_id, {"egressProxy": None}
        )
        # Explicit null egressProxy clears the proxy.
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


async def test_network_allow_public_traffic_does_not_skip_token(
    multinode_two_workers,
):
    """SEC-K0S-005: ``allowPublicTraffic`` must NOT waive the envd access token.

    envd is the sandbox *control* surface (files / process / PTY), so an
    unauthenticated request there is remote code execution, not "public
    traffic" -- measured on the live cluster as a cross-sandbox
    ``POST /process.Process/Start`` with no token. Both a public-traffic and a
    default sandbox must answer 401 without the token and 200 with it.
    """
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
        for sandbox in (public, private):
            address = await _route_address(sandbox["sandboxID"])
            token = (await _detail(harness, sandbox["sandboxID"]))["envdAccessToken"]
            assert token
            async with httpx.AsyncClient() as client:
                without = await client.get(
                    f"{address}/envs",
                    headers={"E2b-Sandbox-Id": sandbox["sandboxID"]},
                )
                wrong = await client.get(
                    f"{address}/envs",
                    headers={
                        "E2b-Sandbox-Id": sandbox["sandboxID"],
                        "X-Access-Token": "not-the-token",
                    },
                )
                right = await client.get(
                    f"{address}/envs",
                    headers={
                        "E2b-Sandbox-Id": sandbox["sandboxID"],
                        "X-Access-Token": token,
                    },
                )
            assert without.status_code == 401
            assert wrong.status_code == 401
            assert right.status_code == 200
    finally:
        async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
            for sandbox_id in (public["sandboxID"], private["sandboxID"]):
                await client.delete(
                    f"/sandboxes/{sandbox_id}",
                    headers={"X-API-Key": "local-key"},
                )
