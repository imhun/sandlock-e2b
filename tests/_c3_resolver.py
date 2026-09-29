"""C3 Task 2 test helper: expected node endpoints for an in-process lane.

The control plane's internal API derives a node's expected address/IP from a
resolver (never from the request) and refuses a node-scoped claim it cannot
resolve (controller rulings D4/D5). Lanes that drive the internal API through
an in-process ASGI client (peer ``127.0.0.1``) therefore have to hand the
control plane the endpoints it should expect -- production gets them from the
k8s pod API or compose DNS.
"""

from __future__ import annotations

from control_plane.node_address import NodeEndpoint, StaticAddressResolver


def loopback_resolver(*node_ids: str) -> StaticAddressResolver:
    """Map each ``node_id`` to a loopback endpoint (the ASGI client's peer IP)."""
    return StaticAddressResolver(
        {
            node_id: NodeEndpoint("http://127.0.0.1:49983", "127.0.0.1")
            for node_id in node_ids
        }
    )


class AnyNodeLoopbackResolver:
    """Any node id resolves to the in-process client's own address.

    For lanes whose nodes are arbitrary test doubles (the shared ``apps``
    fixture, a heartbeat contract that invents ``node_disk``): every "node" in
    such a lane speaks from ``127.0.0.1``, which is exactly what the control
    plane is asked to verify. Lanes that need *distinct* nodes (the multinode
    harness) use :func:`loopback_resolver` with an explicit table instead.
    """

    def __init__(self, *, ip: str = "127.0.0.1", port: int = 49983) -> None:
        self._ip = ip
        self._port = port

    def resolve(self, node_id: str) -> NodeEndpoint:
        return NodeEndpoint(f"http://{self._ip}:{self._port}", self._ip)
