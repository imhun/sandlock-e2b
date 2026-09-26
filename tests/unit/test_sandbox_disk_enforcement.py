"""N25/L2b: the measured-disk gate (worker measures, both sides act).

The per-node and fleet ledgers bound what a sandbox was **sold** (its
``diskMB`` at create time); nothing bounded what it actually wrote. These
tests pin both halves of the answer -- the worker's measurement (including the
scan budget that keeps it off the heartbeat's critical path) and the control
plane's reaction, which is to *record*, not to freeze: over budget means the
writes stop (the worker's zero file-size ceiling, plus the mediator's `ENOSPC`
for the entry-creating calls a ceiling cannot reach) while the sandbox keeps
running, so its owner can still delete its way back inside.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import Settings
from control_plane.registry.manager import (
    SandboxRecord,
    SandboxRegistry,
    UnknownSandboxError,
)
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common.timeutil import to_iso_z


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

    # The tree holds *only* what the sandbox wrote: the runtime record lives
    # beside it in ``_runtime/<id>/`` (the platform/workspace split), so the
    # measured number is exactly the user's data -- platform bookkeeping no
    # longer counts against the sandbox's disk budget. Since N31 fix 2 the
    # number is the files **plus each directory's own `st_size`**, so the two
    # parts are asserted separately rather than hidden in one sum.
    entries = list(Path(tree).rglob("*"))
    files = [path for path in entries if path.is_file()]
    dirs = [path for path in entries if path.is_dir()]
    # `rglob("*")` lists what is *under* the tree, so the tree's own entry is
    # added by hand -- it is the root of the walk and costs its own block too.
    root_bytes = Path(tree).stat().st_blocks * 512
    dirs_bytes = sum(path.stat().st_blocks * 512 for path in dirs)
    expected = root_bytes + dirs_bytes + sum(
        path.stat().st_size for path in files
    )
    assert registry.disk_usage_snapshot() == {"sbx_disk_a": expected}
    assert sum(path.stat().st_size for path in files) == 4096
    assert expected == 4096 + root_bytes + dirs_bytes
    assert not (Path(tree) / "sandbox.json").exists()


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


def test_a_tree_over_budget_is_reported_but_never_frozen(workspace):
    """The product semantic: over the limit, writes stop; the sandbox does not.

    Freezing it took away the reads, the exec and -- the one that matters --
    the deletes its owner needs to get back inside. The enforcement lives where
    the writes are (the worker's data plane), so all this path does is record
    the number and name the crossing.
    """
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_over")

    over = record.disk_size_mb * 1024 * 1024 + 1
    reported = registry.enforce_disk_budget({"sbx_over": over})

    assert [r.sandbox_id for r in reported] == ["sbx_over"]
    assert registry.get("sbx_over").state == "running"
    # The measurement is the accounting, and it lands on the record whether or
    # not the sandbox is over (N28/D).
    assert registry.get("sbx_over").workspace_disk_used_bytes == over
    # N30: the *outward* number is that same measurement -- ``diskUsed`` is the
    # stock reading the worker reported (not a walk of a tree this record does
    # not even carry: a remote sandbox has no ``workspace_dir``), and
    # ``diskTotal`` is the ceiling it was sold.
    metrics = registry.get("sbx_over").sample_metric()
    assert metrics["diskUsed"] == over
    assert metrics["diskTotal"] == record.disk_size_mb * 1024 * 1024
    # Nothing was parked: a running sandbox keeps its reservation, so it keeps
    # its place while it deletes its way back inside.
    assert registry.global_reserved()["disk"] == record.disk_size_mb


def test_a_tree_within_budget_is_left_alone(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_fit")

    assert registry.enforce_disk_budget({"sbx_fit": record.disk_size_mb * 1024 * 1024}) == []
    assert registry.get("sbx_fit").state == "running"
    assert registry.global_reserved()["disk"] == record.disk_size_mb


def test_a_repeated_report_records_once_and_never_pauses(workspace):
    """Every heartbeat re-sends the last scan, and the heartbeat is 5 s.

    The record has to keep the number without accumulating anything: no second
    "paused" log line (there is no pause at all any more), and one stored
    measurement, not one per pulse.
    """
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_twice")
    over = record.disk_size_mb * 1024 * 1024 + 1

    assert len(registry.enforce_disk_budget({"sbx_twice": over})) == 1
    assert len(registry.enforce_disk_budget({"sbx_twice": over})) == 1
    assert registry.get("sbx_twice").state == "running"
    assert registry.get("sbx_twice").workspace_disk_used_bytes == over
    assert [entry["line"] for entry in registry.get("sbx_twice").logs] == []


def test_the_pause_reason_survives_the_shared_store(workspace):
    """The reason is durable state, not a log line (N28/D).

    ``to_storage_dict`` carries the record's *durable* fields only -- ``logs``
    is deliberately not among them -- so a reason that lived only in the log
    was gone by the next ``get()``, which under Redis is every read.
    """
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_round")
    record.pause("its workspace grew past its budget (1200 MiB used of 1024 MiB)")

    restored = SandboxRecord.from_storage_dict(record.to_storage_dict())

    assert restored.pause_reason == (
        "its workspace grew past its budget (1200 MiB used of 1024 MiB)"
    )
    # The stored form is the contract: ISO-Z, millisecond precision.
    assert to_iso_z(restored.paused_at) == to_iso_z(record.paused_at)
    restored.resume()
    assert restored.pause_reason is None
    assert restored.paused_at is None


def test_a_caller_initiated_pause_carries_no_reason(workspace):
    registry = SandboxRegistry(_settings())
    record = _create(registry, sandbox_id="sbx_plain")
    registry.pause(record)
    assert record.pause_reason is None
    assert record.paused_at is None
    assert [entry["line"] for entry in record.logs] == ["sandbox paused"]


def test_a_foreign_or_malformed_report_is_ignored(workspace):
    """A worker heartbeat is untrusted input: it names its own sandboxes."""
    registry = SandboxRegistry(_settings())
    _create(registry, sandbox_id="sbx_mine")

    assert registry.enforce_disk_budget({"sbx_elsewhere": 10**12}) == []
    assert registry.enforce_disk_budget({"sbx_mine": "not-a-number"}) == []
    assert registry.get("sbx_mine").state == "running"
    with pytest.raises(UnknownSandboxError):
        registry.get("sbx_elsewhere")
