"""C3 Task 2 / N49: the control plane's internal API binds identity to the key.

Today ``_require_internal_key`` checks a **fleet-wide** bearer credential, and
the node identity comes from the URL/body -- so a compromised worker can claim
to be another node, and C3 turns the control plane into a privilege amplifier
by acting on that claim. The fix (``docs/c3-privilege-relocation.md`` §11.1
item 9, §14.3) is a three-step validation plus one layer, and the acceptance
criteria are exactly the three below:

① the credential layer: node A's credential may not act for node B;
② the source-IP layer: node B's credential from node A's network position may
   not act -- this is the layer's whole reason for existing, and the only thing
   it defends is "the key was stolen";
③ non-degeneracy: the two nodes' expected source IPs must *differ*, or the
   layer is a constant-true piece of dead code (an in-path proxy collapses every
   worker onto one IP; a pin for that lives in
   ``tests/unit/test_c3_internal_api_shape.py``).

The credential→node mapping is configured (``E2B_INTERNAL_NODE_KEYS``); the
expected address/IP come from a resolver that is **injected here** (k8s queries
the pod API in production, compose resolves the worker's hostname -- see
``control_plane/node_address.py`` and controller ruling D4). A resolver that
cannot determine a node-scoped request's expected address is **fail closed**.

Where a caller genuinely has no node identity (the autoscaler, the gateway, an
operator, or a fleet key that was never bound to a node) the path is named in
``control_plane/api/internal.py``'s module docstring and logged; it is never a
silent exemption. Those fleet surfaces are pinned by ``test_internal_tenants``,
``test_multinode`` and ``test_internal_key_rotation``.
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry

#: The per-node credentials and the endpoints the resolver hands back. Node A
#: and node B are on *different* addresses (① needs distinct credentials, ②/③
#: need distinct network positions).
KEY_A = "key-node-a"
KEY_B = "key-node-b"
ENDPOINT_A = NodeEndpoint("http://10.0.0.1:49983", "10.0.0.1")
ENDPOINT_B = NodeEndpoint("http://10.0.0.2:49983", "10.0.0.2")
FLEET_KEY = "fleet-key"


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        internal_api_key=FLEET_KEY,
        internal_api_keys=(),
        internal_node_keys={KEY_A: "node_a", KEY_B: "node_b"},
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


def _endpoints(**extra) -> dict[str, NodeEndpoint]:
    return {"node_a": ENDPOINT_A, "node_b": ENDPOINT_B, **extra}


def _app(
    workspace,
    *,
    settings: ControlSettings | None = None,
    endpoints: dict[str, NodeEndpoint] | None = None,
    registry: SandboxRegistry | None = None,
    nodes: NodeRegistry | None = None,
):
    """A control plane with the resolver injected (the D4 test seam)."""
    return create_control_app(
        settings=settings or _settings(),
        registry=registry or SandboxRegistry(settings or _settings()),
        nodes_registry=nodes or NodeRegistry(heartbeat_timeout=600.0),
        workspace_base=workspace,
        node_address_resolver=StaticAddressResolver(
            endpoints if endpoints is not None else _endpoints()
        ),
    )


def _client(app, *, source_ip: str):
    """An ASGI client whose ``request.client.host`` is ``source_ip``."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _register(client, *, key: str, node_id: str, address: str):
    return await client.post(
        "/internal/nodes/register",
        headers={"X-Internal-Key": key},
        json={
            "nodeID": node_id,
            "address": address,
            "totalMemoryMB": 1024,
            "totalCPUPercent": 100,
            "totalDiskMB": 1024,
            "totalProcesses": 64,
        },
    )


def _sandbox_on(registry: SandboxRegistry, node_id: str, sandbox_id: str):
    record = registry.create(
        template_id="base",
        sandbox_id=sandbox_id,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    record.node_id = node_id
    registry.save(record)
    return record


# ------------------------------------------------- ① 凭据层：A 的凭据不能替 B 说话


@pytest.mark.asyncio
async def test_node_as_credential_cannot_heartbeat_for_node_b(workspace) -> None:
    """① The URL's ``{node_id}`` must equal the node the credential is bound to.

    Today the URL is never compared with the caller (``internal.py``), so node
    A's key drives node B's heartbeats. It is refused with a 403 that names both
    sides, before the registry is consulted at all.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    app = _app(workspace, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await client.post(
            "/internal/nodes/node_b/heartbeat",
            headers={"X-Internal-Key": KEY_A},
            json={},
        )
        assert resp.status_code == 403
        assert resp.json() == {
            "code": 403,
            "message": (
                "X-Internal-Key is bound to node node_a; "
                "request claims node node_b"
            ),
        }
    # The refusal is identity-only: node B was never touched.
    assert nodes.get("node_b") is None


@pytest.mark.asyncio
async def test_registration_cannot_bind_node_as_credential_to_node_b(
    workspace,
) -> None:
    """① applied to ``register``: the body's ``nodeID`` is a *claim* to check."""
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    app = _app(workspace, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await _register(
            client, key=KEY_A, node_id="node_b", address=ENDPOINT_B.address
        )
        assert resp.status_code == 403
        assert resp.json() == {
            "code": 403,
            "message": (
                "X-Internal-Key is bound to node node_a; "
                "request claims node node_b"
            ),
        }
    assert nodes.get("node_b") is None
    assert nodes.get("node_a") is None


@pytest.mark.asyncio
async def test_node_as_credential_cannot_read_node_bs_sandboxes(workspace) -> None:
    """① on the read path too: ``GET .../sandboxes`` is node-scoped."""
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    registry = SandboxRegistry(_settings())
    _sandbox_on(registry, "node_b", "sbx_b_only")
    app = _app(workspace, registry=registry, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await client.get(
            "/internal/nodes/node_b/sandboxes",
            headers={"X-Internal-Key": KEY_A},
        )
        assert resp.status_code == 403
        assert resp.json() == {
            "code": 403,
            "message": (
                "X-Internal-Key is bound to node node_a; "
                "request claims node node_b"
            ),
        }


# ---------------------------------------- ② 源 IP 层：偷了 B 的凭据也过不去


@pytest.mark.asyncio
async def test_node_bs_key_from_node_as_network_position_is_rejected(
    workspace,
) -> None:
    """② The one thing the IP layer defends: a **stolen** key.

    Node B's own credential, sent from node A's address, is refused; the
    *identical* request from node B's address is accepted. Without the second
    half the assertion would pass on a handler that refuses everything -- hence
    both arms.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    app = _app(workspace, nodes=nodes)
    # Node B registers honestly first, from its own address.
    async with _client(app, source_ip=ENDPOINT_B.ip) as client:
        ok = await _register(
            client,
            key=KEY_B,
            node_id="node_b",
            address="http://192.168.1.1:49983",  # ignored: resolver wins
        )
        assert ok.status_code == 200
        assert ok.json() == {"nodeID": "node_b"}
    assert nodes.get("node_b").address == ENDPOINT_B.address

    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        stolen = await client.post(
            "/internal/nodes/node_b/heartbeat",
            headers={"X-Internal-Key": KEY_B},
            json={},
        )
        assert stolen.status_code == 403
        assert stolen.json() == {
            "code": 403,
            "message": (
                "request for node node_b came from 10.0.0.1, expected 10.0.0.2"
            ),
        }

    async with _client(app, source_ip=ENDPOINT_B.ip) as client:
        honest = await client.post(
            "/internal/nodes/node_b/heartbeat",
            headers={"X-Internal-Key": KEY_B},
            json={},
        )
        assert honest.status_code == 204


# ------------------------------------------------- ③ 源 IP 层不是恒真的死代码


@pytest.mark.asyncio
async def test_the_two_nodes_expected_source_ips_differ(workspace) -> None:
    """③ The layer only exists if the two positions are actually distinct.

    If a proxy/ingress/sidecar sat in front of the internal API every worker
    would present one source IP and this check could never fire. The property is
    asserted on the resolver's two answers *and* end to end: the same request
    (node B, node B's key) is accepted from node B's IP and refused from node
    A's.
    """
    resolver = StaticAddressResolver(_endpoints())
    assert resolver.resolve("node_a").ip == "10.0.0.1"
    assert resolver.resolve("node_b").ip == "10.0.0.2"
    assert resolver.resolve("node_a").ip != resolver.resolve("node_b").ip

    nodes = NodeRegistry(heartbeat_timeout=600.0)
    app = _app(workspace, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_B.ip) as client:
        assert (
            await _register(client, key=KEY_B, node_id="node_b", address="")
        ).status_code == 200
    for source_ip, expected in ((ENDPOINT_B.ip, 204), (ENDPOINT_A.ip, 403)):
        async with _client(app, source_ip=source_ip) as client:
            resp = await client.post(
                "/internal/nodes/node_b/heartbeat",
                headers={"X-Internal-Key": KEY_B},
                json={},
            )
            assert resp.status_code == expected


# ------------------------------------------- ③ 对象用 CP 自己的记录校验


@pytest.mark.asyncio
async def test_object_validation_uses_the_control_planes_own_records(
    workspace,
) -> None:
    """Step 3: the objects acted on come from the CP's records for *your* node.

    A node-bound request that names node A can only ever touch node A's
    records: the reconcile body is the worker's *local* fact, and the records
    the control plane mutates are its own, filtered by the credential-derived
    node id. Node B's record survives a hostile reconcile body verbatim.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    registry = SandboxRegistry(_settings())
    _sandbox_on(registry, "node_a", "sbx_a")
    record_b = _sandbox_on(registry, "node_b", "sbx_b")
    app = _app(workspace, registry=registry, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        assert (
            await _register(client, key=KEY_A, node_id="node_a", address="")
        ).status_code == 200
        assert nodes.get("node_a").heartbeat_at <= time.time()
        resp = await client.post(
            "/internal/nodes/node_a/reconcile",
            headers={"X-Internal-Key": KEY_A},
            json={"sandboxIDs": ["sbx_b"], "snapshotIDs": ["sbx_a", "sbx_b"]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"recovered": [], "removed": ["sbx_a"], "kept": []}
    # Node B's record was never a candidate: only node A's records were scanned.
    assert registry.get("sbx_b") is record_b
    assert registry.get("sbx_b").state == "running"


# ------------------------------------------------- register: address provenance


@pytest.mark.asyncio
async def test_registration_derives_the_address_from_the_resolver(workspace) -> None:
    """The address the control plane dials is never ``body.get("address")``.

    A stolen/rewritten body address would otherwise redirect the control plane's
    dial-back to an attacker (and, with C3, to its privileged agent).
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    app = _app(workspace, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await _register(
            client, key=KEY_A, node_id="node_a", address="http://evil.example:1"
        )
        assert resp.status_code == 200
        assert resp.json() == {"nodeID": "node_a"}
    assert nodes.get("node_a").address == ENDPOINT_A.address


# ------------------------------------------------------- fail closed, by name


@pytest.mark.asyncio
async def test_unresolvable_node_bound_request_is_refused_named(workspace) -> None:
    """A node-bound request whose expected address is unknown is **refused**.

    "The pod API is unreachable" must not degrade into "so allow it": an
    attacker who poisons the resolver's view could then walk through. The
    refusal names the node and says why, so the fleet-wide self-inflicted
    failure this creates is visible instead of looking like a dead node.
    """
    app = _app(workspace, endpoints={})  # nothing resolves
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await _register(
            client, key=KEY_A, node_id="node_a", address=ENDPOINT_A.address
        )
        assert resp.status_code == 503
        assert resp.json() == {
            "code": 503,
            "message": "cannot determine the expected address for node node_a",
        }


@pytest.mark.asyncio
async def test_unresolvable_claim_is_refused_named_for_heartbeats(workspace) -> None:
    """The same fail-closed rule on every node-scoped handler, not just register."""
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    app = _app(workspace, nodes=nodes, endpoints={})
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await client.get(
            "/internal/nodes/node_a/sandboxes", headers={"X-Internal-Key": KEY_A}
        )
        assert resp.status_code == 503
        assert resp.json() == {
            "code": 503,
            "message": "cannot determine the expected address for node node_a",
        }


# ------------------------------------- the named, explicit non-node-scoped path


@pytest.mark.asyncio
async def test_a_fleet_key_is_an_explicit_named_degradation(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """A key bound to no node is *named*, not quietly exempt.

    The fleet key keeps the pre-C3 behavior (the registration body's address is
    used, no identity claim is checked) so local/combined and not-yet-migrated
    lanes keep working -- but each such key logs the degradation once, so
    "the N49 layers are off here" is a fact an operator can grep for rather
    than an invisible bypass.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    settings = _settings(
        internal_api_key="fleet-key-degraded-probe", internal_node_keys={}
    )
    app = _app(workspace, settings=settings, nodes=nodes, endpoints={})
    with caplog.at_level(logging.WARNING, logger="control_plane.api.internal"):
        async with _client(app, source_ip="10.9.9.9") as client:
            resp = await _register(
                client,
                key="fleet-key-degraded-probe",
                node_id="node_x",
                address="http://10.9.9.9:49983",
            )
            assert resp.status_code == 200
    assert nodes.get("node_x").address == "http://10.9.9.9:49983"
    assert any(
        "fleet key with no node binding" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_an_explicit_address_mode_gives_a_fleet_key_the_second_factor(
    workspace,
) -> None:
    """A deployment that names its address mode gets layer ④ even on a fleet key.

    The shipped compose/k8s stacks still share one ``E2B_INTERNAL_API_KEY``, so
    binding keys to nodes is the deployment change that activates step ①. Naming
    the address mode explicitly is a weaker but free step: the claim cannot be
    verified against a credential, but it *can* be verified against the network
    position -- "steal the key and speak from another node's IP" is still
    refused, and the register address still comes from the resolver.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    settings = _settings(
        internal_api_key="fleet-key-mode-probe",
        internal_node_keys={},
        node_address_mode="hostname",
    )
    app = _app(workspace, settings=settings, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_B.ip) as client:
        ok = await _register(
            client,
            key="fleet-key-mode-probe",
            node_id="node_b",
            address="http://evil.example:1",
        )
        assert ok.status_code == 200
    assert nodes.get("node_b").address == ENDPOINT_B.address

    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        stolen = await client.post(
            "/internal/nodes/node_b/heartbeat",
            headers={"X-Internal-Key": "fleet-key-mode-probe"},
            json={},
        )
        assert stolen.status_code == 403
        assert stolen.json() == {
            "code": 403,
            "message": (
                "request for node node_b came from 10.0.0.1, expected 10.0.0.2"
            ),
        }
