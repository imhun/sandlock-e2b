"""Task 11, minor 6: the fleet view speaks the ledger's own units.

``fleet_metrics_payload`` reports the "standard sandbox" a stock create gets and
the remaining capacity that follows from it. Both halves used to read
``E2B_DEFAULT_CPU_PERCENT`` raw, while the admission ledger books
``cores_from_percent(...) * 100`` (N84: the record carries ``cpu_count``
*cores*, and the ledger is exactly that times 100, so the record is the single
source of both). For any default that is not a whole number of cores the two
disagreed -- ``E2B_DEFAULT_CPU_PERCENT=250`` books 300% but reported 250% -- and
``remainingSandboxCapacity`` is the number the autoscaler divides a node's
**percent** total by (``autoscaler/policy.py::per_node_capacity``), so the
scaling decision was reading a demand nobody was charged.
"""

from __future__ import annotations

import httpx

from control_plane.config import Settings as ControlSettings
from control_plane.fleet_view import fleet_metrics_payload
from control_plane.registry.nodes import NodeRegistry

API = {"X-API-Key": "local-key"}


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        default_memory_mb=1024,
        # 2.5 cores: `cores_from_percent` rounds a part-core default **up** (to
        # 3 cores, 300%), which is the shape where reading the raw percent is
        # visible. It is also the oversell-safe direction, so the rounding is
        # the record's rule and the view has to follow it.
        default_cpu_percent=250,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=10240,
        max_total_processes=0,
        create_queue_timeout_s=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


def _one_node(cpu_percent: int) -> NodeRegistry:
    nodes = NodeRegistry()
    nodes.add_local_node(
        node_id="local",
        total_memory_mb=8192,
        total_cpu_percent=cpu_percent,
        total_disk_mb=10240,
        total_processes=2048,
        sandbox_cpu_percent_max=cpu_percent,
        sandbox_memory_mb_max=8192,
        sandbox_processes_max=2048,
    )
    return nodes


async def test_a_stock_create_is_viewed_in_the_unit_it_was_booked_in(
    make_apps,
) -> None:
    """One create, then the view of it: 3 cores booked, 300% reported, and the
    remaining capacity computed from 300%."""
    control, _envd = make_apps(
        control_settings=_settings(enable_local_node=False),
        control_kwargs={"nodes_registry": _one_node(cpu_percent=1000)},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/sandboxes",
            headers=API,
            json={"templateID": "base", "timeout": 300},
        )
    assert resp.status_code == 201
    sandbox_id = resp.json()["sandboxID"]

    payload = fleet_metrics_payload(control.state)
    record = control.state.registry.get(sandbox_id)
    # The record is the source: 250% is three whole cores, never two and a half.
    assert record.cpu_count == 3
    assert payload["standardSandboxDims"]["cpu"] == 300
    assert payload["standardSandboxDims"]["cpu"] == record.cpu_count * 100
    # ... and the remaining capacity is that demand against the node's own
    # percent totals: (1000 - 300) // 300 = 2, where the raw 250 would have
    # claimed 4.
    assert payload["remainingSandboxCapacity"] == 2
    reserved = payload["nodes"][0]["utilization"]["cpu"]
    assert reserved["reserved"] == 300
    assert reserved["total"] == 1000


async def test_the_view_and_the_ledger_agree_without_any_create(make_apps) -> None:
    """The same numbers on an idle node: the dims are the ledger's, not a
    second reading of the env."""
    control, _envd = make_apps(
        control_settings=_settings(enable_local_node=False),
        control_kwargs={"nodes_registry": _one_node(cpu_percent=1000)},
    )
    payload = fleet_metrics_payload(control.state)

    assert payload["standardSandboxDims"] == {
        "memory": 1024,
        "cpu": 300,
        "disk": 1024,
        "processes": 64,
    }
    assert payload["remainingSandboxCapacity"] == 3
