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
