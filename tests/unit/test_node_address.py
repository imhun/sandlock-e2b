"""C3 Task 2 (controller ruling D4): the node address resolver.

The control plane must never learn a node's address from the request it is
deciding about (N49: ``body.get("address")`` was accepted verbatim). The
expected address comes from a resolver instead, and C3 has to cover the
separated compose stacks as well as k8s, where there is no pod API at all --
hence two explicit modes, k8s and hostname, selected by ``mode`` and injectable
for the contract lane.

What is pinned here is only the resolver itself: that each mode turns a node id
into an address *and* the expected source IP (the second factor needs the IP,
the dial-back needs the URL), that "cannot determine" is ``None`` and never a
guess, and that the mode selection is explicit. The fail-closed *use* of
``None`` is pinned by ``tests/contract/test_internal_identity.py``.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from control_plane.config import Settings
from control_plane.node_address import (
    HostnameAddressResolver,
    K8sPodAddressResolver,
    NodeEndpoint,
    StaticAddressResolver,
    build_node_address_resolver,
)
from control_plane.node_address import _service_account_present


# ------------------------------------------------------------ static (injection)


def test_static_resolver_is_the_injection_seam() -> None:
    """Tests and embedders hand the control plane its expected endpoints."""
    resolver = StaticAddressResolver(
        {
            "node_a": NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1"),
            "node_b": NodeEndpoint("http://10.0.0.2:49983", "10.0.0.2"),
        }
    )
    assert resolver.resolve("node_a") == NodeEndpoint(
        "http://10.0.0.1:49983", "10.0.0.1"
    )
    assert resolver.resolve("node_a").ip != resolver.resolve("node_b").ip
    assert resolver.resolve("unknown") is None


# ------------------------------------------------------------------ hostname mode


def test_hostname_resolver_reads_the_ip_and_keeps_the_dial_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose: ``worker-1`` is a name on the network, its IP is the factor."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "worker-1"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("172.18.0.5", 0),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolver = HostnameAddressResolver(port=49983)
    endpoint = resolver.resolve("worker-1")
    assert endpoint.address == "http://worker-1:49983"
    assert endpoint.ip == "172.18.0.5"
    assert endpoint.source_ips == ("172.18.0.5",)


def test_hostname_resolver_keeps_every_resolved_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name that answers AAAA + A is accepted from *either* address.

    Taking only the first ``getaddrinfo`` entry would refuse a worker whose
    connection arrives over IPv4 just because the resolver listed IPv6 first.
    """

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2001:db8::1", 0, 0, 0),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("172.18.0.5", 0),
            ),
            # A duplicate entry must not repeat in the accepted set.
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("172.18.0.5", 0),
            ),
        ],
    )
    endpoint = HostnameAddressResolver(port=49983).resolve("worker-1")
    assert endpoint.ip == "2001:db8::1"
    assert endpoint.source_ips == ("2001:db8::1", "172.18.0.5")


def test_a_malformed_node_id_never_reaches_the_api_or_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape check before interpolation (path segment / DNS name).

    Both resolvers must answer "no address" for an id that cannot be a pod or
    service name, without calling the k8s API and without handing the id to
    ``getaddrinfo``.
    """
    called: list[tuple] = []

    def fake_getaddrinfo(*args, **kwargs):
        called.append(args)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 0),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    hostname = HostnameAddressResolver(port=49983)
    for bad in ("../etc", "a/b", "", "-leading", "has space", "x" * 129, "a\nb"):
        assert hostname.resolve(bad) is None
    assert called == []

    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the k8s API must not be called for a bad id")

    k8s = K8sPodAddressResolver(
        namespace="sandlock",
        port=49983,
        client=httpx.Client(transport=httpx.MockTransport(explode)),
    )
    assert k8s.resolve("../etc") is None


def test_hostname_resolver_fails_to_none_never_to_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable node is ``None``: not the literal name, not loopback."""

    def raise_gaierror(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)
    assert HostnameAddressResolver(port=49983).resolve("node_abc") is None

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    assert HostnameAddressResolver(port=49983).resolve("node_abc") is None


# ----------------------------------------------------------------------- k8s mode


def _pod_client(pod_name: str, pod_ip: str | None, *, status: int = 200) -> httpx.Client:
    """A k8s API stand-in that answers one ``pods/<name>`` GET."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/namespaces/sandlock/pods/{pod_name}"
        assert request.headers.get("authorization") == "Bearer sa-token"
        if status != 200:
            return httpx.Response(status, json={"kind": "Status"})
        return httpx.Response(
            200,
            json={
                "kind": "Pod",
                "metadata": {"name": pod_name},
                "status": {"podIP": pod_ip},
            },
        )

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://kubernetes.default.svc:443",
        headers={"Authorization": "Bearer sa-token"},
    )


def test_k8s_resolver_queries_the_pod_by_node_id() -> None:
    """``node_id`` *is* the StatefulSet pod name; the pod IP is authoritative."""
    resolver = K8sPodAddressResolver(
        namespace="sandlock",
        port=49983,
        client=_pod_client("e2b-worker-0", "10.42.0.7"),
    )
    assert resolver.resolve("e2b-worker-0") == NodeEndpoint(
        "http://10.42.0.7:49983", "10.42.0.7"
    )


def test_k8s_resolver_missing_pod_or_ip_is_none() -> None:
    """No pod, and a pod that has not been assigned an IP yet, are both None."""
    missing = K8sPodAddressResolver(
        namespace="sandlock", port=49983, client=_pod_client("ghost", None, status=404)
    )
    assert missing.resolve("ghost") is None
    pending = K8sPodAddressResolver(
        namespace="sandlock", port=49983, client=_pod_client("pending", None)
    )
    assert pending.resolve("pending") is None


def _raw_json_client(body) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body)
        ),
        base_url="https://kubernetes.default.svc",
    )


def test_k8s_resolver_200_with_a_non_dict_body_is_no_address() -> None:
    """A 200 is not enough: the body must be the Pod object this code reads.

    A ``Status`` object, a list, a bare string or a null all mean "no address"
    -- the ``isinstance`` guard exists so none of them raises out of the
    resolver (which would surface as a 500 instead of the named 503).
    """
    for body in (
        [],
        ["pods"],
        "boom",
        7,
        None,
        {"status": "Pending"},  # a status that is not the pod's object
        {"status": []},
    ):
        resolver = K8sPodAddressResolver(
            namespace="sandlock", port=49983, client=_raw_json_client(body)
        )
        assert resolver.resolve("e2b-worker-0") is None, body


# --------------------------------------------------------------- mode selection


def test_mode_hostname_and_mode_k8s_are_explicit() -> None:
    """An explicit ``E2B_NODE_ADDRESS_MODE`` picks the mode, no guessing."""
    assert isinstance(
        build_node_address_resolver(Settings(node_address_mode="hostname")),
        HostnameAddressResolver,
    )
    k8s = build_node_address_resolver(Settings(node_address_mode="k8s"))
    assert isinstance(k8s, K8sPodAddressResolver)


def test_auto_mode_prefers_k8s_only_when_the_service_account_is_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``auto`` (the default) is in-cluster detection between the two modes.

    The k8s deploy mounts a ServiceAccount; the compose stacks do not. Auto
    keeps a single image honest in both without the operator having to set the
    mode, while an explicit value still wins (above).
    """
    monkeypatch.setattr(
        "control_plane.node_address._service_account_present", lambda: True
    )
    assert isinstance(
        build_node_address_resolver(Settings(node_address_mode="auto")),
        K8sPodAddressResolver,
    )
    monkeypatch.setattr(
        "control_plane.node_address._service_account_present", lambda: False
    )
    assert isinstance(
        build_node_address_resolver(Settings(node_address_mode="auto")),
        HostnameAddressResolver,
    )


def test_service_account_probe_reads_the_mounted_token() -> None:
    """The auto probe is a real file check, not an env-shaped guess."""
    assert isinstance(_service_account_present(), bool)
