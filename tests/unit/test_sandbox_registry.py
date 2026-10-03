"""Registry create / get / delete / max-concurrency behavior."""

from __future__ import annotations

import time

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import (
    ResourceUnavailableError,
    SandboxRecord,
    SandboxRegistry,
    UnknownSandboxError,
)
from gateway_common.timeutil import utcnow


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=3,
        max_total_memory_mb=0,
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


def test_create_and_get(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry)
    assert record.sandbox_id.startswith("sbx_")
    assert record.client_id.startswith("cli_")
    assert record.envd_access_token.startswith("tok_")
    assert record.state == "running"
    assert record.envd_version == "0.6.4+sandlock"
    assert registry.get(record.sandbox_id) is record
    assert registry.count() == 1


def test_lookup_missing_raises(workspace):
    registry = SandboxRegistry(_settings())
    with pytest.raises(UnknownSandboxError):
        registry.get("sbx_nonexistent")
    with pytest.raises(UnknownSandboxError):
        registry.get("../etc/passwd")


def test_migration_lock_single_owner_in_memory():
    """Only one migration may hold a sandbox; the token gates the release."""
    registry = SandboxRegistry(_settings())
    first = registry.try_acquire_migration("sbx_migrating")
    assert first is not None
    # A second claim (e.g. another request in the same process) is refused.
    assert registry.try_acquire_migration("sbx_migrating") is None
    # A stale token cannot release the lock held by someone else.
    registry.release_migration("sbx_migrating", "stale-token")
    assert registry.try_acquire_migration("sbx_migrating") is None
    # The owner's token releases it and a fresh claim succeeds.
    registry.release_migration("sbx_migrating", first)
    assert registry.try_acquire_migration("sbx_migrating") is not None


def test_migration_lock_expires_in_memory():
    """A crashed migration holder cannot block migrations forever."""
    registry = SandboxRegistry(_settings())
    token = registry.try_acquire_migration("sbx_expiring", ttl=1)
    assert token is not None
    time.sleep(1.1)
    assert registry.try_acquire_migration("sbx_expiring") is not None


def test_network_field_round_trip_and_detail(workspace):
    registry = SandboxRegistry(_settings())
    network = {"allowOut": ["8.8.8.8"], "allowPublicTraffic": True}
    record = _create(registry, network=network)
    assert record.network == network
    assert record.as_detail()["network"] == network
    restored = SandboxRecord.from_storage_dict(record.to_storage_dict())
    assert restored.network == network


def test_network_field_defaults_to_none(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry)
    assert record.network is None
    assert record.as_detail()["network"] == {}


def test_delete_releases_entry(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry)
    deleted = registry.delete(record.sandbox_id)
    assert deleted is record
    assert registry.count() == 0
    with pytest.raises(UnknownSandboxError):
        registry.delete(record.sandbox_id)


def test_an_orphaned_record_is_collected_once_the_opt_in_grace_elapses(workspace):
    """N22: ``E2B_ORPHAN_RECORD_TTL`` is the only way an orphan ever goes away.

    Orphaned records are protected from expiry because the lost worker may still
    be running them -- but a worker that never comes back then keeps its
    records, its quota rows and its trees on the shared base forever. The grace
    period is opt-in (0 = never, the default) and measured from the record's own
    transition, so a long outage is what elapses, not a repeated sweep.
    """
    from datetime import timedelta

    registry = SandboxRegistry(_settings(orphan_record_ttl_s=60))
    record = _create(registry)
    record.node_id = "node_a"
    registry.save(record)
    registry.mark_orphaned("node_a")
    assert registry.get(record.sandbox_id).orphaned_at is not None

    record.end_at = utcnow() - timedelta(seconds=10)
    assert registry.remove_expired() == []          # orphaned seconds ago

    # The outage is what ages: push the stamp past the grace period.
    orphaned = registry.get(record.sandbox_id)
    orphaned.orphaned_at = utcnow() - timedelta(seconds=120)
    registry.save(orphaned)
    assert [r.sandbox_id for r in registry.remove_expired()] == [record.sandbox_id]
    assert registry.list() == []


def test_the_default_keeps_the_promise_that_an_orphan_is_never_reaped(workspace):
    """Without the setting, an orphaned record survives its deadline forever."""
    from datetime import timedelta

    registry = SandboxRegistry(_settings())
    record = _create(registry)
    record.node_id = "node_a"
    registry.save(record)
    registry.mark_orphaned("node_a")
    orphaned = registry.get(record.sandbox_id)
    orphaned.end_at = utcnow() - timedelta(days=365)
    orphaned.orphaned_at = utcnow() - timedelta(days=365)
    registry.save(orphaned)

    assert registry.remove_expired() == []
    assert registry.get(record.sandbox_id).state == "orphaned"


def _stampless_orphan(registry, *, age_s: float):
    """An orphan the way an *older* build left one: no ``orphaned_at`` stamp.

    ``mark_orphaned`` only stamps on the transition, so a record that was
    already ``orphaned`` when the stamp was introduced never gets one -- and
    the TTL branch used to read "no stamp" as "never collect this", which made
    the opt-in switch silently useless for exactly the records it was added
    for (N61).
    """
    from datetime import timedelta

    record = _create(registry)
    record.node_id = "node_a"
    record.state = "orphaned"
    record.orphaned_at = None
    record.end_at = utcnow() - timedelta(seconds=age_s)
    registry.save(record)
    return record


def test_a_stampless_orphan_is_collected_once_the_opt_in_grace_elapses(workspace):
    registry = SandboxRegistry(_settings(orphan_record_ttl_s=300))
    record = _stampless_orphan(registry, age_s=301)

    assert [r.sandbox_id for r in registry.remove_expired()] == [record.sandbox_id]
    assert registry.list() == []


def test_a_stampless_orphan_survives_while_the_switch_is_off(workspace):
    registry = SandboxRegistry(_settings())
    _stampless_orphan(registry, age_s=365 * 24 * 3600)

    assert registry.remove_expired() == []
    assert registry.list()[0].state == "orphaned"


def test_a_stamped_orphan_still_ages_from_its_own_stamp(workspace):
    """The fallback must not hijack a record whose outage is younger than its TTL."""
    from datetime import timedelta

    registry = SandboxRegistry(_settings(orphan_record_ttl_s=300))
    record = _stampless_orphan(registry, age_s=3600)
    record.orphaned_at = utcnow() - timedelta(seconds=100)
    registry.save(record)

    assert registry.remove_expired() == []
    assert registry.list()[0].state == "orphaned"


def test_a_recovered_sandbox_loses_its_orphan_stamp(workspace):
    """Recovery clears the stamp, so the *next* outage gets its own grace.

    Otherwise a sandbox that was orphaned long ago and then recovered would be
    reaped the moment it is orphaned again -- the deadline has to belong to the
    outage, not to the sandbox.
    """
    from datetime import timedelta

    registry = SandboxRegistry(_settings(orphan_record_ttl_s=60))
    record = _create(registry)
    record.node_id = "node_a"
    registry.save(record)
    registry.mark_orphaned("node_a")
    old = registry.get(record.sandbox_id)
    old.orphaned_at = utcnow() - timedelta(seconds=600)
    registry.save(old)

    registry.recover_node(
        "node_a",
        {record.sandbox_id},
        snapshot_ids={record.sandbox_id},
        timeout=60,
    )
    recovered = registry.get(record.sandbox_id)
    assert recovered.state == "running"
    assert recovered.orphaned_at is None

    registry.mark_orphaned("node_a")  # the node drops out again
    again = registry.get(record.sandbox_id)
    again.end_at = utcnow() - timedelta(seconds=10)
    registry.save(again)
    assert registry.remove_expired() == []  # the new grace has just started


def test_the_shared_workspace_budget_refuses_and_says_so(workspace):
    """N25/L1: the fleet-wide disk ledger is a real gate with a real reason.

    Two workers sharing one workspace each carry their own `E2B_NODE_DISK_MB`
    budget, so only `E2B_MAX_TOTAL_DISK_MB` says how much the *deployment* has
    sold off that slice. When it is what refuses a create, the caller must not
    be told "No resources available" -- that sends them looking at memory
    instead of at the workspace their own sandboxes filled.
    """
    registry = SandboxRegistry(
        # 1.5 sandboxes' worth: the first fits, the second cannot.
        _settings(max_total_disk_mb=1536, default_disk_mb=1024)
    )
    _create(registry)
    assert registry.global_reserved()["disk"] == 1024

    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry)
    assert str(exc.value) == (
        "shared workspace disk budget exhausted: 1024 MiB reserved of 1536 MiB"
    )

    # Without the budget (the historical default) the same create goes through.
    unbounded = SandboxRegistry(_settings(default_disk_mb=1024))
    _create(unbounded)
    _create(unbounded)
    assert unbounded.global_reserved()["disk"] == 2048


def test_a_non_disk_refusal_keeps_the_historical_message(workspace):
    """The E9.3/E9.4 retry paths still see the message they were written for."""
    registry = SandboxRegistry(
        _settings(max_total_memory_mb=1024, default_memory_mb=1024)
    )
    _create(registry)

    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry)
    assert str(exc.value) == "No resources available"


def test_the_shared_store_budget_refuses_and_says_so(workspace):
    """The Redis-backed ledger explains a refusal exactly like the in-memory one.

    Multi-replica deployments keep the fleet ledger in the shared store, so
    the store's ``reserve`` only ever answers "no". That branch used to raise
    the historical message unconditionally, which meant the readable reason
    added for the in-memory path never reached the clients of the deployments
    that actually run more than one replica.
    """
    fakeredis = pytest.importorskip("fakeredis")
    settings = _settings(max_total_disk_mb=1536, default_disk_mb=1024)
    registry = SandboxRegistry(
        settings, redis_client=fakeredis.FakeStrictRedis()
    )
    _create(registry)
    assert registry.global_reserved()["disk"] == 1024

    with pytest.raises(ResourceUnavailableError) as exc:
        _create(registry)
    assert str(exc.value) == (
        "shared workspace disk budget exhausted: 1024 MiB reserved of 1536 MiB"
    )

    # A non-disk dimension keeps the historical wording through the store too.
    memory_capped = SandboxRegistry(
        _settings(max_total_memory_mb=1024, default_memory_mb=1024),
        redis_client=fakeredis.FakeStrictRedis(),
    )
    _create(memory_capped)
    with pytest.raises(ResourceUnavailableError) as exc:
        _create(memory_capped)
    assert str(exc.value) == "No resources available"


def test_mark_orphaned_skips_ttl_and_recovers(workspace):
    """E6.1: sandboxes on a lost node are marked orphaned; TTL never reaps
    them while orphaned; recovery flips them back to running."""
    from datetime import timedelta

    registry = SandboxRegistry(_settings())
    rec_a = _create(registry)
    rec_a.node_id = "node_a"
    registry.save(rec_a)

    assert registry.mark_orphaned("node_a") == [rec_a]
    assert registry.get(rec_a.sandbox_id).state == "orphaned"
    assert registry.mark_orphaned("node_a") == [rec_a]  # idempotent

    rec_a.end_at = utcnow() - timedelta(seconds=10)
    assert registry.remove_expired() == []  # orphaned records are protected

    result = registry.recover_node(
        "node_a",
        {rec_a.sandbox_id},
        snapshot_ids={rec_a.sandbox_id},
        timeout=60,
    )
    assert result == {"recovered": [rec_a.sandbox_id], "removed": [], "kept": []}
    record = registry.get(rec_a.sandbox_id)
    assert record.state == "running"
    assert record.end_at > utcnow()  # refreshed on recovery
    record.end_at = utcnow() - timedelta(seconds=10)
    assert registry.remove_expired() == [record]  # expiry applies again


def test_recover_node_removes_stale_records_only(workspace):
    """E6.1: the worker's local runtime list is authoritative — records it no
    longer has are deleted; other nodes' records are untouched."""
    registry = SandboxRegistry(_settings())
    rec_keep = _create(registry)
    rec_keep.node_id = "node_a"
    registry.save(rec_keep)
    rec_gone = _create(registry)
    rec_gone.node_id = "node_a"
    registry.save(rec_gone)
    rec_other = _create(registry)
    rec_other.node_id = "node_b"
    registry.save(rec_other)
    registry.mark_orphaned("node_a")

    result = registry.recover_node(
        "node_a",
        {rec_keep.sandbox_id},
        snapshot_ids={rec_keep.sandbox_id, rec_gone.sandbox_id},
        timeout=60,
    )
    assert result["recovered"] == [rec_keep.sandbox_id]
    assert result["removed"] == [rec_gone.sandbox_id]
    assert registry.get(rec_keep.sandbox_id).state == "running"
    with pytest.raises(UnknownSandboxError):
        registry.get(rec_gone.sandbox_id)
    assert registry.get(rec_other.sandbox_id).node_id == "node_b"
    assert registry.list_by_node("node_b") == [rec_other]


def test_recover_node_protects_records_created_after_snapshot(workspace):
    """E6.1 race: a sandbox created/assigned to the node after the worker's
    snapshot was taken is a concurrent create — even if the worker's report
    (computed before the create landed) omits it, the record must survive."""
    registry = SandboxRegistry(_settings())
    rec_keep = _create(registry)
    rec_keep.node_id = "node_a"
    registry.save(rec_keep)
    rec_gone = _create(registry)
    rec_gone.node_id = "node_a"
    registry.save(rec_gone)
    registry.mark_orphaned("node_a")
    # Snapshot taken when only keep/gone existed.
    snapshot_ids = {rec_keep.sandbox_id, rec_gone.sandbox_id}

    # Concurrent create: a brand-new record lands on node_a after the
    # snapshot but before the worker's report is processed.
    rec_new = _create(registry)
    rec_new.node_id = "node_a"
    registry.save(rec_new)

    result = registry.recover_node(
        "node_a",
        {rec_keep.sandbox_id},  # worker never saw rec_new (created after diff)
        snapshot_ids=snapshot_ids,
        timeout=60,
    )
    assert result["recovered"] == [rec_keep.sandbox_id]
    assert result["removed"] == [rec_gone.sandbox_id]
    assert result["kept"] == [rec_new.sandbox_id]
    assert registry.get(rec_keep.sandbox_id).state == "running"
    with pytest.raises(UnknownSandboxError):
        registry.get(rec_gone.sandbox_id)
    # The live concurrent create was not deleted.
    survivor = registry.get(rec_new.sandbox_id)
    assert survivor.state == "running"
    assert survivor.node_id == "node_a"


def test_max_sandboxes_rejected(workspace):
    registry = SandboxRegistry(_settings(max_sandboxes=2))
    _create(registry)
    _create(registry)
    with pytest.raises(ResourceUnavailableError):
        _create(registry)
    # Freeing a slot allows creation again.
    registry.remove_expired()
    first = next(iter(registry._sandboxes.values()))
    registry.delete(first.sandbox_id)
    _create(registry)


def test_connect_refreshes_end_at(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, timeout=300)
    old_end = record.end_at
    registry.connect(record.sandbox_id, timeout=600)
    assert record.end_at > old_end


def test_create_with_explicit_sandbox_id(workspace):
    registry = SandboxRegistry(_settings())
    record = registry.create(
        template_id="base",
        sandbox_id="sbx_clientprovided",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    assert record.sandbox_id == "sbx_clientprovided"
    assert registry.get("sbx_clientprovided") is record


def test_create_rejects_invalid_sandbox_id(workspace):
    registry = SandboxRegistry(_settings())
    with pytest.raises(ValueError):
        registry.create(
            template_id="base",
            sandbox_id="../../etc/passwd",
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
        )


def test_pending_claim_get_release(workspace):
    registry = SandboxRegistry(_settings())
    assert registry.claim_pending("sbx_p", {"node": "n1"}, ttl=30) is True
    assert registry.claim_pending("sbx_p", {"node": "n2"}, ttl=30) is False
    assert registry.get_pending("sbx_p") == {"node": "n1"}
    registry.release_pending("sbx_p")
    assert registry.get_pending("sbx_p") is None
    assert registry.claim_pending("sbx_p", {"node": "n2"}, ttl=30) is True
    registry.release_pending(None)  # no-op


def test_pending_expires(workspace):
    registry = SandboxRegistry(_settings())
    assert registry.claim_pending("sbx_e", {"node": "n1"}, ttl=-1)
    assert registry.get_pending("sbx_e") is None
