"""N25/L2b: the measured-disk gate (worker measures, control plane pauses).

The per-node and fleet ledgers bound what a sandbox was **sold** (its
``diskMB`` at create time); nothing bounded what it actually wrote. These
tests pin both halves of the answer -- the worker's measurement (including
the scan budget that keeps it off the heartbeat's critical path) and the
control plane's pause -- plus the heartbeat contract that carries one to the
other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import (
    SandboxRegistry,
    UnknownSandboxError,
)
from envd_service.runtime.registry import RuntimeRegistry


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
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


def _worker_tree(root, sandbox_id: str, blob_bytes: int) -> str:
    """A sandbox tree with exactly ``blob_bytes`` of regular-file content."""
    tree = root / sandbox_id
    (tree / "workspace").mkdir(parents=True, exist_ok=True)
    (tree / "blob").write_bytes(b"x" * blob_bytes)
    return str(tree)


# -- the worker's measurement ---------------------------------------------


def test_worker_reports_the_bytes_it_wrote(workspace):
    registry = RuntimeRegistry(workspace)
    tree = _worker_tree(workspace, "sbx_disk_a", 4096)
    registry.register(
        sandbox_id="sbx_disk_a",
        access_token="tok",
        workspace_dir=tree,
    )

    # The tree also holds the runtime's own ``sandbox.json``, so the expected
    # number is a walk of the same tree rather than the 4096 the test wrote.
    expected = sum(
        path.stat().st_size for path in Path(tree).rglob("*") if path.is_file()
    )
    assert registry.disk_usage_snapshot() == {"sbx_disk_a": expected}
    assert expected > 4096


def test_a_scan_addresses_every_tree_in_turn(workspace):
    """A budget that fits one tree must not starve the others.

    The worker's scan runs against the heartbeat thread with a wall-clock
    ceiling, so on a worker with more trees than budget it has to rotate --
    otherwise the sandboxes it never reaches are exactly the ones nothing can
    stop.
    """
    registry = RuntimeRegistry(workspace)
    for name in ("sbx_rot_a", "sbx_rot_b", "sbx_rot_c"):
        registry.register(
            sandbox_id=name,
            access_token="tok",
            workspace_dir=_worker_tree(workspace, name, 1024),
        )

    # A zero budget means "only the mandatory first tree" -- the round still
    # advances, so three rounds see all three.
    seen = set()
    for _ in range(3):
        report = registry.disk_usage_snapshot(budget_s=0)
        assert len(report) == 1
        seen.update(report)
    assert seen == {"sbx_rot_a", "sbx_rot_b", "sbx_rot_c"}


def test_no_runtime_means_nothing_to_report(workspace):
    registry = RuntimeRegistry(workspace)
    assert registry.disk_usage_snapshot() == {}


# -- the control plane's pause --------------------------------------------


def test_a_tree_over_budget_is_paused_and_releases_its_slice(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_over")
    assert registry.global_reserved()["disk"] == record.disk_size_mb

    over = record.disk_size_mb * 1024 * 1024 + 1
    paused = registry.enforce_disk_budget({"sbx_over": over})

    assert [r.sandbox_id for r in paused] == ["sbx_over"]
    assert registry.get("sbx_over").state == "paused"
    # E9.2 semantics: a paused sandbox holds no reservation.
    assert registry.global_reserved()["disk"] == 0


def test_a_tree_within_budget_is_left_alone(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_fit")

    assert registry.enforce_disk_budget({"sbx_fit": record.disk_size_mb * 1024 * 1024}) == []
    assert registry.get("sbx_fit").state == "running"
    assert registry.global_reserved()["disk"] == record.disk_size_mb


def test_the_gate_is_idempotent_across_repeated_reports(workspace):
    """Every heartbeat re-sends the last scan; the pause must land once.

    The worker's report is cached between scans, so the control plane sees the
    same over-budget number on every pulse for up to the scan interval. A
    second pause would append a second "sandbox paused" log line to a record
    that is already frozen.
    """
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_twice")
    over = record.disk_size_mb * 1024 * 1024 + 1

    assert len(registry.enforce_disk_budget({"sbx_twice": over})) == 1
    assert registry.enforce_disk_budget({"sbx_twice": over}) == []
    lines = [entry["line"] for entry in registry.get("sbx_twice").logs]
    assert lines.count("sandbox paused") == 1


def test_a_foreign_or_malformed_report_is_ignored(workspace):
    """A worker heartbeat is untrusted input: it names its own sandboxes."""
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_mine")

    assert registry.enforce_disk_budget({"sbx_elsewhere": 10**12}) == []
    assert registry.enforce_disk_budget({"sbx_mine": "not-a-number"}) == []
    assert registry.get("sbx_mine").state == "running"
    with pytest.raises(UnknownSandboxError):
        registry.get("sbx_elsewhere")
