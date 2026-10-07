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

import logging

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


async def test_the_in_process_nodes_row_keeps_disks_zero_semantics(tmp_path) -> None:
    """Review round 1 / Important 2: the one row that still reads `0` itself.

    The three derived dimensions fall back to their pre-Task-9 numbers when the
    deployment names none (``IN_PROCESS_NODE_DEFAULT_TOTALS``) precisely because
    their ``0`` now means "derive". **Disk's ``0`` never changed meaning** --
    ``docs/env-vars.md`` and ``spec.md`` both still document it as "that
    dimension is not policed" -- so ``E2B_MAX_TOTAL_DISK_MB=0`` must leave the
    in-process node's own disk admission unpoliced, exactly as it did before
    Task 9, and its row must read ``0`` rather than the 10240 default.
    """
    from control_plane.app import create_app

    app = create_app(
        settings=_settings(enable_local_node=True, max_total_disk_mb=0)
    )
    local = app.state.nodes.get("local")
    assert local is not None

    assert local.total_disk_mb == 0
    # ...and 0 really is "not policed" on that row, not "nothing may be placed":
    assert local.blocking_dimension(1024, 100, 10**9, 64) is None

    # The other three dimensions are the *other* decision (their 0 = derive),
    # so this row gets the pre-Task-9 defaults instead.
    assert local.total_memory_mb == 8192
    assert local.total_cpu_percent == 400
    assert local.total_processes == 2048


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


def test_a_non_positive_sum_is_never_adopted_as_a_budget(workspace, caplog) -> None:
    """Task 11, minor 7: the derived number gets the explicit branch's test.

    The explicit branch above only takes a value it can see is positive
    (``configured > 0``); the derived branch used to pass the sum straight into
    the ledger, so a non-positive one -- an embedder's row that reported ``0``,
    or a negative nothing in the registration path rejects -- would reach a
    ladder whose consumers read "not positive" as "this dimension is not
    policed". "Not a budget" and "no budget" are the same answer here, so the
    non-positive sum takes the documented "nothing to derive from" path (see
    ``_fleet_limits``): ``0`` in this dict, said out loud, with the refusal left
    to placement by name and to each sandbox's own per-sandbox ceiling.
    """
    registry = SandboxRegistry(_settings())
    registry.set_fleet_totals_provider(
        _summing_provider(memory=-5, cpu=0, disk=10240, processes=100)
    )

    with caplog.at_level(logging.DEBUG):
        limits = registry._fleet_limits()

    # A budget is a positive number or this ladder's own ``0`` -- never a
    # negative, and never a silent ``0`` that reads like a deliberate policy.
    assert limits == {"memory": 0, "cpu": 0, "processes": 100, "disk": 10240}
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "control_plane.registry.manager"
    ] == [
        "fleet totals: the registered healthy nodes sum to -5 for memory, which "
        "is not a positive total, so E2B_MAX_TOTAL_MEMORY_MB does not police "
        "memory (a non-positive total is not a budget; a create in this state is "
        "still bounded by the node ladder and by each sandbox's own ceiling)",
        "fleet totals: the registered healthy nodes sum to 0 for cpu, which is "
        "not a positive total, so E2B_MAX_TOTAL_CPU_PERCENT does not police cpu "
        "(a non-positive total is not a budget; a create in this state is still "
        "bounded by the node ladder and by each sandbox's own ceiling)",
    ]

    # The create is admitted (this ladder polices nothing on those two dims) and
    # the nonsense sum was booked nowhere: the reservation is the standard one.
    _create(registry)
    assert registry.global_reserved()["memory"] == 512
    assert registry.global_reserved()["processes"] == 64


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
