"""N46: a copy holds a refreshed lease, so "in flight" stops meaning "orphan".

The defect: ``reconcile_pending_snapshots`` asks "is somebody copying this id?"
with the fleet-wide claim (``try_acquire_copy``), and the request path only took
that claim for a *named* id (``Idempotency-Key``/``snapshotID``). An unnamed
async copy had no marker at all, so its ``creating`` record was
indistinguishable from an orphan's and a peer's pass re-drove it: a second copy
of the same tree, and -- on the real worker, where the payload is half written
-- a 409 that the pass recorded as ``failed``.

The lease is that marker: the replica running the copy takes the id with a
token, refreshes it for as long as the copy runs (TTL ``COPY_LEASE_TTL_S``,
refresh ``COPY_LEASE_REFRESH_S``) and releases it when the record carries the
answer. A live copy is therefore protected, and a dead owner stops looking live
within the TTL instead of forever.

Timing is driven by an injected clock here: ``fakeredis`` expires keys on the
wall clock, so "a copy that outlives its TTL ten times over" would need minutes
of sleeping there, while the facts under test (who holds the id, and when) are
the same either way. ``tests/unit/test_redis_multireplica.py`` exercises the
same lease against ``fakeredis`` where the timing is not the point.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

import pytest

from control_plane.api.snapshots import (
    SNAPSHOT_RECONCILE_INTERVAL_S,
    _refresh_copy_lease,
    reconcile_pending_snapshots,
    reconcile_pending_snapshots_or_report,
)
from control_plane.registry.snapshots import (
    COPY_LEASE_REFRESH_S,
    COPY_LEASE_TTL_S,
    SnapshotRegistry,
)


class _StopRefreshing(Exception):
    """Ends the refresher in a test, instead of cancelling its task."""


class _ClockStore:
    """The Redis operations the copy lease uses, against an injected clock.

    ``set``/``get``/``expire``/``delete`` are the whole surface
    ``SnapshotRegistry`` touches, so the registry code under test is the real
    one; only the clock moves.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._values: dict[str, str] = {}
        self._expires: dict[str, float] = {}

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, at in self._expires.items() if at <= now]:
            self._values.pop(key, None)
            self._expires.pop(key, None)

    def set(self, key, value, *, nx: bool = False, ex=None):
        self._drop_expired()
        if nx and key in self._values:
            return None
        self._values[key] = value.decode() if isinstance(value, bytes) else value
        if ex is None:
            self._expires.pop(key, None)
        else:
            self._expires[key] = self._clock() + ex
        return True

    def get(self, key):
        self._drop_expired()
        return self._values.get(key)

    def expire(self, key, seconds) -> bool:
        self._drop_expired()
        if key not in self._values:
            return False
        self._expires[key] = self._clock() + seconds
        return True

    def delete(self, key) -> int:
        self._drop_expired()
        return 1 if self._values.pop(key, None) is not None else 0


def _registries(tmp_path: Path, store) -> tuple[SnapshotRegistry, SnapshotRegistry]:
    """Two replicas over one shared store (and one shared record directory)."""
    return (
        SnapshotRegistry(tmp_path, redis_client=store),
        SnapshotRegistry(tmp_path, redis_client=store),
    )


def _reserve(registry: SnapshotRegistry, *, source: str | None, name: str):
    return registry.reserve_from_sandbox(
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        source_sandbox_id=source,
        node_id="worker-1",
        name=name,
    )


class _Registry:
    """Stand-in for the sandbox view: no worker is reachable from these tests."""

    def get(self, sandbox_id):
        raise RuntimeError("no worker in this test")


class _App:
    class state:  # noqa: N801 - the pass only reads ``app.state``
        pass


def _app_for(registry: SnapshotRegistry) -> _App:
    app = _App()
    app.state.snapshots = registry
    app.state.registry = _Registry()
    return app


async def test_a_refreshed_lease_outlives_its_ttl_by_ten_times(tmp_path):
    """Requirement ④: the refresh is what keeps a long copy's lease alive.

    A copy can run for minutes (the worker call times out at 120 s; N32
    measured 76 s for a 2000-file tree) while the lease has to be short, so the
    owner's refresh is load-bearing: without it the lease would lapse mid-copy
    and the record would be settled as an orphan anyway. Ten TTLs of copying
    are simulated here, and the control probe right at the start proves the
    store really does expire an unrefreshed lease at that TTL -- otherwise this
    test would pass for the wrong reason.
    """
    now = [1000.0]
    store = _ClockStore(lambda: now[0])
    owner, rival = _registries(tmp_path, store)

    # The control: an id nobody refreshes is free again one second past the TTL.
    assert rival.try_acquire_copy("snap_unrefreshed", ttl_s=30, token="rival") is True
    now[0] += 31
    assert (
        rival.try_acquire_copy("snap_unrefreshed", ttl_s=30, token="rival") is True
    ), "the store does not expire claims -- the test below would prove nothing"

    snapshot_id = "snap_long_copy"
    token = "owner-token"
    assert owner.try_acquire_copy(snapshot_id, ttl_s=30, token=token) is True
    started_at = now[0]

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) > 30:
            raise _StopRefreshing
        await asyncio.sleep(0)

    with pytest.raises(_StopRefreshing):
        await _refresh_copy_lease(
            owner,
            snapshot_id,
            token,
            ttl_s=COPY_LEASE_TTL_S,
            refresh_s=10,
            sleep=fake_sleep,
        )

    assert len(sleeps) == 31, "the refresher did not run once per interval"
    assert now[0] == started_at + 31 * 10
    assert (
        rival.try_acquire_copy(snapshot_id, ttl_s=30, token="rival") is False
    ), "a peer took over an id this replica is still copying"


async def test_a_rival_cannot_refresh_or_take_a_live_lease(tmp_path):
    """The lease is token-owned: a rival neither extends nor steals it.

    An unconditional "extend this key" refresh would let any replica keep a
    lease alive -- including one that had just lost the id to a peer, which is
    how a *settled* record would look live again. The negative answer is what
    makes the TTL mean "the owner stopped saying so".
    """
    now = [1000.0]
    store = _ClockStore(lambda: now[0])
    owner, rival = _registries(tmp_path, store)
    snapshot_id = "snap_owned"
    assert owner.try_acquire_copy(snapshot_id, ttl_s=30, token="owner") is True

    assert rival.try_acquire_copy(snapshot_id, ttl_s=30, token="rival") is False
    assert rival.refresh_copy(snapshot_id, "rival", ttl_s=30) is False
    assert owner.refresh_copy(snapshot_id, "owner", ttl_s=30) is True

    # The failed rival refresh extended nothing: the lease still ends one second
    # past the *owner's* last refresh, and then the id is up for grabs again.
    now[0] += 31
    assert rival.try_acquire_copy(snapshot_id, ttl_s=30, token="rival") is True


async def test_the_pass_settles_an_expired_lease_and_spares_a_refreshed_one(
    tmp_path,
):
    """Requirement ①②: the pass reads the lease, and the TTL is the clock.

    Two records are ``creating`` with an owner that has stopped refreshing: the
    one whose owner is *still there* (its lease is refreshed) must be left
    alone for as long as that lasts, and the one whose owner is gone (lease
    expired) is the pass's business. That is the whole trade the lease makes --
    a dead owner is settled after the TTL, not never, and not immediately.
    """
    now = [1000.0]
    store = _ClockStore(lambda: now[0])
    owner, scanner = _registries(tmp_path, store)
    ttl = COPY_LEASE_TTL_S

    live = _reserve(owner, source="sbx_live", name="live")
    dead = _reserve(owner, source="sbx_dead", name="dead")
    assert owner.try_acquire_copy(live.snapshot_id, ttl_s=ttl, token="live") is True
    assert owner.try_acquire_copy(dead.snapshot_id, ttl_s=ttl, token="dead") is True

    app = _app_for(scanner)
    assert await reconcile_pending_snapshots(app) == 0
    assert scanner.get(live.snapshot_id).status == "creating"
    assert scanner.get(dead.snapshot_id).status == "creating"

    # Copy on past the point where the dead owner's silence has to matter: the
    # bound is the lease TTL plus one reconcile round (30 s + 10 s), and it is
    # asserted here as a number of seconds rather than through the constants,
    # because "settled within a minute" is the operational promise. The live
    # copy's owner keeps saying "still here" on its cadence meanwhile (that
    # refresh is the refresher's job, pinned above).
    silence_s = 61
    for _ in range(silence_s // COPY_LEASE_REFRESH_S):
        now[0] += COPY_LEASE_REFRESH_S
        assert owner.refresh_copy(live.snapshot_id, "live", ttl_s=ttl) is True
    now[0] += silence_s % COPY_LEASE_REFRESH_S

    assert await reconcile_pending_snapshots(app) == 1
    spared = scanner.get(live.snapshot_id)
    assert spared.status == "creating"
    assert spared.error is None
    settled = scanner.get(dead.snapshot_id)
    assert settled.status == "failed"
    assert settled.error == "interrupted by a restart: no worker in this test"


async def test_a_late_release_cannot_delete_the_lease_a_peer_took(tmp_path):
    """Releasing is conditional, or a stale owner could free a live copy.

    The release at the end of a copy is the "the record carries the answer
    now" signal. If it deleted the key unconditionally, an owner that had
    already lost the id -- one that stalled past the TTL while a peer took the
    record over -- would delete the *peer's* lease on its way out, and the next
    pass would then settle a copy the peer is still running. Conditional on the
    token, a late release is a no-op.
    """
    now = [1000.0]
    store = _ClockStore(lambda: now[0])
    owner, peer = _registries(tmp_path, store)
    snapshot_id = "snap_handover"
    assert owner.try_acquire_copy(snapshot_id, ttl_s=30, token="owner") is True
    now[0] += 31
    assert peer.try_acquire_copy(snapshot_id, ttl_s=30, token="peer") is True

    owner.release_copy(snapshot_id, token="owner")
    assert peer.refresh_copy(snapshot_id, "peer", ttl_s=30) is True


async def test_an_owner_that_comes_back_after_a_restart_settles_its_own_record(
    tmp_path,
):
    """The lease must not outlive the *record* either (requirement ①③).

    A copy that finished leaves ``completed`` and releases; a copy whose owner
    died leaves ``creating`` and, once the lease is gone, the next pass
    re-drives it -- the worker's answer (200 for a finished payload, 409 for a
    half-written one) is what decides ``completed`` vs ``failed``. Settling is
    the recovery, not the harm: what was harmful was settling a copy that was
    still running.
    """
    now = [1000.0]
    store = _ClockStore(lambda: now[0])
    owner, scanner = _registries(tmp_path, store)
    record = _reserve(owner, source="sbx_resumed", name="resumed")
    assert owner.try_acquire_copy(record.snapshot_id, ttl_s=30, token="owner")

    app = _app_for(scanner)
    assert await reconcile_pending_snapshots(app) == 0
    now[0] += COPY_LEASE_TTL_S + 1
    assert await reconcile_pending_snapshots(app) == 1
    settled = scanner.get(record.snapshot_id)
    assert settled.status == "failed"
    assert settled.error == "interrupted by a restart: no worker in this test"
    # The pass released the lease it took to do that work, so a later pass is
    # not blocked by its own claim.
    assert scanner.try_acquire_copy(record.snapshot_id, token="probe") is True


def test_only_one_replica_runs_a_reconcile_round(tmp_path):
    """Requirement ③: the periodic pass is single-flighted like every sweep.

    Two replicas that each ran the pass would settle the same records twice --
    duplicated ``mark_failed`` writes, duplicated re-drives of a tree -- so the
    round is claimed with the store's TTL'd-key shape
    (``try_acquire_sweep``/``try_claim``), and one dies mid-round at the cost of
    exactly one round.
    """
    now = [1000.0]
    store = _ClockStore(lambda: now[0])
    a, b = _registries(tmp_path, store)

    assert a.try_acquire_reconcile(ttl_s=10) is True
    assert b.try_acquire_reconcile(ttl_s=10) is False
    now[0] += 11
    assert b.try_acquire_reconcile(ttl_s=10) is True
    assert a.try_acquire_reconcile(ttl_s=10) is False


def test_without_a_store_this_process_does_the_round_and_holds_every_lease(
    tmp_path,
):
    """Single-process deployments keep the shape they had (no store, no peers).

    There is nothing to coordinate without a shared store: this replica is the
    only copy owner and the only reconcile loop, so the lease and the round
    claim answer yes, and the in-process ``_IN_FLIGHT_COPIES`` set is what keeps
    the pass off this replica's own copy (pinned in the contract suite).
    """
    solo = SnapshotRegistry(tmp_path)
    assert solo.try_acquire_copy("snap_any", token="solo") is True
    assert solo.refresh_copy("snap_any", "solo", ttl_s=COPY_LEASE_TTL_S) is True
    assert solo.try_acquire_reconcile(ttl_s=10) is True
    solo.release_copy("snap_any", token="solo")


def test_the_lease_is_short_enough_to_be_a_lease():
    """The operational contract the constants carry (N46).

    Behaviour is pinned above -- a refreshed lease survives ten TTLs of
    copying, an unrefreshed one is free again one second past the TTL. This
    pins the *order of magnitude*, because the second half of the trade is why
    a lease was chosen over "hold the claim until the copy ends": an owner that
    dies has to stop looking live in seconds. A TTL measured in minutes would
    leave the dead owner's record unsettled for that long, and a TTL of the
    same order as the refresh interval would lapse a live copy on one missed
    tick. The settle bound an operator reads is
    ``COPY_LEASE_TTL_S + SNAPSHOT_RECONCILE_INTERVAL_S`` (plus the re-drive
    itself, which is bounded by the worker call's own timeout).
    """
    assert COPY_LEASE_TTL_S <= 60
    assert COPY_LEASE_REFRESH_S * 2 <= COPY_LEASE_TTL_S
    assert SNAPSHOT_RECONCILE_INTERVAL_S <= 60


async def test_a_peer_deleting_the_record_mid_settle_is_named_not_raised(
    tmp_path, monkeypatch, caplog
):
    """N74: the *reason* survives the record, and the pass does not blow up.

    A peer's ``delete`` is the truth (its ``rmtree`` removes the very file this
    pass is about to write the failure into) -- the same race ``_run_reserved_capture``
    already names. The defect was that this call site let ``UnknownSnapshotError``
    escape: the reason vanished with the record, and the exception only surfaced
    at garbage-collection time in a fire-and-forget startup task.
    """
    now = [1000.0]
    store = _ClockStore(lambda: now[0])
    owner, scanner = _registries(tmp_path, store)
    record = _reserve(owner, source="sbx_gone", name="gone")
    assert (
        owner.try_acquire_copy(
            record.snapshot_id, ttl_s=COPY_LEASE_TTL_S, token="dead"
        )
        is True
    )

    def _peer_deletes_then_fails(*_args, **_kwargs):
        scanner.delete(record.snapshot_id)
        raise RuntimeError("no worker in this test")

    monkeypatch.setattr(
        "control_plane.api.snapshots._capture_reserved", _peer_deletes_then_fails
    )

    now[0] += COPY_LEASE_TTL_S + 1
    with caplog.at_level(logging.WARNING, logger="control_plane.api.snapshots"):
        assert await reconcile_pending_snapshots(_app_for(scanner)) == 1

    messages = [log_record.getMessage() for log_record in caplog.records]
    assert (
        f"snapshot {record.snapshot_id}: interrupted by a restart: "
        "no worker in this test"
    ) in messages
    assert (
        f"snapshot {record.snapshot_id}: the record was deleted by another "
        "replica before the interruption could be recorded"
    ) in messages


async def test_the_startup_wrapper_reports_a_failed_pass_and_swallows_it(
    tmp_path, monkeypatch, caplog
):
    """N74: the one-shot startup task has nowhere to surface an exception.

    It logs one instead. Nothing is lost by swallowing here: the periodic
    ``snapshot_reconcile_loop`` is the retry, and it does not depend on this
    pass having succeeded.
    """
    registry = SnapshotRegistry(tmp_path)
    app = _app_for(registry)

    def _boom():
        raise RuntimeError("the record store is unreadable")

    monkeypatch.setattr(registry, "in_progress", _boom)

    with caplog.at_level(logging.ERROR, logger="control_plane.api.snapshots"):
        assert await reconcile_pending_snapshots_or_report(app) == 0

    assert [
        log_record.getMessage()
        for log_record in caplog.records
        if log_record.levelno == logging.ERROR
    ] == ["startup snapshot reconcile pass failed"]


async def test_the_startup_wrapper_still_honours_cancellation(tmp_path, monkeypatch):
    """N74: shutdown is not a failure, so ``CancelledError`` keeps going."""
    app = _app_for(SnapshotRegistry(tmp_path))

    async def _cancelled(_app):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "control_plane.api.snapshots.reconcile_pending_snapshots", _cancelled
    )

    with pytest.raises(asyncio.CancelledError):
        await reconcile_pending_snapshots_or_report(app)
