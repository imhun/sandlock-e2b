"""Fixtures the unit lane shares.

``registry`` and ``make_record`` live here rather than in the two files that
use them because the N30 ledger cases in ``test_pause_quota.py`` and
``test_tenant_quota.py`` assert the *same* property of the same object -- the
reservation rows equal the live records' own numbers -- and a per-file copy is
the copy that drifts the first time one of them grows a dimension.

The ``registry`` fixture is deliberately the *unbounded* shape: no pool can
refuse, so nothing it provides can decide an assertion. A case that needs a
refusal to be possible builds its own bounded registry and says so.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRecord, SandboxRegistry


def ledger_settings(**overrides) -> Settings:
    """Settings for a registry whose only subject is the ledger.

    Realistic per-record defaults (a create still books memory, cpu and
    processes), every *pool* unbounded -- ``0`` is "no ceiling" throughout the
    manager -- so the disk rows are the only thing a case can be surprised by.
    """
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=0,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=0,
        default_max_processes=64,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture()
def registry() -> SandboxRegistry:
    return SandboxRegistry(ledger_settings())


@pytest.fixture()
def make_record() -> Callable[..., SandboxRecord]:
    """Factory for a live record holding exactly ``disk_size_mb`` of disk.

    ``SandboxRegistry.create`` sells whatever ``settings.default_disk_mb``
    says, so a case that needs two different budgets in one ledger has to write
    the sale itself. The write goes through the same release/hold pair the
    pause/resume path uses -- never a poke at the counters -- so the helper
    cannot leave a ledger that admission could not have produced, and the
    property under test (release gives the row back, hold takes it again) stays
    the only thing the cases assert.
    """

    def _make(
        registry: SandboxRegistry,
        *,
        disk_size_mb: int,
        sandbox_id: str | None = None,
        **overrides,
    ) -> SandboxRecord:
        kwargs = dict(
            template_id="base",
            timeout=300,
            metadata={},
            env_vars={},
            secure=True,
            allow_internet_access=False,
            base_image=None,
            sandbox_id=sandbox_id,
        )
        kwargs.update(overrides)
        record = registry.create(**kwargs)
        if record.disk_size_mb == disk_size_mb:
            return record
        assert registry.release_quota(record) is True, (
            "the sale helper re-books through release/hold, so a fresh record "
            f"must be holding a reservation ({record.sandbox_id})"
        )
        record.disk_size_mb = disk_size_mb
        assert registry.hold_quota(record) is True, (
            f"an unbounded-pool registry must be able to hold "
            f"{disk_size_mb} MiB for {record.sandbox_id}"
        )
        return registry.save(record)

    return _make
