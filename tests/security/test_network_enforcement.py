"""Egress enforcement of the network API on the Sandlock executor."""

from __future__ import annotations

import pytest

from e2b import Sandbox
from e2b.sandbox.commands.command_handle import CommandExitException


def _opts(harness):
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


@pytest.mark.usefixtures("require_sandlock")
async def test_network_deny_then_allow_via_update(multinode_two_workers):
    """allow_internet_access=false denies egress; a dynamic update with
    allow_out restores it for the next command."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(allow_internet_access=False, **_opts(harness))
    try:
        probe = (
            "/usr/local/bin/python3 -c "
            "\"import urllib.request; "
            "urllib.request.urlopen('https://example.com', timeout=8)\""
        )
        with pytest.raises(CommandExitException):
            sandbox.commands.run(probe)

        sandbox.update_network({"allow_out": ["example.com:443"]})
        allowed = sandbox.commands.run(probe)
        assert allowed.exit_code == 0
    finally:
        sandbox.kill()


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
        assert "injected-ok" in result.stdout
    finally:
        sandbox.kill()
