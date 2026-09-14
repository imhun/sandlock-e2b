"""The MCP gateway port pool: band placement *and* per-allocation bind check.

``McpPortPool.allocate`` used to hand out ``_MCP_PORT_BASE + n`` unchecked,
from a base of ``51000`` -- inside the kernel's ephemeral range (measured as
``32768-60999`` inside the sandbox containers). A number in that range can be
held by an outgoing connection's *source* port (the suite opens thousands of
them) or by any other listener, and the gateway only discovers the collision
when it binds, deep inside the sandbox -- exactly the shape flake #2 had on the
harness side.

Both halves are pinned here: allocation must never return a port something else
already holds, and the band must sit outside every range this process shares
ports with. The band's own home (above the ephemeral top, because
net_isolation's host listener requires >= 50005) is spelled out in the test
below; the harness-pool half is cross-checked by
``tests/contract/test_server_port_reservation.py``.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from envd_service.runtime.context import (
    _MCP_PORT_BASE,
    _MCP_PORT_MAX,
    McpPortPool,
    _port_bindable,
)
from tests.conftest import _PORT_POOL_MAX


def _hold(port: int) -> socket.socket:
    """A competing holder on ``port`` -- what the kernel/some listener does."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(1)
    return sock


def test_a_port_that_is_already_held_is_not_handed_out() -> None:
    """The first candidate is taken -> the pool moves on instead of returning it.

    Before the fix the pool returned ``_MCP_PORT_BASE + 1`` here (the counter
    was the only input), i.e. the SDK's gateway was started with a port
    something else was already listening on.
    """
    taken = _MCP_PORT_BASE + 1
    held = _hold(taken)
    try:
        assert not _port_bindable(taken)
        pool = McpPortPool()
        assert pool.allocate() == _MCP_PORT_BASE + 2
        assert all(pool.allocate() != taken for _ in range(8))
    finally:
        held.close()


def test_a_released_port_that_was_taken_meanwhile_is_not_reused() -> None:
    """Freed ports are rechecked: the pool only knows what it handed out."""
    pool = McpPortPool()
    first = pool.allocate()
    assert first == _MCP_PORT_BASE + 1
    pool.release(first)

    held = _hold(first)
    try:
        # The recycled candidate is held again, so the pool must advance.
        assert pool.allocate() == _MCP_PORT_BASE + 2
    finally:
        held.close()


def test_the_band_sits_outside_every_shared_range() -> None:
    """``[base+1, max]`` is above the ephemeral top and the harness pool.

    Two constraints squeeze the band: net_isolation's host-side inbound
    listener refuses a host port below 50005 (sandlock's ``net_bind_map``
    validation), and the kernel hands an ephemeral port to an outgoing
    connection -- without asking anyone -- from ``ip_local_port_range``. The
    only window that satisfies both is above the ephemeral *top*, which is
    where the band is. The harness pool assignment is checked by
    ``tests/contract/test_server_port_reservation.py``.
    """
    # net_isolation: the supervisor's host listener for the sandbox's MCP
    # gateway is on this number, and sandlock rejects host ports below 50005.
    assert _MCP_PORT_BASE >= 50005
    assert _MCP_PORT_BASE >= _PORT_POOL_MAX
    assert _MCP_PORT_BASE < _MCP_PORT_MAX
    try:
        _floor, ephemeral_top = (
            Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()
        )
    except (OSError, ValueError):
        # No /proc: not the kernel the workers run on, so its own declaration
        # is the only thing that can be asserted.
        return
    assert _MCP_PORT_BASE > int(ephemeral_top)


def test_an_exhausted_band_fails_instead_of_borrowing_a_port() -> None:
    """No candidate left -> refuse; never return a port from outside the band."""
    width = 2
    pool = McpPortPool(base=_MCP_PORT_BASE, max_port=_MCP_PORT_BASE + width)
    assert [pool.allocate() for _ in range(width)] == [
        _MCP_PORT_BASE + 1,
        _MCP_PORT_BASE + 2,
    ]
    # A port *is* free right above the band -- handing that one out would put
    # the gateway back among the numbers the kernel gives to connections.
    assert _port_bindable(_MCP_PORT_BASE + width + 1)
    with pytest.raises(RuntimeError):
        pool.allocate()
