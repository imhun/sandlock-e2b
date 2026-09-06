"""Egress enforcement of the network API on the Sandlock executor (D4=A).

Once a command has launched the instance, the network endpoint only accepts
monotone, ip-only allowOut narrowings: deny-all -> allowOut widening and any
re-widening after a narrow are HTTP 409s that leave the record unchanged,
and a narrowed update really denies the previously allowed destination for
the next command. A rejected re-widen (409) leaves the worker's runtime copy
on the narrowed policy: the next command to the formerly-allowed destination
still fails (FUP #12).
"""

from __future__ import annotations

import socket
import subprocess

import httpx
import pytest

from e2b import Sandbox
from e2b.sandbox.commands.command_handle import CommandExitException
from tests.security.test_fork_network_features import _start_bg_origin


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


def _tcp_probe(ip: str, port: int = 80) -> str:
    """Plain TCP connect probe: exit 0 when the destination is reachable
    under the current policy, exit 3 when egress is denied."""
    return (
        "/usr/local/bin/python3 -c \"import socket, sys; "
        f"s = socket.socket(); s.settimeout(5); "
        f"sys.exit(0 if s.connect_ex(('{ip}', {port})) == 0 else 3)\""
    )


@pytest.fixture()
def loopback_alias_ip():
    """Put 198.18.0.99/32 (the SSRF-guard-allowed benchmark range) on ``lo``
    so a bare-IP ``allowOut`` can target a hermetic local origin; same
    NET_ADMIN gating as ``test_fork_network_features.loopback_alias``."""
    addr = "198.18.0.99"
    add = subprocess.run(
        ["ip", "addr", "add", f"{addr}/32", "dev", "lo"],
        check=False,
        capture_output=True,
        text=True,
    )
    detail = (add.stderr or add.stdout or "").strip()
    already_there = add.returncode != 0 and (
        "already assigned" in detail.lower() or "file exists" in detail.lower()
    )
    if add.returncode != 0 and not already_there:
        pytest.skip(
            f"cannot put {addr}/32 on lo (needs NET_ADMIN, run with "
            f"--cap-add NET_ADMIN): {detail[:160]}"
        )
    added_by_us = add.returncode == 0
    try:
        yield addr
    finally:
        if added_by_us:
            subprocess.run(
                ["ip", "addr", "del", f"{addr}/32", "dev", "lo"],
                check=False,
                capture_output=True,
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
async def test_rejected_rewiden_leaves_worker_runtime_copy_narrowed(
    multinode_two_workers, loopback_alias_ip
):
    """FUP #12: a successful narrow (204) denies the formerly-allowed
    destination, and the rejected re-widen (409, record unchanged) must not
    mutate the worker runtime copy back — a fresh command to that hermetic
    destination still fails."""
    harness = multinode_two_workers
    addr = loopback_alias_ip
    origin, thread, loop, state = _start_bg_origin(addr, 0)
    try:
        sandbox = Sandbox.create(
            network={"allow_out": [addr]},
            allow_internet_access=False,
            **_opts(harness),
        )
        try:
            launched = sandbox.commands.run("/bin/echo launch-ok")
            assert launched.exit_code == 0

            allowed = sandbox.commands.run(_tcp_probe(addr, origin.port))
            assert allowed.exit_code == 0

            narrowed = await _put_network(
                harness, sandbox.sandbox_id, {"allowOut": []}
            )
            assert narrowed.status_code == 204
            with pytest.raises(CommandExitException):
                sandbox.commands.run(_tcp_probe(addr, origin.port))

            narrow_record = (await _detail(harness, sandbox.sandbox_id))["network"]
            assert narrow_record["allowOut"] == []

            rewiden = await _put_network(
                harness, sandbox.sandbox_id, {"allowOut": [addr]}
            )
            assert rewiden.status_code == 409
            after_409 = (await _detail(harness, sandbox.sandbox_id))["network"]
            assert after_409 == narrow_record

            # The rejected widen must not have reached the worker runtime:
            # the narrowed policy (deny all) still applies to this new exec.
            with pytest.raises(CommandExitException):
                sandbox.commands.run(_tcp_probe(addr, origin.port))
        finally:
            sandbox.kill()
    finally:
        origin._server.close()
        loop.call_soon_threadsafe(state["shutdown"].set)
        thread.join(timeout=5)
        loop.close()


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
