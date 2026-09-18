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
