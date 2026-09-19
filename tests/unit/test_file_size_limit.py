"""N28/C: the single-file ceiling (RLIMIT_FSIZE) the platform asks the fork for.

Two layers, and the split is the contract:

* the *number* is the control plane's own judgment about what the sandbox was
  sold (``max_file_size_mb``), and its whole job is to never refuse a write the
  sandbox was allowed to make;
* the *wiring* is the executor's, so the ceiling actually reaches the fork.
"""

from __future__ import annotations

import pytest

from envd_service.executors.sandlock import SandlockExecutor
from envd_service.runtime.registry import (
    RuntimeSandbox,
    max_file_size_mb,
    state_clause,
)

MIB = 1024 * 1024


def _record(*, disk_mb: int = 1024, volumes: list[dict] | None = None):
    return RuntimeSandbox(
        sandbox_id="sbx_fs",
        access_token="t",
        workspace_dir="/var/lib/e2b-sandboxes/sbx_fs",
        disk_mb=disk_mb,
        volume_mounts=list(volumes or []),
    )


def _mount(quota_mb: int) -> dict:
    return {
        "path": "mnt/data",
        "hostPath": "/vol/vol_1/sbx_fs",
        "perSandboxQuotaMb": quota_mb,
    }


# -- the number -------------------------------------------------------------


def test_no_volumes_the_tree_budget_is_the_ceiling():
    assert max_file_size_mb(_record(disk_mb=1024)) == 1024


def test_a_bigger_volume_slice_raises_the_ceiling():
    """A file legal in the volume must not be refused by the tree's budget."""
    assert max_file_size_mb(_record(disk_mb=1024, volumes=[_mount(4096)])) == 4096


def test_a_smaller_volume_slice_does_not_lower_the_ceiling():
    assert max_file_size_mb(_record(disk_mb=2048, volumes=[_mount(512)])) == 2048


def test_an_unlimited_volume_slice_removes_the_ceiling():
    """0 = unlimited (``build_volume_mounts``): there is no honest number."""
    assert max_file_size_mb(_record(disk_mb=1024, volumes=[_mount(0)])) is None


def test_an_unbudgeted_tree_removes_the_ceiling():
    assert max_file_size_mb(_record(disk_mb=0)) is None


def test_a_record_written_before_volume_quotas_existed_removes_the_ceiling():
    """Unknown is not zero: a rolling upgrade must not refuse legal writes."""
    legacy = {"path": "mnt/data", "hostPath": "/vol/vol_1/sbx_fs"}
    assert max_file_size_mb(_record(disk_mb=1024, volumes=[legacy])) is None


# -- the wiring -------------------------------------------------------------


def _executor(**kwargs) -> SandlockExecutor:
    base = dict(
        workspace_dir="/var/lib/e2b-sandboxes/sbx_fs",
        base_image=None,
        image_rootfs=None,
        memory_mb=1024,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=1024,
        allow_internet_access=False,
        enable_network=False,
    )
    base.update(kwargs)
    return SandlockExecutor(**base)


def test_the_ceiling_reaches_the_instance_policy_as_bytes():
    """The fork's builder takes bytes; the setting is in MiB."""
    ceiling = _executor(max_file_size_mb=2048)._policy_ceiling()
    assert ceiling["max_file_size"] == 2048 * MIB


def test_no_ceiling_means_no_field_rather_than_zero():
    """A zero RLIMIT_FSIZE refuses every write, including a shell's temp file."""
    assert _executor()._policy_ceiling()["max_file_size"] is None
    assert _executor()._max_file_size_bytes() is None


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_setting_is_treated_as_unset(value):
    assert _executor(max_file_size_mb=value)._max_file_size_bytes() is None


# -- the per-exec ceiling (N25/C) -------------------------------------------


class _FakeRegistry:
    """Stands in for the worker registry the context asks for a fresh number."""

    def __init__(self, used: int | None) -> None:
        self.used = used
        self.calls = 0

    def refresh_disk_usage(self, sandbox_id: str) -> int | None:
        self.calls += 1
        return self.used


def _settings(**overrides):
    from types import SimpleNamespace

    base = {
        "disk_exec_limit": True,
        "disk_exec_limit_floor_mb": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(*, used: int | None, disk_mb: int = 1024, volumes=None, settings=None, registry=True):
    from envd_service.runtime.context import SandboxRuntimeContext

    record = _record(disk_mb=disk_mb, volumes=volumes)
    context = SandboxRuntimeContext.__new__(SandboxRuntimeContext)
    context.record = record
    context.settings = settings or _settings()
    context.runtime_registry = _FakeRegistry(used) if registry else None
    return context


def test_the_exec_ceiling_is_what_is_left():
    assert _context(used=700 * MIB).max_file_size_for_exec() == 324 * MIB


def test_the_exec_ceiling_refreshes_before_each_command():
    """The point of the whole feature: never use a value from the last scan."""
    registry = _FakeRegistry(700 * MIB)
    context = _context(used=700 * MIB)
    context.runtime_registry = registry
    context.max_file_size_for_exec()
    context.max_file_size_for_exec()
    assert registry.calls == 2


def test_the_floor_keeps_an_over_budget_sandbox_usable():
    """It must still be able to run a command that deletes something."""
    assert _context(used=2000 * MIB).max_file_size_for_exec() == 1 * MIB


def test_a_volume_slice_only_ever_widens_the_ceiling():
    """A slice is a budget of its own, and its usage is not in this ledger."""
    assert (
        _context(used=1000 * MIB, volumes=[_mount(4096)]).max_file_size_for_exec()
        == 4096 * MIB
    )


def test_without_a_fresh_number_there_is_no_per_exec_change():
    """`None` means the instance ceiling applies -- never a guess."""
    assert _context(used=None).max_file_size_for_exec() is None
    assert _context(used=0, registry=False).max_file_size_for_exec() is None


def test_the_feature_is_off_unless_it_is_switched_on():
    assert (
        _context(used=0, settings=_settings(disk_exec_limit=False))
        .max_file_size_for_exec()
        is None
    )


def test_an_unbudgeted_tree_has_no_exec_ceiling():
    assert _context(used=1, disk_mb=0).max_file_size_for_exec() is None


# -- the refusal text -------------------------------------------------------


def test_state_clause_is_bare_without_a_reason():
    assert state_clause(_record()) == "Sandbox is running"


def test_state_clause_names_a_platform_pause():
    record = _record()
    record.state = "paused"
    record.pause_reason = (
        "its workspace grew past its budget (1340 MiB used of 1024 MiB)"
    )
    assert state_clause(record) == (
        "Sandbox is paused: its workspace grew past its budget "
        "(1340 MiB used of 1024 MiB)"
    )
