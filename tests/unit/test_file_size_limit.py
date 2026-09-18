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
