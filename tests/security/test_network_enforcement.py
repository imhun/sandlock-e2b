"""Egress enforcement of the network API on the Sandlock executor (D4=A).

Once a command has launched the instance, the network endpoint only accepts
monotone, ip-only allowOut narrowings: deny-all -> allowOut widening and any
re-widening after a narrow are HTTP 409s that leave the record unchanged,
and a narrowed update really denies the previously allowed destination for
the next command.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from e2b import Sandbox
from e2b.sandbox.commands.command_handle import CommandExitException


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


def _target_ip() -> str:
    """Resolve ``example.com`` on the test host so the sandbox ``allowOut``
    carries a literal IP (the D4=A live update binds ip literals only)."""
    infos = socket.getaddrinfo("example.com", 80, type=socket.SOCK_STREAM)
    return infos[0][4][0]


def _tcp_probe(ip: str) -> str:
    """Plain TCP connect probe: exit 0 when the destination is reachable
    under the current policy, exit 3 when egress is denied."""
    return (
        "/usr/local/bin/python3 -c \"import socket, sys; "
        f"s = socket.socket(); s.settimeout(5); "
        f"sys.exit(0 if s.connect_ex(('{ip}', 80)) == 0 else 3)\""
    )


@pytest.mark.usefixtures("require_sandlock")
async def test_network_update_live_instance_is_monotone_narrowing(
    multinode_two_workers,
):
    """Deny-all -> allowOut widening is a 409 with the record unchanged;
    an ip allowOut instance narrows to [] (denying the previously allowed
    destination for the next command) and re-widening is a 409."""
    harness = multinode_two_workers
    target = _target_ip()

    sandbox = Sandbox.create(allow_internet_access=False, **_opts(harness))
    try:
        launched = sandbox.commands.run("/bin/echo launch-ok")
        assert launched.exit_code == 0
        before = (await _detail(harness, sandbox.sandbox_id))["network"]

        widened = await _put_network(
            harness, sandbox.sandbox_id, {"allowOut": ["8.8.8.8"]}
        )
        assert widened.status_code == 409
        after = (await _detail(harness, sandbox.sandbox_id))["network"]
        assert after == before
    finally:
        sandbox.kill()

    narrow = Sandbox.create(
        network={"allow_out": [target]},
        allow_internet_access=False,
        **_opts(harness),
    )
    try:
        allowed = narrow.commands.run(_tcp_probe(target))
        assert allowed.exit_code == 0

        narrowed = await _put_network(harness, narrow.sandbox_id, {"allowOut": []})
        assert narrowed.status_code == 204

        with pytest.raises(CommandExitException):
            narrow.commands.run(_tcp_probe(target))

        detail_narrowed = (await _detail(harness, narrow.sandbox_id))["network"]
        assert detail_narrowed["allowOut"] == []

        rewiden = await _put_network(
            harness, narrow.sandbox_id, {"allowOut": [target]}
        )
        assert rewiden.status_code == 409
        detail_after_409 = (await _detail(harness, narrow.sandbox_id))["network"]
        assert detail_after_409 == detail_narrowed
    finally:
        narrow.kill()


@pytest.mark.usefixtures("require_sandlock")
async def test_header_inject_and_host_mask_accepted_end_to_end(
    multinode_two_workers,
):
    """Block B over the real API+worker: ``rules[].transform.headers`` and
    ``maskRequestHost`` map onto the fork wheel's kwargs and a sandbox still
    runs commands. (The vendored SDK has no ``mask_request_host`` field, so
    the API acceptance/echo of that field is covered by the raw-HTTP contract
    test; the proxy-side injection and Host rewriting are covered hermeticly
    by the fork integration tests.)"""
    harness = multinode_two_workers
    sandbox = Sandbox.create(
        network={
            "allow_out": ["example.com"],
            "rules": {
                "example.com": [
                    {
                        "transform": {
                            "headers": {"X-API-Key": "sk-secret"}
                        }
                    }
                ]
            },
        },
        **_opts(harness),
    )
    try:
        result = sandbox.commands.run(
            "/usr/local/bin/python3 -c \"print('injected-ok')\""
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == "injected-ok"
    finally:
        sandbox.kill()
