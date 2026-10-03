"""Phase 3: Redis-backed shared registries give atomic quotas across replicas."""

from __future__ import annotations

import logging
import time

import pytest

fakeredis = pytest.importorskip("fakeredis")

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
        max_total_memory_mb=1024,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create(registry, **kw):
    kwargs = dict(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    kwargs.update(kw)
    return registry.create(**kwargs)


def test_redis_sandbox_quota_atomic_across_replicas():
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)
    registry_a = SandboxRegistry(_settings(), redis_client=client_a)
    registry_b = SandboxRegistry(_settings(), redis_client=client_b)

    _create(registry_a)  # 512 MB on replica A
    _create(registry_b)  # 1024 MB on replica B
    with pytest.raises(ResourceUnavailableError):
        _create(registry_a)  # 1536 > 1024, shared ledger

    # Replica B sees the record created by replica A.
    first_id = registry_a.list(limit=None)[0].sandbox_id
    assert registry_b.get(first_id).sandbox_id == first_id

    registry_b.delete(first_id)
    _create(registry_a)  # quota released through the shared ledger


def test_redis_node_quota_atomic_across_replicas():
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)
    nodes_a = NodeRegistry(redis_client=client_a)
    nodes_b = NodeRegistry(redis_client=client_b)
    for registry in (nodes_a, nodes_b):
        registry.register(
            node_id="node_r",
            address="http://r:49983",
            total_memory_mb=1024,
            total_cpu_percent=200,
            total_disk_mb=2048,
            total_processes=128,
        )

    first = nodes_a.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert first is not None
    second = nodes_b.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert second is not None
    third = nodes_a.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert third is None  # shared node ledger is full

    nodes_b.release_quota(
        "node_r",
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        processes=64,
    )
    again = nodes_a.select_and_reserve(
        base_image=None, memory_mb=512, cpu_percent=100, disk_mb=1024, processes=64
    )
    assert again is not None


def test_redis_expired_reaped_across_replicas():
    import datetime
    import time

    server = fakeredis.FakeServer()
    registry_a = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    registry_b = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    record = _create(registry_a, timeout=1)
    time.sleep(1.5)
    expired = registry_b.remove_expired()
    assert [r.sandbox_id for r in expired] == [record.sandbox_id]
    _create(registry_a)  # quota released


def test_redis_migration_lock_excludes_other_replicas():
    """The SETNX migration marker is shared: one replica's claim blocks the
    other, and only the owning token releases it."""
    server = fakeredis.FakeServer()
    registry_a = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    registry_b = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )

    token = registry_a.try_acquire_migration("sbx_race")
    assert token is not None
    assert registry_b.try_acquire_migration("sbx_race") is None

    # A wrong token cannot clear the marker held by replica A.
    registry_b.release_migration("sbx_race", "stale-token")
    assert registry_b.try_acquire_migration("sbx_race") is None

    registry_a.release_migration("sbx_race", token)
    assert registry_b.try_acquire_migration("sbx_race") is not None


def test_redis_migration_lock_expires_after_ttl():
    """An owner that crashes (never releases) is unblocked by the TTL."""
    server = fakeredis.FakeServer()
    registry = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    token = registry.try_acquire_migration("sbx_crash", ttl=1)
    assert token is not None
    time.sleep(1.1)
    assert registry.try_acquire_migration("sbx_crash") is not None


# --------------------------------------------------------------- F11 step 1
#
# The node *view* (address, capacity, reservations, usage, health) used to live
# in each replica's process memory. That is the thing that made a second replica
# dangerous: two replicas could answer "healthy" and "unhealthy" about the same
# node in the same moment, and a heartbeat that arrived at the "wrong" replica
# was answered 404 -- so the worker re-registered and churned its node id. The
# view is shared now; these pin what that buys.


def _register(nodes, node_id="node_shared", **overrides) -> None:
    kwargs = dict(
        node_id=node_id,
        address="http://10.0.0.9:49983",
        total_memory_mb=4096,
        total_cpu_percent=200,
        total_disk_mb=8192,
        total_processes=512,
    )
    kwargs.update(overrides)
    nodes.register(**kwargs)


def test_the_node_view_is_shared_between_replicas():
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    nodes_b = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    _register(nodes_a)

    seen = nodes_b.get("node_shared")
    assert seen is not None, "replica B must see a node replica A registered"
    assert (seen.address, seen.total_memory_mb) == ("http://10.0.0.9:49983", 4096)
    assert [n.node_id for n in nodes_b.list()] == ["node_shared"]

    # A drain issued on one replica is a drain everywhere: placement on the
    # other replica must stop choosing it.
    assert nodes_a.set_draining("node_shared", True) is not None
    assert [n.draining for n in nodes_b.list()] == [True]


def test_a_heartbeat_lands_on_a_node_another_replica_registered():
    """The worker may dial any replica; 404 + re-register was the old answer."""
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    nodes_b = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    _register(nodes_a)
    before = nodes_a.get("node_shared").heartbeat_at

    record = nodes_b.heartbeat("node_shared")
    assert record is not None, "a heartbeat served by another replica must land"
    assert record.heartbeat_at > before
    assert nodes_a.get("node_shared").heartbeat_at == record.heartbeat_at


def test_both_replicas_derive_the_same_health_from_the_shared_view():
    """The verdict is a function of the shared stamp, not of local bookkeeping."""
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(
        heartbeat_timeout=0.05, redis_client=fakeredis.FakeRedis(server=server)
    )
    nodes_b = NodeRegistry(
        heartbeat_timeout=0.05, redis_client=fakeredis.FakeRedis(server=server)
    )
    _register(nodes_a)
    assert nodes_a.get("node_shared").status == "healthy"
    assert nodes_b.get("node_shared").status == "healthy"

    time.sleep(0.06)
    statuses = {
        nodes_a.get("node_shared").status,
        nodes_b.get("node_shared").status,
        nodes_a.list()[0].status,
        nodes_b.list()[0].status,
    }
    assert statuses == {"unhealthy"}, statuses

    nodes_b.heartbeat("node_shared")
    assert nodes_a.get("node_shared").status == "healthy"
    assert nodes_b.list()[0].status == "healthy"


class _OrphanRecorder:
    """The two calls ``reap_unhealthy`` makes on the sandbox registry."""

    def __init__(self) -> None:
        self.marked: list[str] = []

    def mark_orphaned(self, node_id: str) -> list:
        self.marked.append(node_id)
        return []

    def list_by_node(self, node_id: str) -> list:
        return []


def test_the_health_sweep_reads_the_shared_view_not_the_local_cache():
    """The *destructive* verdict has to come from the shared row too.

    Measured on the live cluster 2026-10-03 (two replicas, heartbeats every
    5 s, ``E2B_NODE_HEARTBEAT_TIMEOUT=30``): the replica that won the sweep
    round had gone 30.6 s without handling one of ``e2b-worker-1``'s
    heartbeats -- they were all landing on its peer -- while the worker was
    heartbeating into the shared row the whole time. The sweep judged it from
    **its own dict**, declared the node dead and orphaned that node's live
    sandboxes. ``get()``/``list()`` read through the view (pinned above); the
    one path that decides whether a worker is dead did not.
    """
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(
        heartbeat_timeout=0.05, redis_client=fakeredis.FakeRedis(server=server)
    )
    nodes_b = NodeRegistry(
        heartbeat_timeout=0.05, redis_client=fakeredis.FakeRedis(server=server)
    )
    _register(nodes_a)
    assert nodes_b.get("node_shared") is not None  # B has its own cached row
    time.sleep(0.06)
    # Only the peer keeps the shared row fresh -- exactly the live shape where
    # every heartbeat landed on the other replica.
    nodes_b.heartbeat("node_shared")

    sandboxes = _OrphanRecorder()
    nodes_a.reap_unhealthy(sandboxes)

    assert sandboxes.marked == [], (
        "the sweep orphaned a node whose shared row was fresh: "
        f"{sandboxes.marked}"
    )


def test_the_view_is_written_with_an_expiry():
    """A worker that is gone for good retires its own row -- in Redis, once."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    nodes = NodeRegistry(heartbeat_timeout=15.0, redis_client=client)
    _register(nodes)

    ttl = client.ttl("e2b:node:view:node_shared")
    assert ttl > 15.0, f"the view must outlive a heartbeat window: ttl={ttl}"
    assert ttl <= 60.0, f"and not much longer than that: ttl={ttl}"


def test_a_local_node_is_not_published_to_the_fleet():
    """``local://`` is a worker embedded in *this* replica, not a fleet node.

    Publishing it would let another replica place work on a worker it cannot
    reach, and both replicas' rows would collide on the id ``local``.
    """
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    nodes_b = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    nodes_a.add_local_node(
        total_memory_mb=1024,
        total_cpu_percent=100,
        total_disk_mb=2048,
        total_processes=128,
    )
    _register(nodes_a)

    assert sorted(n.node_id for n in nodes_a.list()) == ["local", "node_shared"]
    assert [n.node_id for n in nodes_b.list()] == ["node_shared"]
    assert nodes_b.get("local") is None


def test_usage_numbers_ride_the_shared_view():
    """What the worker reports to one replica is what the other places on."""
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    nodes_b = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    _register(nodes_a)

    record = nodes_b.heartbeat("node_shared")
    record.update_usage(used_disk_mb=1234, mcp_ports_in_use=7, mcp_ports_capacity=64)
    nodes_b.publish(record)

    seen = nodes_a.get("node_shared")
    assert (seen.used_disk_mb, seen.mcp_ports_in_use, seen.mcp_ports_capacity) == (
        1234,
        7,
        64,
    )


def test_only_one_replica_sweeps_each_round():
    """F11 step 2: the sweep is single-flight across replicas.

    It is duplicated *work* rather than extra coverage now -- the shared view
    means every replica derives the same verdict and the same orphan set -- and
    a duplicated round also duplicates the operator-facing warning.
    """
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    nodes_b = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))

    assert nodes_a.try_acquire_sweep(ttl_s=1) is True
    assert nodes_b.try_acquire_sweep(ttl_s=1) is False

    time.sleep(1.1)
    assert nodes_b.try_acquire_sweep(ttl_s=1) is True
    assert nodes_a.try_acquire_sweep(ttl_s=1) is False


def test_without_redis_this_process_is_the_sweeper():
    assert NodeRegistry().try_acquire_sweep(ttl_s=1) is True


# --------------------------------------------------------------- N70
#
# The view row (``e2b:node:view:<id>``) is **one JSON string** written whole, so
# the heartbeat and the delete path both do read-modify-write on it and two
# replicas lose each other's updates. The live shape (2026-10-02): the view's
# ``reserved_*`` came out *higher* than the sandbox records -- worker-0 holding a
# phantom 1024 MB -- the shared quota ledger was clean, no warning was logged,
# and only a re-registration pressed the view back down. The reservations now
# live in an atomic counter hash of their own, which the registry merges over
# the row on every read; the row keeps carrying the fields for a rolling
# upgrade, so a node with no counter hash still reads sensibly.


def test_concurrent_heartbeat_and_release_do_not_lose_the_release():
    """Replica B's stale read-modify-write must not un-release A's release."""
    server = fakeredis.FakeServer()
    nodes_a = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    nodes_b = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    _register(nodes_a)

    placed = nodes_a.select_and_reserve(
        base_image=None, memory_mb=1024, cpu_percent=200, disk_mb=8192, processes=512
    )
    assert placed is not None
    assert placed.reserved_memory_mb == 1024

    # The heartbeat on replica B read the row *before* the release, so the copy
    # it is about to write back still says the reservation is held.
    stale = nodes_b.get("node_shared")
    assert stale.reserved_memory_mb == 1024

    nodes_a.release_quota(
        "node_shared", memory_mb=1024, cpu_percent=200, disk_mb=8192, processes=512
    )
    nodes_b.publish(stale)  # whole-row write-back of the pre-release numbers

    # The row says 1024 again; the atomic counter says 0, and it wins.
    assert nodes_a.get("node_shared").reserved_memory_mb == 0
    assert nodes_b.get("node_shared").reserved_memory_mb == 0


def test_a_missing_counter_hash_falls_back_to_the_row():
    """A node with no counter yet (rolling upgrade) reads the row's fields."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    nodes_a = NodeRegistry(redis_client=client)
    nodes_b = NodeRegistry(redis_client=fakeredis.FakeRedis(server=server))
    _register(nodes_a)

    record = nodes_a.get("node_shared")
    record.reserved_memory_mb = 700
    record.reserved_cpu_percent = 42
    record.reserved_disk_mb = 300
    record.reserved_processes = 7
    nodes_a.publish(record)

    assert client.exists("e2b:node:res:node_shared") == 0
    seen = nodes_b.get("node_shared")
    assert (
        seen.reserved_memory_mb,
        seen.reserved_cpu_percent,
        seen.reserved_disk_mb,
        seen.reserved_processes,
    ) == (700, 42, 300, 7)


def test_a_negative_counter_is_named_not_clamped(caplog):
    """A counter below zero is reported verbatim, never floored to zero."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    nodes = NodeRegistry(redis_client=client)
    _register(nodes)
    client.hset(
        "e2b:node:res:node_shared",
        mapping={"memory": -512, "cpu": 0, "disk": 0, "processes": 0},
    )

    with caplog.at_level(logging.WARNING):
        seen = nodes.get("node_shared")

    assert seen.reserved_memory_mb == -512
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == [
        "node node_shared reservation counter is negative (memory=-512); the "
        "shared counter and the sandbox records disagree, and the counter is "
        "read as it stands rather than clamped to 0"
    ]


# --------------------------------------------------------------- F11 step 3


def _snapshot_registries(tmp_path, server):
    from control_plane.registry.snapshots import SnapshotRegistry

    return (
        SnapshotRegistry(tmp_path, redis_client=fakeredis.FakeRedis(server=server)),
        SnapshotRegistry(tmp_path, redis_client=fakeredis.FakeRedis(server=server)),
    )


def test_only_one_replica_claims_a_snapshot_copy(tmp_path):
    """A named snapshot id is claimed fleet-wide: one copy, not one per replica."""
    server = fakeredis.FakeServer()
    reg_a, reg_b = _snapshot_registries(tmp_path, server)

    assert reg_a.try_acquire_copy("snap_shared") is True
    assert reg_b.try_acquire_copy("snap_shared") is False

    reg_a.release_copy("snap_shared")
    assert reg_b.try_acquire_copy("snap_shared") is True


def test_a_copy_claim_expires_so_a_dead_replica_does_not_block_the_id(tmp_path):
    server = fakeredis.FakeServer()
    reg_a, reg_b = _snapshot_registries(tmp_path, server)

    assert reg_a.try_acquire_copy("snap_dead", ttl_s=1) is True
    time.sleep(1.1)
    assert reg_b.try_acquire_copy("snap_dead", ttl_s=1) is True


async def test_startup_reconcile_skips_the_copy_another_replica_is_running(tmp_path):
    """The startup pass must not walk over a *live* peer's copy.

    Re-driving every ``creating`` record is what settles the ones a restart
    orphaned -- but with two replicas a rolling restart also meets the peer's
    in-flight copy, and re-driving *that* submits a second POST for the same
    id. The worker answers 409 for a half-written payload, so the newcomer used
    to mark the record failed while its owner was one step from completing it:
    a client-visible failure, and a retry that copies the tree again. The
    shared claim is what tells the two apart, so the pass takes it first.
    """
    from control_plane.api.snapshots import reconcile_pending_snapshots

    class _Registry:
        """Stand-in for the sandbox view: reaching a worker is not this test."""

        def get(self, sandbox_id):
            raise RuntimeError("no worker in this test")

    class _App:
        class state:  # noqa: N801 - the handler only reads ``app.state``
            pass

    server = fakeredis.FakeServer()
    reg_a, reg_b = _snapshot_registries(tmp_path, server)
    in_flight = reg_a.reserve_from_sandbox(
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        source_sandbox_id="sbx_copying",
        node_id="worker-1",
        name="in-flight",
    )
    orphaned = reg_a.reserve_from_sandbox(
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        source_sandbox_id="sbx_orphaned",
        node_id="worker-1",
        name="orphaned",
    )
    # Exactly what the request path leaves behind while it copies: the id is
    # claimed and the record says ``creating``.
    assert reg_a.try_acquire_copy(in_flight.snapshot_id) is True

    app = _App()
    app.state.snapshots = reg_b
    app.state.registry = _Registry()

    # Only the record nobody claims is this pass's business.
    assert await reconcile_pending_snapshots(app) == 1
    left_alone = reg_b.get(in_flight.snapshot_id)
    assert left_alone.status == "creating"
    assert left_alone.error is None
    settled = reg_b.get(orphaned.snapshot_id)
    assert settled.status == "failed"
    assert settled.error == "interrupted by a restart: no worker in this test"


def test_without_redis_one_process_claims_everything(tmp_path):
    from control_plane.registry.snapshots import SnapshotRegistry

    registry = SnapshotRegistry(tmp_path)
    assert registry.try_acquire_copy("snap_any") is True


def test_a_creating_record_is_re_read_so_a_poll_sees_the_other_replicas_flip(tmp_path):
    """The record is shared (a file on the volume); the cache must not hide it.

    ``creating`` is the one state another replica can change under this one --
    it owns the copy and flips it when the bytes are in -- so a poll that lands
    on the "wrong" replica has to see the flip rather than wait forever.
    """
    server = fakeredis.FakeServer()
    reg_a, reg_b = _snapshot_registries(tmp_path, server)
    reserved = reg_a.reserve_from_sandbox(
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        source_sandbox_id="sbx_src",
        snapshot_id="snap_poll",
    )
    assert reserved.status == "creating"
    assert reg_b.get("snap_poll").status == "creating"

    reg_a.mark_completed("snap_poll")
    assert reg_b.get("snap_poll").status == "completed"


# --------------------------------------------------------------- F11 step 4


def test_a_rate_limit_is_one_budget_across_replicas():
    """Two replicas must not each spend the *configured* limit."""
    from control_plane.ratelimit import SlidingWindowRateLimiter

    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)
    limiter_a = SlidingWindowRateLimiter(3, name="create", redis_client=client_a)
    limiter_b = SlidingWindowRateLimiter(3, name="create", redis_client=client_b)

    assert [limiter_a.allow("k") for _ in range(3)] == [True, True, True]
    assert limiter_b.allow("k") is False, "the budget is fleet-wide, not per replica"
    assert limiter_b.allow("other") is True, "another key has its own budget"
    assert limiter_a.remaining("k") == 0
    assert limiter_b.remaining("other") == 2


def test_two_limits_do_not_spend_each_others_budget():
    """The limiter's *name* is part of the key (snapshot vs volume, ...)."""
    from control_plane.ratelimit import SlidingWindowRateLimiter

    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    snapshots = SlidingWindowRateLimiter(1, name="snapshot", redis_client=client)
    volumes = SlidingWindowRateLimiter(1, name="volume", redis_client=client)

    assert snapshots.allow("k") is True
    assert snapshots.allow("k") is False
    assert volumes.allow("k") is True


def test_without_redis_the_window_stays_local():
    from control_plane.ratelimit import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(1, name="create")
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_the_ttl_sweep_claim_is_single_flight():
    """F11 step 4: one replica expires sandboxes per round (shared deadlines)."""
    from control_plane.registry.redis_backend import try_claim

    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)

    assert try_claim(client_a, "e2b:ttl:sweep", ttl_s=1) is True
    assert try_claim(client_b, "e2b:ttl:sweep", ttl_s=1) is False
    time.sleep(1.1)
    assert try_claim(client_b, "e2b:ttl:sweep", ttl_s=1) is True
    assert try_claim(None, "e2b:ttl:sweep", ttl_s=1) is True, "no store = one process"
