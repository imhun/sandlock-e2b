"""N83 phase 2 / Task 9: the fleet's totals are Σ of the nodes, not a config.

The user's ruling (2026-10-07): "max total 是不是没必要了，其实就是 worker 的
上限加一起，可以自动计算". ``E2B_MAX_TOTAL_{MEMORY_MB,CPU_PERCENT,PROCESSES}``
become **optional overrides** -- explicit (>0) wins, which is what preserves
"deliberately sell less"; unset/``0`` derives from the registered, healthy
nodes' own ``total_*``, the same numbers the node ledger admits against, so the
two ledgers cannot drift.

Why that is safe by construction (and not merely convenient): a create has to
fit the *node* it lands on (``NodeRecord.blocking_dimension``, checked on the
same create), so ``Σ reserved ≤ Σ totals`` always holds at the moment any
placement succeeds -- the fleet budget can therefore never be the gate that
refuses first. Today it *is*, on the shipped lanes: the k8s control plane
declares no ``E2B_MAX_TOTAL_CPU_PERCENT`` and the code default is 400 while the
two nodes sell 800, so half the fleet's CPU is unusable.

``E2B_MAX_TOTAL_DISK_MB`` is deliberately **not** derived: the worker reports
the *filesystem's* size (``shutil.disk_usage``), so on a shared volume every
node reports the same number and Σ double-counts (control-plane manifest,
``deploy/k8s/control-plane.yaml`` around :489-498). It stays explicit, and
``E2B_MAX_SANDBOXES`` is a product policy rather than a capacity -- untouched.
"""

from __future__ import annotations

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxRegistry,
)
from control_plane.registry.nodes import NodeRegistry


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        # The three derivable dimensions are unset (`0`) in the shape this file
        # is about; disk stays explicit, as it does in every manifest.
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=10240,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry: SandboxRegistry):
    return registry.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )


def _summing_provider(**totals):
    """A provider of the shape the app wires: ``None`` = no healthy node."""
    return lambda: dict(totals) if totals else None


# ------------------------------------------------- Σ of the healthy nodes


def test_the_fleet_budget_is_the_sum_of_the_nodes(workspace) -> None:
    """An unset fleet total follows the node totals, per dimension."""
    registry = SandboxRegistry(_settings())
    registry.set_fleet_totals_provider(
        _summing_provider(
            memory=1024, cpu=200, disk=2048, processes=128
        )
    )

    _create(registry)  # 512 MB / 100% / 64 tasks
    _create(registry)  # 1024 MB -> the summed memory budget is exactly full
    with pytest.raises(ResourceUnavailableError):
        _create(registry)

    # ...and a release gives the derived capacity back, exactly as it does for
    # a configured one (the fleet ladder is a ledger, not a one-way latch).
    for record in registry.list(limit=None):
        registry.delete(record.sandbox_id)
    _create(registry)
    _create(registry)
    with pytest.raises(ResourceUnavailableError):
        _create(registry)
    assert registry.count() == 2


def test_an_explicit_fleet_total_still_wins_over_the_sum(workspace) -> None:
    """The one remaining reason to set one: deliberately sell less."""
    registry = SandboxRegistry(_settings(max_total_memory_mb=512))
    registry.set_fleet_totals_provider(
        _summing_provider(memory=10240, cpu=800, disk=10240, processes=2048)
    )

    _create(registry)
    with pytest.raises(ResourceUnavailableError):
        _create(registry)


def test_an_explicit_fleet_total_above_the_sum_is_not_clamped(workspace) -> None:
    """The override is authoritative in both directions (the lane's own case:
    the acceptance script raises CPU so two boxes can co-locate)."""
    registry = SandboxRegistry(_settings(max_total_cpu_percent=1600))
    registry.set_fleet_totals_provider(
        _summing_provider(memory=10240, cpu=600, disk=10240, processes=2048)
    )

    for _ in range(4):
        _create(registry)  # 4 x 100% = 400% of the *node* sum (600 is not used)
    assert registry.count() == 4


def test_no_healthy_node_leaves_the_fleet_ladder_out_of_the_way(workspace) -> None:
    """An empty fleet derives nothing, so *this* ladder polices nothing.

    That is not a fail-open: the create is refused one step later, by the layer
    that owns "there is nowhere to put this sandbox" -- placement -- with its
    own named 503. ``test_a_create_with_no_registered_node_is_a_named_503``
    below is the end-to-end half of that statement; this one pins which layer
    stays quiet (and why: a ledger that refuses for a reason it cannot name
    would also break the shapes that drive it without placement at all).
    """
    registry = SandboxRegistry(_settings())
    registry.set_fleet_totals_provider(_summing_provider())

    _create(registry)
    assert registry.count() == 1


async def test_a_create_with_no_registered_node_is_a_named_503(tmp_path) -> None:
    """The end-to-end half: no node registered ⇒ named 503, nothing admitted."""
    import httpx

    from control_plane.app import create_app

    app = create_app(
        settings=_settings(
            enable_local_node=False,
            create_queue_timeout_s=0,
            # Admission-only: never require a base image (`executor=auto` would
            # warm-peek E2B_BASE_IMAGE and 428 on a cold cache).
            executor="local",
            workspace_base=tmp_path / "workspaces",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300},
        )

    assert resp.status_code == 503
    assert resp.json() == {"code": 503, "message": "No resources available"}
    assert app.state.registry.count() == 0


def test_without_a_provider_zero_keeps_todays_meaning(workspace) -> None:
    """A bare ``SandboxRegistry`` (no node ledger wired) is unchanged.

    The derivation needs a node registry to read; an embedder that has none is
    the shape this class had before the task, and ``0`` there still means "this
    dimension is not policed at the fleet layer" exactly as it always did. The
    app wires the provider, so every deployment takes the derived path.
    """
    registry = SandboxRegistry(_settings())

    _create(registry)
    _create(registry)
    _create(registry)
    assert registry.count() == 3


def test_the_disk_budget_is_never_derived(workspace) -> None:
    """Σ of the worker-reported filesystem sizes would double-count a shared
    volume, so disk stays the one explicit fleet number."""
    registry = SandboxRegistry(_settings(max_total_disk_mb=1024))
    registry.set_fleet_totals_provider(
        _summing_provider(memory=10240, cpu=800, disk=999999, processes=2048)
    )

    _create(registry)  # 1024 MiB of disk against the explicit 1024 budget
    with pytest.raises(ResourceUnavailableError):
        _create(registry)


def test_the_tenant_budget_is_a_separate_ledger(workspace) -> None:
    """Task 9 touches the *global* ladder only; a tenant cap still refuses."""
    registry = SandboxRegistry(
        _settings(tenant_limits={"tenant-a": {"max_total_memory_mb": 512}})
    )
    registry.set_fleet_totals_provider(
        _summing_provider(memory=10240, cpu=800, disk=10240, processes=2048)
    )

    _create_with_tenant(registry, "tenant-a")
    with pytest.raises(ResourceUnavailableError) as excinfo:
        _create_with_tenant(registry, "tenant-a")
    assert str(excinfo.value) == "tenant quota exceeded"


def _create_with_tenant(registry: SandboxRegistry, tenant_id: str):
    return registry.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
        tenant_id=tenant_id,
    )


# ------------------------------------------------- the node-side sum itself


def test_healthy_totals_sums_only_the_nodes_that_are_healthy() -> None:
    nodes = NodeRegistry(heartbeat_timeout=15.0)
    nodes.register(
        node_id="worker-1",
        address="http://worker-1:49983",
        total_memory_mb=2048,
        total_cpu_percent=200,
        total_disk_mb=4096,
        total_processes=256,
    )
    nodes.register(
        node_id="worker-2",
        address="http://worker-2:49983",
        total_memory_mb=4096,
        total_cpu_percent=400,
        total_disk_mb=8192,
        total_processes=512,
    )

    assert nodes.healthy_totals() == {
        "memory": 6144,
        "cpu": 600,
        "disk": 12288,
        "processes": 768,
    }

    # A node quiet for longer than the heartbeat window stops contributing:
    # its capacity is not available to place work on, so it must not be
    # *sold* either -- the sum follows the node set, not a cached number.
    stale = nodes.get("worker-2")
    assert stale is not None
    stale.heartbeat_at -= 60.0
    totals = nodes.healthy_totals()
    assert totals is not None
    assert totals == {
        "memory": 2048,
        "cpu": 200,
        "disk": 4096,
        "processes": 256,
    }


def test_healthy_totals_is_none_when_no_node_has_registered() -> None:
    """``None`` is "no capacity is known", never 0 (= unbounded)."""
    nodes = NodeRegistry(heartbeat_timeout=15.0)

    assert nodes.healthy_totals() is None
