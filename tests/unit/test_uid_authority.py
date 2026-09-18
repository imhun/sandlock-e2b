"""OBS-9 item 1: the host-uid allocation is authoritative outside the volume.

A per-sandbox host uid is a fleet-level invariant, and it used to be derived
from ``sandbox.json`` inside each sandbox tree -- files that live on the shared
volume, where any root on any mounting node can rewrite them. Forging one is
enough to make two sandboxes share a uid and remove the cross-uid isolation
wall.

The allocation now lives in the registry's shared store; the tree keeps a
cache. These tests pin both halves, including the contrast with the worker's
fallback allocator (which still reads the tree, and is why the fallback is a
fallback).
"""

from __future__ import annotations

import json

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.uid_pool import UidPool, UidPoolError


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=1000,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
        uid_pool_start=10000,
        uid_pool_size=8,
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


def _tree_with_cached_uid(workspace, sandbox_id: str, host_uid: int) -> None:
    """A sandbox tree whose cached ``host_uid`` claims ``host_uid``."""
    tree = workspace / sandbox_id
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "sandbox.json").write_text(
        json.dumps({"sandbox_id": sandbox_id, "host_uid": host_uid}),
        encoding="utf-8",
    )


# -- the registry is the allocator ----------------------------------------


def test_allocations_are_distinct_and_released_with_the_record(workspace):
    registry = SandboxRegistry(_settings())
    first = _create(registry, sandbox_id="sbx_uid_a")
    second = _create(registry, sandbox_id="sbx_uid_b")

    a = registry.allocate_host_uid("sbx_uid_a")
    b = registry.allocate_host_uid("sbx_uid_b")
    assert (a, b) == (10000, 10001)
    assert first.host_uid is None  # allocate_host_uid is the caller's job
    assert second.host_uid is None

    # Removing the record returns the uid: an eviction, a TTL reap and every
    # rollback funnel through ``_release``.
    registry.delete("sbx_uid_a")
    assert registry.allocate_host_uid("sbx_uid_c") == 10000


def test_a_forged_tree_cannot_claim_a_uid(workspace):
    """The OBS-9 chain: a writable file must not steer the allocation.

    ``sbx_victim``'s tree claims 10000 *and* 10001 is the next free uid; a
    forged record claiming 10001 must not stop the registry from handing 10001
    to the next sandbox, because the registry never reads those files.
    """
    registry = SandboxRegistry(_settings())
    assert registry.allocate_host_uid("sbx_first") == 10000
    _tree_with_cached_uid(workspace, "sbx_attacker", 10001)
    _tree_with_cached_uid(workspace, "sbx_first", 10000)

    assert registry.allocate_host_uid("sbx_second") == 10001

    # Contrast, and the reason the fallback path is only a fallback: the
    # worker's own allocator still reads the tree, so on its own it *would*
    # have skipped the forged uid.
    worker_pool = RuntimeRegistry(workspace).uid_pool = UidPool(
        start=10000, size=8, workspace_base=workspace
    )
    assert worker_pool.acquire("sbx_second") == 10002


def test_pool_exhaustion_is_reported(workspace):
    registry = SandboxRegistry(_settings(uid_pool_size=1))
    assert registry.allocate_host_uid("sbx_only") == 10000
    assert registry.allocate_host_uid("sbx_next") is None
    # Re-asking for the same sandbox is idempotent, not a second claim.
    assert registry.allocate_host_uid("sbx_only") == 10000


def test_two_replicas_share_one_ledger(workspace):
    client = fakeredis.FakeStrictRedis()
    settings = _settings()
    replica_a = SandboxRegistry(settings, redis_client=client)
    replica_b = SandboxRegistry(settings, redis_client=client)

    assert replica_a.allocate_host_uid("sbx_ra") == 10000
    assert replica_b.allocate_host_uid("sbx_rb") == 10001
    # A release on one replica is visible on the other.
    replica_b.release_host_uid("sbx_ra")
    assert replica_a.allocate_host_uid("sbx_rc") == 10000


def test_host_uid_survives_a_record_round_trip(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_rt")
    record.host_uid = registry.allocate_host_uid("sbx_rt")
    restored = type(record).from_storage_dict(record.to_storage_dict())
    assert restored.host_uid == record.host_uid


# -- the worker applies what it is given ----------------------------------


def test_worker_claims_the_allocated_uid_and_blocks_the_fallback(workspace):
    pool = UidPool(start=10000, size=8, workspace_base=workspace)
    assert pool.claim("sbx_claimed", 10003) == 10003
    # The fallback allocator must not hand the same uid to someone else.
    assert pool.acquire("sbx_other") == 10000
    assert pool.acquire("sbx_third") == 10001


def test_worker_rejects_a_uid_outside_its_pool_range(workspace):
    """A control plane and a worker that disagree about the range must fail
    loudly: silently applying it would put two sandboxes on one uid."""
    pool = UidPool(start=10000, size=8, workspace_base=workspace)
    with pytest.raises(UidPoolError, match="outside this worker's pool"):
        pool.claim("sbx_bad", 20000)
