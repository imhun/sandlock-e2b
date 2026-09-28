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

The shipped shapes do **not** bind keys to nodes yet, so the fleet key's path
matters as much as the node-bound one (controller ruling D5): an unbound key may
no longer claim any node it likes. Its claim must resolve to an expected
address and the request must arrive from it; a claim that resolves nowhere --
or no claim at all on ``register`` -- is a named refusal, never the request's
word and never ``body["address"]``. The credential layer (step ①) is what a
per-node key adds, and that is the deployment change left for the design's
"近期" hardening.

Callers that genuinely are not nodes (the autoscaler, the gateway, an operator)
are on the *fleet* surfaces, which keep the shared key and are exempt by name
(``_require_fleet_key``, ``docs/open-issues.md`` N49); those are pinned by
``test_internal_tenants``, ``test_multinode`` and ``test_internal_key_rotation``.
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


# --- the fleet key (the shape the manifests actually ship today): no free pass


@pytest.mark.asyncio
async def test_a_fleet_key_with_an_unresolvable_claim_is_refused_named(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """D5: an unbound key may not claim a node that resolves nowhere.

    This is the hole the review found: with the shipped fleet key, a compromised
    worker used to be able to ``POST /internal/nodes/register
    {"nodeID": "e2b-worker-1", "address": "http://attacker:1"}`` and have the
    control plane dial the attacker. Now the claim must resolve to an expected
    address, and a claim that resolves nowhere is a refusal -- the body address
    is never used as a fallback. The degradation is still named once per key.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    settings = _settings(
        internal_api_key="fleet-key-unresolvable-probe", internal_node_keys={}
    )
    app = _app(workspace, settings=settings, nodes=nodes, endpoints={})
    with caplog.at_level(logging.WARNING, logger="control_plane.api.internal"):
        async with _client(app, source_ip="10.9.9.9") as client:
            resp = await _register(
                client,
                key="fleet-key-unresolvable-probe",
                node_id="e2b-worker-1",
                address="http://attacker.example:1",
            )
            assert resp.status_code == 503
            assert resp.json() == {
                "code": 503,
                "message": (
                    "cannot determine the expected address for node e2b-worker-1"
                ),
            }
            heartbeat = await client.post(
                "/internal/nodes/e2b-worker-1/heartbeat",
                headers={"X-Internal-Key": "fleet-key-unresolvable-probe"},
                json={},
            )
            assert heartbeat.status_code == 503
    # The impersonated node was never created, and the impersonation is logged.
    assert nodes.get("e2b-worker-1") is None
    messages = [record.getMessage() for record in caplog.records]
    assert (
        "internal API: cannot determine the expected address for node "
        "e2b-worker-1; refusing (fail closed)"
    ) in messages


@pytest.mark.asyncio
async def test_a_fleet_key_register_without_a_claim_is_refused_named(
    workspace,
) -> None:
    """With no credential-derived node, register must *declare* one to be checked."""
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    settings = _settings(
        internal_api_key="fleet-key-noclaim-probe", internal_node_keys={}
    )
    app = _app(workspace, settings=settings, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": "fleet-key-noclaim-probe"},
            json={"address": "http://anything:1"},
        )
        assert resp.status_code == 403
        assert resp.json() == {
            "code": 403,
            "message": (
                "a node-scoped request with a fleet key must declare the node "
                "it acts for (register: body.nodeID)"
            ),
        }
    # Nothing was registered -- only the in-process ``local`` node exists.
    assert [n.node_id for n in nodes.list()] == ["local"]


@pytest.mark.asyncio
async def test_a_fleet_key_is_accepted_only_from_the_claims_resolved_address(
    workspace,
) -> None:
    """The whole point of D5: the claim is checked against the network position.

    Node B's claim is accepted (and the record's address comes from the
    resolver, not the body) exactly when the request arrives from the address
    the resolver maps node B to; the *identical* request from node A's address
    is refused. This is also what makes the shipped shapes work: the fleet key
    keeps functioning where the resolver is configured -- and only there.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    settings = _settings(
        internal_api_key="fleet-key-position-probe", internal_node_keys={}
    )
    app = _app(workspace, settings=settings, nodes=nodes)
    async with _client(app, source_ip=ENDPOINT_B.ip) as client:
        ok = await _register(
            client,
            key="fleet-key-position-probe",
            node_id="node_b",
            address="http://evil.example:1",
        )
        assert ok.status_code == 200
        assert ok.json() == {"nodeID": "node_b"}
    assert nodes.get("node_b").address == ENDPOINT_B.address

    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        stolen = await client.post(
            "/internal/nodes/node_b/heartbeat",
            headers={"X-Internal-Key": "fleet-key-position-probe"},
            json={},
        )
        assert stolen.status_code == 403
        assert stolen.json() == {
            "code": 403,
            "message": (
                "request for node node_b came from 10.0.0.1, expected 10.0.0.2"
            ),
        }


# ------------------------------------------- a non-ASCII credential is a 401


@pytest.mark.asyncio
async def test_a_non_ascii_internal_key_is_a_401_not_a_500(workspace) -> None:
    """``secrets.compare_digest`` raises on non-ASCII ``str``; a header is bytes.

    The header is decoded as latin-1, so any client can put a non-ASCII value
    there. That is "wrong credential" (401), never a 500 from the comparison.
    """
    app = _app(workspace)
    async with _client(app, source_ip=ENDPOINT_A.ip) as client:
        resp = await client.get(
            "/internal/nodes/node_a/sandboxes",
            # ``str`` header values are encoded as ASCII by the client, so the
            # non-ASCII value travels as raw bytes -- which is exactly what a
            # hostile client can send.
            headers={b"X-Internal-Key": "\u00e9-not-ascii".encode("utf-8")},
        )
        assert resp.status_code == 401
        assert resp.json() == {"code": 401, "message": "Unauthorized"}


# ------------------------- ④ 的多地址：名字解析出 AAAA+A 时任一都算匹配


@pytest.mark.asyncio
async def test_a_node_that_resolves_to_several_addresses_is_accepted_from_any(
    workspace,
) -> None:
    """A name answering both AAAA and A must not lose by ordering.

    ``getaddrinfo`` may list IPv6 first while the worker's connection arrives
    over IPv4; only the *set* is the expected value, not the first entry.
    """
    endpoints = _endpoints()
    endpoints["node_b"] = NodeEndpoint(
        "http://node-b:49983", "2001:db8::1", ("2001:db8::1", "10.0.0.2")
    )
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    app = _app(workspace, nodes=nodes, endpoints=endpoints)
    async with _client(app, source_ip="10.0.0.2") as client:
        ok = await _register(client, key=KEY_B, node_id="node_b", address="")
        assert ok.status_code == 200
        heartbeat = await client.post(
            "/internal/nodes/node_b/heartbeat",
            headers={"X-Internal-Key": KEY_B},
            json={},
        )
        assert heartbeat.status_code == 204

    # A source in neither address is still refused, and the refusal names the
    # whole set so "why did this node start failing?" is one line.
    async with _client(app, source_ip="10.0.0.9") as client:
        refused = await client.post(
            "/internal/nodes/node_b/heartbeat",
            headers={"X-Internal-Key": KEY_B},
            json={},
        )
        assert refused.status_code == 403
        assert refused.json() == {
            "code": 403,
            "message": (
                "request for node node_b came from 10.0.0.9, expected one of "
                "2001:db8::1, 10.0.0.2"
            ),
        }


# ------------------------- 503 的告警节流（拒绝不变，日志不刷屏）


@pytest.mark.asyncio
async def test_an_unresolvable_node_warns_once_but_refuses_every_time(
    workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """A fleet-wide resolver/RBAC outage is one cause for every node and round.

    The refusal (503, named) is per request and unchanged; the WARNING is once
    per node, so a heartbeat every 5 s does not flood the log. A unique node id
    keeps this deterministic across a shared pytest session.
    """
    key = "key-node-zz"
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    settings = _settings(internal_node_keys={key: "node_zz"})
    app = _app(workspace, settings=settings, nodes=nodes, endpoints={})
    with caplog.at_level(logging.WARNING, logger="control_plane.api.internal"):
        async with _client(app, source_ip=ENDPOINT_A.ip) as client:
            for _ in range(3):
                resp = await client.post(
                    "/internal/nodes/node_zz/heartbeat",
                    headers={"X-Internal-Key": key},
                    json={},
                )
                assert resp.status_code == 503
                assert resp.json() == {
                    "code": 503,
                    "message": (
                        "cannot determine the expected address for node node_zz"
                    ),
                }
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "control_plane.api.internal"
    ] == [
        "internal API: cannot determine the expected address for node node_zz; "
        "refusing (fail closed)"
    ]


# ------------------------- 舰队作用域的新枚举端点（D6）


@pytest.mark.asyncio
async def test_the_fleet_sandbox_list_is_named_fleet_scope(workspace) -> None:
    """A worker's ownership sweep asks a *fleet* question, not a node one.

    It authenticates with the shared key and needs no node identity -- including
    when a registered node is unresolvable (which is the state this endpoint
    exists for). It is in ``_require_fleet_key``'s set, named in the module
    docstring; the per-node endpoints stay identity-guarded.
    """
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    registry = SandboxRegistry(_settings())
    _sandbox_on(registry, "node_a", "sbx_a")
    _sandbox_on(registry, "node_b", "sbx_b")
    # A record on a node the resolver does not know (its worker is gone).
    _sandbox_on(registry, "node_ghost", "sbx_ghost")
    app = _app(workspace, registry=registry, nodes=nodes)
    async with _client(app, source_ip="10.9.9.9") as client:
        unauthorized = await client.get("/internal/fleet/sandboxes")
        assert unauthorized.status_code == 401
        assert unauthorized.json() == {"code": 401, "message": "Unauthorized"}

        listed = await client.get(
            "/internal/fleet/sandboxes", headers={"X-Internal-Key": FLEET_KEY}
        )
        assert listed.status_code == 200
        assert sorted(listed.json()["sandboxIDs"]) == [
            "sbx_a",
            "sbx_b",
            "sbx_ghost",
        ]
