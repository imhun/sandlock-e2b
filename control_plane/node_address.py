"""Resolve a node's expected address (and source IP) from a trusted source.

C3 Task 2 / N49: the control plane used to take a node's ``address`` from the
registration body -- a self-declared value -- and had no notion of "the network
position this node must speak from". Both are now derived here, and the address
is *never* learned from the request it is validating: an attacker holding a key
could otherwise re-pin a node to their own address/IP and the source-IP second
factor would defend nothing (``docs/c3-privilege-relocation.md`` §11.1 item 9,
consumer ruling D4).

Two explicit modes, because C3's coverage is k8s **and** the separated compose
stacks:

* ``k8s`` -- ``node_id`` *is* the StatefulSet pod name; the pod's ``status.podIP``
  is read from the API with the mounted ServiceAccount (no extra dependency;
  same shape ``autoscaler/backends/k8s.py`` uses). Pod names are stable across
  restarts while IPs are not, so the expected value follows the pod -- exactly
  what §11.1 item 9's premise (b) requires (never hard-pin a worker's IP).
* ``hostname`` -- the compose stacks address their workers by service name
  (``worker-1``), which resolves on the compose network; the resolved IP is the
  expected source IP and the dial-back address keeps the name.

``auto`` (the default) picks ``k8s`` when a ServiceAccount is mounted and
``hostname`` otherwise, so one image is honest in both shapes; an explicit
``E2B_NODE_ADDRESS_MODE`` always wins.

Every mode returns ``None`` when it cannot determine the endpoint -- never a
guess, never the observed peer IP. The *use* of ``None`` (fail closed, named) is
the caller's job; see :func:`control_plane.api.internal._require_node_identity`.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

import httpx

from gateway_common.paths import validate_node_id

logger = logging.getLogger(__name__)

#: Where a pod-mounted ServiceAccount lives. Its presence is what ``auto`` reads.
_SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

#: The worker/agent port every deployment shape uses (``E2B_NODE_ADDRESS`` in
#: the manifests is ``http://<ip>:49983``). Overridable for a host that moved
#: the worker's ``E2B_ENVD_PORT``.
DEFAULT_NODE_PORT = 49983


@dataclass(frozen=True)
class NodeEndpoint:
    """A node's dial-back address (URL) and the IP it must speak from.

    The two travel together because they come from one trusted lookup: the
    address is what the control plane dials, the IP is the second factor's
    expected value. Keeping them in one value makes "the resolver answered" the
    single precondition for both.

    ``ips`` is the *whole* set a name resolved to, when that is more than one
    address (a compose service name can answer AAAA as well as A). A station
    whose connection arrives over IPv4 must not be refused because the first
    ``getaddrinfo`` entry happened to be IPv6. ``ip`` stays the primary/display
    value; :attr:`source_ips` is what the check uses.
    """

    address: str
    ip: str
    ips: tuple[str, ...] = ()

    @property
    def source_ips(self) -> tuple[str, ...]:
        """Every address the node's name is allowed to speak from."""
        return self.ips or (self.ip,)


class NodeAddressResolver(Protocol):
    def resolve(self, node_id: str) -> NodeEndpoint | None:
        """The node's endpoint, or ``None`` when it cannot be determined."""
        ...


class StaticAddressResolver:
    """A fixed node→endpoint table (tests, embedders, a hand-run lane).

    This is the injection seam controller ruling D4 asks for: the contract lane
    hands the control plane the endpoints it expects instead of standing up a
    pod API or a resolvable compose network.
    """

    def __init__(self, endpoints: Mapping[str, NodeEndpoint]) -> None:
        self._endpoints = dict(endpoints)

    def resolve(self, node_id: str) -> NodeEndpoint | None:
        return self._endpoints.get(node_id)


class HostnameAddressResolver:
    """Compose mode: the node id is a name on the deployment's network."""

    def __init__(self, *, port: int = DEFAULT_NODE_PORT, scheme: str = "http") -> None:
        self._port = int(port)
        self._scheme = scheme

    def resolve(self, node_id: str) -> NodeEndpoint | None:
        if not validate_node_id(node_id):
            return None
        try:
            infos = socket.getaddrinfo(node_id, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return None
        ips = tuple(dict.fromkeys(info[4][0] for info in infos if info[4][0]))
        if not ips:
            return None
        # Keep the *name* in the dial-back address: it tracks a restarted
        # container whose IP moved, which is premise (b). Every resolved
        # address is acceptable as a source (see ``NodeEndpoint.source_ips``).
        return NodeEndpoint(
            f"{self._scheme}://{node_id}:{self._port}", ips[0], ips
        )


class K8sPodAddressResolver:
    """k8s mode: ``node_id`` is the StatefulSet pod name; read its pod IP."""

    def __init__(
        self,
        *,
        namespace: str,
        port: int = DEFAULT_NODE_PORT,
        scheme: str = "http",
        client: httpx.Client | None = None,
    ) -> None:
        self._namespace = namespace
        self._port = int(port)
        self._scheme = scheme
        self._client = client or self._in_cluster_client()

    @staticmethod
    def _in_cluster_client() -> httpx.Client:
        host = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
        token_path = _SERVICE_ACCOUNT_DIR / "token"
        token = token_path.read_text(encoding="utf-8").strip() if token_path.is_file() else None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        ca_path = _SERVICE_ACCOUNT_DIR / "ca.crt"
        return httpx.Client(
            base_url=f"https://{host}:{port}",
            headers=headers,
            timeout=5.0,
            verify=str(ca_path) if ca_path.is_file() else True,
        )

    def resolve(self, node_id: str) -> NodeEndpoint | None:
        if not validate_node_id(node_id):
            return None
        try:
            resp = self._client.get(
                f"/api/v1/namespaces/{self._namespace}/pods/{node_id}"
            )
        except httpx.HTTPError:
            # Unreachable API is "cannot determine", not "allow": the caller
            # fails closed and names the node.
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            # A body that is not JSON is "cannot determine", like any other
            # failure on this path -- never a reason to fall back to the request.
            return None
        # ``status`` is a dict on a real Pod; any other shape (a "Status" error
        # object, a truncated body) is "no address", never an ``AttributeError``
        # that would surface as a 500 instead of the named fail-closed refusal.
        status = (payload or {}).get("status") if isinstance(payload, dict) else None
        pod_ip = status.get("podIP") if isinstance(status, dict) else None
        if not pod_ip:
            return None
        return NodeEndpoint(f"{self._scheme}://{pod_ip}:{self._port}", pod_ip)


def _service_account_present() -> bool:
    """True when this process runs under a mounted ServiceAccount (in-cluster)."""
    return (_SERVICE_ACCOUNT_DIR / "token").is_file()


def build_node_address_resolver(settings) -> NodeAddressResolver:
    """Build the resolver named by ``settings.node_address_mode``.

    ``auto`` is in-cluster detection between the two explicit modes; an explicit
    ``k8s``/``hostname`` always wins. Anything else is a configuration error and
    refused at startup rather than silently falling back to a mode.
    """
    mode = (getattr(settings, "node_address_mode", "auto") or "auto").strip().lower()
    port = int(getattr(settings, "node_address_port", DEFAULT_NODE_PORT))
    namespace = getattr(settings, "node_address_namespace", "sandlock")
    if mode == "auto":
        mode = "k8s" if _service_account_present() else "hostname"
    if mode == "k8s":
        return K8sPodAddressResolver(namespace=namespace, port=port)
    if mode == "hostname":
        return HostnameAddressResolver(port=port)
    raise ValueError(
        f"E2B_NODE_ADDRESS_MODE must be 'k8s', 'hostname' or 'auto' (got {mode!r})"
    )
