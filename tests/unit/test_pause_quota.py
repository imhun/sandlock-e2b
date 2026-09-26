"""E9.2: pause releases the admission reservation, resume has to buy it back.

The eviction strategy ("hibernate the idle ones to make room") only pays off
if a parked sandbox really frees capacity, so these pin the accounting:
release on pause, re-admission on resume, rollback on refusal, and no
double-release when a parked sandbox is later deleted or reaped.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import (
    QUOTA_RELEASE_CLAIM_TTL_S,
    ResourceUnavailableError,
    SandboxRegistry,
    SandboxStateConflictError,
    workspace_disk_refusal,
)
from gateway_common.timeutil import utcnow


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        default_memory_mb=512,
        default_cpu_percent=0,
        default_disk_mb=0,
        default_max_processes=0,
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


def _pool(registry) -> int:
    return registry._reserved_memory


# -- in-memory registries -------------------------------------------------


def test_pause_frees_capacity_for_a_new_sandbox(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")  # pool now full (1024 MB)
    with pytest.raises(ResourceUnavailableError):
        _create(registry, sandbox_id="sbx_c")

    registry.pause(registry.get("sbx_a"))
    assert registry.get("sbx_a").state == "paused"
    assert registry.get("sbx_a").quota_released is True
    assert _pool(registry) == 512

    _create(registry, sandbox_id="sbx_c")  # room again
    assert _pool(registry) == 1024


def test_resume_reacquires_capacity(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    registry.delete("sbx_b")

    registry.resume(registry.get("sbx_a"))
    record = registry.get("sbx_a")
    assert record.state == "running"
    assert record.quota_released is False
    assert _pool(registry) == 512


def test_resume_when_full_keeps_the_sandbox_paused(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    _create(registry, sandbox_id="sbx_c")  # takes the freed slot

    with pytest.raises(ResourceUnavailableError) as exc:
        registry.resume(registry.get("sbx_a"))
    assert str(exc.value) == "No resources available"

    record = registry.get("sbx_a")
    assert record.state == "paused"
    assert record.quota_released is True
    assert _pool(registry) == 1024  # nothing booked, nothing lost


def test_delete_of_paused_sandbox_does_not_release_twice(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    assert _pool(registry) == 512

    registry.delete("sbx_a")
    assert _pool(registry) == 512  # only sbx_b still holds reservation
    _create(registry, sandbox_id="sbx_c")
    assert _pool(registry) == 1024


def test_concurrency_cap_ignores_paused_sandboxes(workspace):
    registry = SandboxRegistry(_settings(max_total_memory_mb=0, max_sandboxes=1))
    _create(registry, sandbox_id="sbx_a")
    with pytest.raises(ResourceUnavailableError):
        _create(registry, sandbox_id="sbx_b")
    registry.pause(registry.get("sbx_a"))
    _create(registry, sandbox_id="sbx_b")


def test_pause_rejects_double_pause_without_touching_the_ledger(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_a")
    registry.pause(record)
    assert _pool(registry) == 0

    with pytest.raises(SandboxStateConflictError):
        registry.pause(registry.get("sbx_a"))
    assert _pool(registry) == 0
    assert registry.get("sbx_a").quota_released is True


def test_resume_of_running_sandbox_leaves_reservation_alone(workspace):
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_a")
    with pytest.raises(SandboxStateConflictError):
        registry.resume(registry.get("sbx_a"))
    assert _pool(registry) == 512
    assert registry.get("sbx_a").quota_released is False


def test_ttl_reaps_running_but_parks_paused(workspace):
    registry = SandboxRegistry(_settings())
    running = _create(registry, sandbox_id="sbx_run")
    parked = _create(registry, sandbox_id="sbx_park")
    registry.pause(parked)
    for record in (running, parked):
        record.end_at = utcnow() - timedelta(seconds=10)

    expired = registry.remove_expired()
    assert [r.sandbox_id for r in expired] == ["sbx_run"]
    assert registry.get("sbx_park").state == "paused"
    assert _pool(registry) == 0


# -- tenant ledger --------------------------------------------------------


def test_pause_releases_tenant_quota_and_resume_rebooks_it(workspace):
    settings = _settings(
        tenant_limits={"t1": {"max_sandboxes": 1, "max_total_memory_mb": 1024}},
    )
    registry = SandboxRegistry(settings)
    _create(registry, sandbox_id="sbx_t1", tenant_id="t1")
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, sandbox_id="sbx_t1b", tenant_id="t1")
    assert str(exc.value) == "tenant quota exceeded"

    registry.pause(registry.get("sbx_t1"))
    assert registry._tenant_reserved["t1"]["sandboxes"] == 0
    _create(registry, sandbox_id="sbx_t1b", tenant_id="t1")
    assert registry._tenant_reserved["t1"]["sandboxes"] == 1


def test_resume_reports_tenant_quota_exhaustion(workspace):
    settings = _settings(
        max_total_memory_mb=0,
        tenant_limits={"t1": {"max_sandboxes": 1}},
    )
    registry = SandboxRegistry(settings)
    _create(registry, sandbox_id="sbx_t1", tenant_id="t1")
    registry.pause(registry.get("sbx_t1"))
    _create(registry, sandbox_id="sbx_t1b", tenant_id="t1")

    with pytest.raises(ResourceUnavailableError) as exc:
        registry.resume(registry.get("sbx_t1"))
    assert str(exc.value) == "tenant quota exceeded"
    assert registry.get("sbx_t1").state == "paused"


# -- shared store (multi-replica) ----------------------------------------


def test_shared_store_pause_frees_capacity_for_another_replica(workspace):
    server = fakeredis.FakeServer()
    client_a = fakeredis.FakeRedis(server=server)
    client_b = fakeredis.FakeRedis(server=server)
    replica_a = SandboxRegistry(_settings(), redis_client=client_a)
    replica_b = SandboxRegistry(_settings(), redis_client=client_b)

    _create(replica_a, sandbox_id="sbx_r1")
    _create(replica_a, sandbox_id="sbx_r2")
    with pytest.raises(ResourceUnavailableError):
        _create(replica_b, sandbox_id="sbx_r3")

    replica_a.pause(replica_a.get("sbx_r1"))
    assert replica_a.get("sbx_r1").quota_released is True
    _create(replica_b, sandbox_id="sbx_r3")  # room visible across replicas
    with pytest.raises(ResourceUnavailableError):
        _create(replica_b, sandbox_id="sbx_r4")

    # The released flag is durable: a resume still has to win admission again.
    with pytest.raises(ResourceUnavailableError):
        replica_b.resume(replica_b.get("sbx_r1"))
    assert replica_b.get("sbx_r1").state == "paused"


def test_shared_store_record_round_trips_quota_released(workspace):
    client = fakeredis.FakeRedis()
    registry = SandboxRegistry(_settings(), redis_client=client)
    _create(registry, sandbox_id="sbx_q")
    registry.pause(registry.get("sbx_q"))
    stored = registry._record_store.get("sbx_q")
    assert stored["quota_released"] is True
    assert registry.get("sbx_q").quota_released is True


# -- N30: the disk row on the ledger --------------------------------------
#
# The disk dimension is the one the ledger's other rows cannot vouch for: the
# pool it guards is the shared workspace, and the half a change moves is the
# release -- "resume re-books" is easy to remember, while the pause that gives
# the row back, the delete after a pause that must *not* give it back twice,
# and the crossing that must not look like a release are what a later edit to
# the pause/delete/TTL chain quietly breaks.


def test_disk_reservation_is_exactly_the_sum_of_live_records(registry, make_record):
    a = make_record(registry, disk_size_mb=64)
    b = make_record(registry, disk_size_mb=128)
    assert registry.global_reserved()["disk"] == 192
    registry.release_quota(b)
    assert registry.global_reserved()["disk"] == 64


def test_every_path_that_frees_a_sandbox_releases_the_disk_row_exactly_once(
    registry, make_record
):
    r = make_record(registry, disk_size_mb=64)
    assert registry.release_quota(r) is True  # pause / delete / expiry share it
    assert registry.release_quota(r) is False  # the second one is a no-op
    assert registry.global_reserved()["disk"] == 0
    assert registry.hold_quota(r) is True  # resume takes it back
    assert registry.global_reserved()["disk"] == 64


def test_an_over_budget_sandbox_keeps_its_reservation(registry, make_record):
    r = make_record(registry, disk_size_mb=64)
    registry.enforce_disk_budget({r.sandbox_id: 128 * 1024 * 1024})
    assert registry.global_reserved()["disk"] == 64


def test_pause_releases_the_disk_row_once_and_the_delete_after_it_is_a_no_op(
    registry, make_record
):
    """A parked sandbox keeps its record, so its delete calls release again.

    The second record is the instrument: with only the parked one booked the
    ledger sits at 0 either way, and a clamped double release would hide
    there. With ``other`` holding 128 MiB, a delete that subtracted the parked
    record's 64 MiB a second time shows up as 0 instead of 128.
    """
    parked = make_record(registry, disk_size_mb=64)
    other = make_record(registry, disk_size_mb=128)
    registry.pause(parked)
    assert registry.global_reserved()["disk"] == 128

    registry.delete(parked.sandbox_id)
    assert registry.global_reserved()["disk"] == 128
    registry.delete(other.sandbox_id)
    assert registry.global_reserved()["disk"] == 0


def test_delete_releases_the_disk_row_once(registry, make_record):
    record = make_record(registry, disk_size_mb=64)
    registry.delete(record.sandbox_id)
    assert registry.global_reserved()["disk"] == 0
    # The record object survives the delete, so a caller that released it
    # again (or a retry of the same delete) must still be a no-op.
    assert registry.release_quota(record) is False


def test_ttl_expiry_releases_the_disk_row_once(registry, make_record):
    record = make_record(registry, disk_size_mb=64)
    # A second record: the sweep of the first must not touch this one's row.
    other = make_record(registry, disk_size_mb=128)
    record.end_at = utcnow() - timedelta(seconds=10)

    expired = registry.remove_expired()
    assert [r.sandbox_id for r in expired] == [record.sandbox_id]
    assert registry.global_reserved()["disk"] == 128
    assert registry.release_quota(record) is False
    assert registry.get(other.sandbox_id).quota_released is False


def test_a_released_disk_budget_is_bookable_by_the_next_create():
    """The row comes back *and* it was the thing standing in the way.

    Bounded on purpose: on an unbounded pool "the next create took it" is true
    even when nothing was returned, so the case first makes the 64 MiB ceiling
    refuse the second create, and checks the refusal is the disk one.
    """
    registry = SandboxRegistry(
        _settings(max_total_memory_mb=0, max_total_disk_mb=64, default_disk_mb=64)
    )
    first = _create(registry, sandbox_id="sbx_n30_first")
    assert registry.global_reserved()["disk"] == 64

    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry, sandbox_id="sbx_n30_second")
    assert str(exc.value) == workspace_disk_refusal(64, 64)

    registry.release_quota(first)
    assert registry.global_reserved()["disk"] == 0

    second = _create(registry, sandbox_id="sbx_n30_second")
    assert second.disk_size_mb == 64
    assert registry.global_reserved()["disk"] == 64


def test_shared_store_pause_then_delete_returns_the_disk_row_once(make_record):
    """A parked record's delete must be a no-op on the shared ledger too.

    Here the gate is the flag *in the store*: the delete re-reads the record,
    so a release path that trusted the caller's in-memory copy instead would
    subtract the parked sandbox's row a second time -- and on this backend
    there is no clamp to hide it, the row goes negative.
    """
    registry = SandboxRegistry(_settings(), redis_client=fakeredis.FakeRedis())
    parked = make_record(registry, disk_size_mb=64)
    make_record(registry, disk_size_mb=128)
    registry.pause(registry.get(parked.sandbox_id))
    assert registry.global_reserved()["disk"] == 128

    registry.delete(parked.sandbox_id)
    assert registry.global_reserved()["disk"] == 128
    assert registry.global_reserved()["disk"] == sum(
        r.disk_size_mb for r in registry.list() if not r.quota_released
    )


def test_shared_store_disk_row_follows_the_live_records_and_returns_once(make_record):
    """The same invariant on the ledger the replicas share.

    The in-memory counters and the shared store are two implementations of one
    contract; a release that only moved one of them would be invisible to a
    single-replica case, and the store path is the one a fix forgets.

    The case drives ``pause``/``resume``/``delete`` rather than calling
    ``release_quota`` directly: on this backend the record's own row lives in
    the store as well, and it is the paths (each of which saves the record)
    that keep the two copies agreeing.
    """
    client = fakeredis.FakeRedis()
    registry = SandboxRegistry(_settings(), redis_client=client)
    make_record(registry, disk_size_mb=64)
    b = make_record(registry, disk_size_mb=128)

    def live_disk_mb() -> int:
        return sum(r.disk_size_mb for r in registry.list() if not r.quota_released)

    assert registry.global_reserved()["disk"] == 192
    assert registry.global_reserved()["disk"] == live_disk_mb()

    registry.pause(registry.get(b.sandbox_id))
    assert registry.global_reserved()["disk"] == 64
    assert registry.global_reserved()["disk"] == live_disk_mb()

    registry.resume(registry.get(b.sandbox_id))
    assert registry.global_reserved()["disk"] == 192
    assert registry.global_reserved()["disk"] == live_disk_mb()

    registry.delete(b.sandbox_id)
    assert registry.global_reserved()["disk"] == 64
    assert registry.global_reserved()["disk"] == live_disk_mb()


# -- N41: one reservation, one return -- across replicas ------------------
#
# ``pause`` is two shared-store writes: the ledger rows move first, the record
# that carries ``quota_released`` is saved second. Another replica whose delete
# lands in between reads the *pre-pause* flag, decides the reservation is still
# held, and gives it back a second time. The ledger then under-counts
# reservations, which is the direction that over-sells the fleet -- so the
# "somebody is already returning this" fact has to be one atomic store write
# rather than a flag the loser has not seen yet.


def _replicas(**overrides):
    """Two registries sharing one fakeredis, in the production shape."""
    server = fakeredis.FakeServer()
    return (
        SandboxRegistry(_settings(**overrides), redis_client=fakeredis.FakeRedis(server=server)),
        SandboxRegistry(_settings(**overrides), redis_client=fakeredis.FakeRedis(server=server)),
    )


def test_a_delete_inside_the_pauses_window_cannot_return_the_row_twice(make_record):
    """The N41 shape: replica B's delete lands between A's two store writes."""
    replica_a, replica_b = _replicas()
    make_record(replica_a, disk_size_mb=128, sandbox_id="sbx_n41")

    stale = replica_b.get("sbx_n41")  # B's copy, read while the record is live
    assert replica_a.release_quota(replica_a.get("sbx_n41")) is True  # pause, 1 of 2
    assert replica_a.global_reserved()["disk"] == 0

    # A has not saved the record yet, so B's copy still says the reservation is
    # held. The delete must not be able to move the row a second time.
    replica_b.delete(stale.sandbox_id)
    assert replica_a.global_reserved()["disk"] == 0
    assert replica_b.global_reserved()["disk"] == 0


def test_the_return_claim_is_dropped_when_the_reservation_comes_back(make_record):
    """The claim belongs to *one* episode: resume must clear it.

    Otherwise the row it protects would never be returnable again -- the
    reservation would leak for the marker's whole TTL, which is the same bug
    wearing the other sign.
    """
    registry = SandboxRegistry(_settings(), redis_client=fakeredis.FakeRedis())
    record = make_record(registry, disk_size_mb=128)

    registry.pause(registry.get(record.sandbox_id))
    assert registry.global_reserved()["disk"] == 0
    registry.resume(registry.get(record.sandbox_id))
    assert registry.global_reserved()["disk"] == 128
    registry.delete(record.sandbox_id)
    assert registry.global_reserved()["disk"] == 0


def test_a_reused_sandbox_id_is_not_blocked_by_the_previous_episodes_claim(make_record):
    """A second record under the same id holds a reservation of its own.

    Clients may pass ``X-Sandbox-Id`` and the suite reuses ids constantly, so
    the claim has to name the *reservation* (the created record), not the id.
    """
    registry = SandboxRegistry(_settings(), redis_client=fakeredis.FakeRedis())
    make_record(registry, disk_size_mb=64, sandbox_id="sbx_reused")
    registry.delete("sbx_reused")
    assert registry.global_reserved()["disk"] == 0

    make_record(registry, disk_size_mb=64, sandbox_id="sbx_reused")
    assert registry.global_reserved()["disk"] == 64
    registry.delete("sbx_reused")
    assert registry.global_reserved()["disk"] == 0


def test_the_return_claim_outlives_the_delete_it_protects_and_has_a_ttl(make_record):
    """Why the delete must *not* clear the guard, and why the TTL matters.

    Clearing it there would reopen the very window it closes (the loser's
    delete runs last). Leaving it forever would leak a reservation whenever a
    replica dies holding the claim, so it expires -- bounded, and small enough
    to be a recovery time rather than a leak.
    """
    server = fakeredis.FakeServer()
    client_b = fakeredis.FakeRedis(server=server)
    replica_a = SandboxRegistry(
        _settings(), redis_client=fakeredis.FakeRedis(server=server)
    )
    replica_b = SandboxRegistry(_settings(), redis_client=client_b)
    make_record(replica_a, disk_size_mb=64, sandbox_id="sbx_n41_claim")

    stale = replica_b.get("sbx_n41_claim")
    assert replica_a.release_quota(replica_a.get("sbx_n41_claim")) is True
    replica_b.delete(stale.sandbox_id)
    assert replica_a.global_reserved()["disk"] == 0

    key = replica_a._quota_release_key(stale)
    assert client_b.get(key) == b"1"  # the loser's delete left the guard alone
    assert client_b.ttl(key) == QUOTA_RELEASE_CLAIM_TTL_S
    assert 0 < QUOTA_RELEASE_CLAIM_TTL_S <= 3600
    # ...so a third attempt at the same reservation is refused as well.
    assert replica_b.release_quota(stale) is False
    assert replica_a.global_reserved()["disk"] == 0
