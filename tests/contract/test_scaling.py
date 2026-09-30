"""Control-plane scaling capabilities: draining, fleet metrics and the
adaptive idempotent create flow (X-Sandbox-Id fast/slow paths)."""

from __future__ import annotations

import httpx
import pytest

from control_plane.autoscaler_service import AutoscalerService, autoscaler_tick_claim
from control_plane.config import Settings as ControlSettings


class _FakeScaleBackend:
    """``ScaleBackend`` without a cluster: the loop's only way out of the process.

    The merged autoscaler needs no kube API in tests, and it needs no HTTP
    either -- it reads the control plane's *own* records -- so the fake is the
    whole outside world for these cases.
    """

    def __init__(self, current: int = 0) -> None:
        self.current_n = current
        self.scaled: list[int] = []
        self.removed: list[str] = []

    def current(self) -> int:
        return self.current_n

    def has_node(self, node_id: str) -> bool:
        return True

    def scale_to(self, replicas: int) -> None:
        self.scaled.append(replicas)
        self.current_n = replicas

    def remove_node(self, node_id: str) -> None:
        self.removed.append(node_id)
        self.current_n -= 1


def _hosted_autoscaler(make_apps, backend, **overrides):
    """A control plane that hosts the autoscaler: the k8s shape, no cluster.

    ``enable_local_node=False`` is part of that shape, not a test convenience:
    the pod that hosts the loop is ``deploy/k8s/control-plane.yaml``, which
    declares no local node (it hosts no sandboxes), so the only nodes the loop
    can see -- and the only ones it may drain -- are the workers that register.
    """
    control, _ = make_apps(
        control_settings=ControlSettings(
            api_keys=("local-key",),
            create_queue_timeout_s=0,
            autoscaler_enabled=True,
            enable_local_node=False,
            **overrides,
        ),
        control_kwargs={"autoscaler_backend": backend},
    )
    return control


async def _register_node(control, node_id: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": "internal-key"},
            json={
                "nodeID": node_id,
                "address": f"http://{node_id}:49983",
                "totalMemoryMB": 2048,
                "totalCPUPercent": 200,
                "totalDiskMB": 4096,
                "totalProcesses": 128,
                "images": [],
                "labels": {"node-type": "container"},
            },
        )
        assert resp.status_code == 200


async def _client(control):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    )


async def _create(control, **headers):
    client = await _client(control)
    async with client:
        return await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", **headers},
            json={"templateID": "base", "timeout": 300, "envVars": {}},
        )


async def test_idempotent_create_fast_path(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": "sbx_idem0001"},
            json={"templateID": "base", "timeout": 300, "envVars": {}},
        )
        assert resp.status_code == 201
        assert resp.json()["sandboxID"] == "sbx_idem0001"

        # Retry with the same ID returns the existing sandbox immediately.
        retry = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key", "X-Sandbox-Id": "sbx_idem0001"},
            json={"templateID": "base", "timeout": 300, "envVars": {}},
        )
        assert retry.status_code == 201
        assert retry.json()["sandboxID"] == "sbx_idem0001"


async def test_invalid_sandbox_id_rejected(apps):
    control, _ = apps
    resp = await _create(control, **{"X-Sandbox-Id": "../../etc/passwd"})
    assert resp.status_code == 400


async def test_cold_image_without_id_returns_428(make_apps):
    control, _ = make_apps(
        control_settings=ControlSettings(
            api_keys=("local-key",),
            base_image="127.0.0.1:1/nope:latest",
            executor="sandlock",
        )
    )
    resp = await _create(control)
    assert resp.status_code == 428
    body = resp.json()
    assert body["code"] == 428
    assert "warm_required" in body["message"]


async def test_cold_image_slow_path_warm_failure_leaves_no_orphan(make_apps):
    control, _ = make_apps(
        control_settings=ControlSettings(
            api_keys=("local-key",),
            base_image="127.0.0.1:1/nope:latest",
            executor="sandlock",
        )
    )
    resp = await _create(control, **{"X-Sandbox-Id": "sbx_idem0002"})
    assert resp.status_code == 503
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        got = await client.get(
            "/sandboxes/sbx_idem0002", headers={"X-API-Key": "local-key"}
        )
        assert got.status_code == 404
        pending = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": "internal-key"}
        )
        assert pending.status_code == 200


async def test_drain_undrain_and_fleet_metrics(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        reg = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": "internal-key"},
            json={
                "nodeID": "worker-x",
                "address": "http://worker-x:49983",
                "totalMemoryMB": 2048,
                "totalCPUPercent": 200,
                "totalDiskMB": 4096,
                "totalProcesses": 128,
                "images": [],
                "labels": {"node-type": "container"},
            },
        )
        assert reg.status_code == 200

        drain = await client.post(
            "/internal/nodes/worker-x/drain",
            headers={"X-Internal-Key": "internal-key"},
        )
        assert drain.status_code == 200
        assert drain.json()["draining"] is True

        metrics = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": "internal-key"}
        )
        assert metrics.status_code == 200
        body = metrics.json()
        by_id = {n["nodeID"]: n for n in body["nodes"]}
        assert by_id["worker-x"]["draining"] is True
        assert by_id["worker-x"]["status"] == "healthy"
        assert body["fleet"]["memory"]["total"] >= 2048
        assert body["remainingSandboxCapacity"] is not None

        undrain = await client.post(
            "/internal/nodes/worker-x/undrain",
            headers={"X-Internal-Key": "internal-key"},
        )
        assert undrain.status_code == 204
        metrics2 = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": "internal-key"}
        )
        by_id2 = {n["nodeID"]: n for n in metrics2.json()["nodes"]}
        assert by_id2["worker-x"]["draining"] is False


async def test_re_register_rebuilds_node_reservations(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        body = {
            "nodeID": "worker-x",
            "address": "http://worker-x:49983",
            "totalMemoryMB": 2048,
            "totalCPUPercent": 200,
            "totalDiskMB": 4096,
            "totalProcesses": 128,
            "labels": {"node-type": "container"},
        }
        reg = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": "internal-key"},
            json=body,
        )
        assert reg.status_code == 200

        # Create sandbox records pinned to worker-x (what survives in Redis
        # when the in-memory node reservations are wiped by a restart).
        settings = control.state.settings
        registry = control.state.registry
        for _ in range(2):
            record = registry.create(
                template_id="base",
                timeout=300,
                metadata={},
                env_vars={},
                secure=True,
                allow_internet_access=False,
                base_image=None,
            )
            record.node_id = "worker-x"
            registry.save(record)

        # Simulate re-registration after a control-plane restart.
        reg2 = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": "internal-key"},
            json=body,
        )
        assert reg2.status_code == 200
        node = control.state.nodes.get("worker-x")
        assert node.reserved_memory_mb == 2 * settings.default_memory_mb
        assert node.reserved_cpu_percent == 2 * settings.default_cpu_percent
        assert node.reserved_disk_mb == 2 * settings.default_disk_mb
        assert node.reserved_processes == 2 * settings.default_max_processes


# --------------------------------------------------------------------------
# The autoscaler, hosted by the control plane (2026-09-30).
#
# It used to be its own Deployment with its own image, talking to this same
# control plane over `GET /internal/fleet/metrics` + `POST /internal/nodes/
# {id}/drain` with the internal key. Since the merge it is a task of this app
# and reads the same two records through the *functions* those handlers call,
# so the loop and the API cannot disagree about the fleet. What has to hold
# instead is what the network used to provide: one replica acts per interval
# (a shared claim) and the loop's marks outlive the process (a shared store).
# --------------------------------------------------------------------------


async def test_the_autoscaler_is_off_unless_the_deployment_asks_for_it(apps):
    control, _ = apps
    assert control.state.autoscaler is None


async def test_the_hosted_loop_grows_the_pool_to_the_warm_pool_floor(make_apps):
    backend = _FakeScaleBackend(current=0)
    control = _hosted_autoscaler(
        make_apps, backend, autoscaler_min_replicas=2, autoscaler_warmup_buffer=0
    )

    service = control.state.autoscaler
    assert await service.tick_once() is True
    assert backend.scaled == [2]


async def test_the_hosted_loop_reads_the_fleet_view_the_endpoint_serves(make_apps):
    """One definition of the fleet view, two readers.

    The standalone autoscaler's numbers came over HTTP and could only ever be
    as fresh as the last request; the merged loop calls the same function the
    endpoint calls, so this is an identity rather than a "close enough".
    """
    backend = _FakeScaleBackend(current=2)
    control = _hosted_autoscaler(make_apps, backend)
    await _register_node(control, "worker-x")
    registry = control.state.registry
    for _ in range(2):
        record = registry.create(
            template_id="base",
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
        )
        record.node_id = "worker-x"
        registry.save(record)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/internal/fleet/metrics", headers={"X-Internal-Key": "internal-key"}
        )
        assert resp.status_code == 200
        via_http = resp.json()

    assert control.state.autoscaler.control.metrics() == via_http


async def test_a_drain_taken_by_the_loop_reaches_the_registry(make_apps):
    """Scale-down is the loop's destructive half: it must go through the
    records, not around them -- the node has to end up `draining` in the node
    registry the scheduler reads, and the pod is only retired once the node
    reports no active sandboxes."""
    backend = _FakeScaleBackend(current=3)
    control = _hosted_autoscaler(
        make_apps, backend, autoscaler_min_replicas=1, autoscaler_scale_down_util=0.4
    )
    for node_id in ("worker-a", "worker-b", "worker-c"):
        await _register_node(control, node_id)

    assert await control.state.autoscaler.tick_once() is True

    assert control.state.nodes.get("worker-a").draining is True
    assert backend.removed == ["worker-a"]


async def test_a_tick_lost_to_a_peer_replica_does_not_act(make_apps):
    backend = _FakeScaleBackend(current=0)
    control = _hosted_autoscaler(make_apps, backend, autoscaler_min_replicas=1)
    service = AutoscalerService(
        loop=control.state.autoscaler.loop, poll_s=5, claim=lambda: False
    )

    assert await service.tick_once() is False
    assert backend.scaled == []


def test_two_control_plane_replicas_share_one_tick_claim():
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    first = fakeredis.FakeRedis(server=server, decode_responses=True)
    second = fakeredis.FakeRedis(server=server, decode_responses=True)

    assert autoscaler_tick_claim(first, poll_s=5) is True
    assert autoscaler_tick_claim(second, poll_s=5) is False


async def test_the_hosted_loop_is_a_task_that_dies_with_the_app(make_apps):
    backend = _FakeScaleBackend(current=1)
    control = _hosted_autoscaler(
        make_apps,
        backend,
        autoscaler_min_replicas=1,
        autoscaler_poll_s=3600,
    )

    async with control.router.lifespan_context(control):
        task = control.state.autoscaler_task
        assert task is not None
        assert task.done() is False

    assert task.done() is True
