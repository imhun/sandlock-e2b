"""N83 phase 2 / Task 9: a worker's totals come from its container's limits.

The user's ruling (2026-10-07): "worker 的容量应该取容器的 limit 吧，不应该按
配置的 node 变量，可能不准". The worker reports ``totalMemoryMB`` /
``totalCPUPercent`` / ``totalProcesses`` / ``totalDiskMB`` on every
register/heartbeat, and those four numbers are what the *node ledger*
(``NodeRecord.blocking_dimension``) admits against. Until this task they were
``E2B_NODE_*`` with a **host-level** fallback -- inside a k8s pod that is the
whole node's memory and core count, which is exactly the inaccuracy the ruling
points at. The container's own reading already exists for the per-sandbox
ceiling (``read_kernel_ceiling``: ``cpu.max`` / ``memory.max`` / ``pids.max``,
Phase 2 Task 1) and it is the same fact, so it is the same source here.

One rule per dimension, and they are deliberately not the same rule:

* **memory / processes take the smaller of the two** -- kernel first, env
  second, today's host probing only when neither names a number. Selling more
  than the container has is not a local mistake: every sandbox's own
  ``memory.max`` is compliant while the *container* is the one the kernel
  OOM-kills, and that kill takes every neighbour in this worker with it;
* **CPU is the env's to sell, above the kernel if the deployment says so** --
  the user's ruling is explicit ("cpu 可以超卖"): CPU is a share, so contention
  is the entire cost. The kernel stays the final throttle, and the oversell is
  *loud*: one named WARN plus both numbers on the record (``total_cpu_percent``
  is what is sold, ``kernelCeiling.cpuPercent`` the physical fact);
* **disk is untouched** -- there is no kernel reading for it (Task 9 brief:
  "磁盘 | 无内核口径，维持 ``E2B_NODE_DISK_MB``（或文件系统）").

The lane switch is the one the plan pinned: ``E2B_SANDBOX_CGROUP=off`` is
**byte-for-byte** today's worker, so a worker that builds no sandbox cgroup
does not read one for its own node totals either.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from envd_service.agent import (
    CPU_OVERSELL_WARNING,
    _container_kernel_limits,
    _node_resources,
    _register_payload,
    _reset_cpu_oversell_warning,
    kernel_ceiling_payload,
)
from envd_service.config import Settings

LOG = "envd_service.agent"


def _settings(
    tmp_path: Path,
    *,
    cgroup: str = "required",
    kernel: dict[str, str] | None = None,
) -> Settings:
    """An envd ``Settings`` whose cgroup mount is a synthetic kernfs tree."""
    mount = tmp_path / "pod-cgroup"
    mount.mkdir(parents=True, exist_ok=True)
    # ``cpu.max`` and ``memory.max`` are the reading's required pair (the strict
    # reader refuses a mount that carries only one), so a case names the ones it
    # cares about and the rest are the compose lanes' measured ``max``.
    files = {"cpu.max": "max", "memory.max": "max", "pids.max": "max"}
    files.update(kernel or {})
    for name, text in files.items():
        (mount / name).write_text(text)
    return Settings(
        executor="local",
        workspace_base=tmp_path / "workspaces",
        sandbox_cgroup=cgroup,
        cgroup_mount=mount,
    )


def _host_memory_mb() -> int:
    return (
        os.sysconf("SC_PHYS_PAGES")
        * os.sysconf("SC_PAGE_SIZE")
        // (1024 * 1024)
    )


@pytest.fixture(autouse=True)
def _one_shot_warning():
    """The oversell WARN is process-wide and fires once, so reset it per case."""
    _reset_cpu_oversell_warning()
    yield
    _reset_cpu_oversell_warning()


# ------------------------------------------------------------- memory


def test_memory_takes_the_kernel_when_the_kernel_is_the_smaller_one(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("E2B_NODE_MEMORY_MB", "4096")
    settings = _settings(tmp_path, kernel={"memory.max": str(2 * 1024 * 1024 * 1024)})

    assert _node_resources(settings)["totalMemoryMB"] == 2048


def test_memory_falls_back_to_the_env_when_the_kernel_says_max(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("E2B_NODE_MEMORY_MB", "4096")
    settings = _settings(tmp_path, kernel={"memory.max": "max"})

    assert _node_resources(settings)["totalMemoryMB"] == 4096


def test_memory_is_the_env_when_the_env_is_the_smaller_one(
    tmp_path, monkeypatch
) -> None:
    """The other direction of "take the smaller": an operator may sell less."""
    monkeypatch.setenv("E2B_NODE_MEMORY_MB", "1024")
    settings = _settings(tmp_path, kernel={"memory.max": str(8 * 1024 * 1024 * 1024)})

    assert _node_resources(settings)["totalMemoryMB"] == 1024


def test_memory_with_neither_reading_keeps_todays_host_probe(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("E2B_NODE_MEMORY_MB", raising=False)
    settings = _settings(tmp_path, kernel={"memory.max": "max"})

    assert _node_resources(settings)["totalMemoryMB"] == _host_memory_mb()


# ------------------------------------------------------------- processes


def test_processes_take_the_kernel_when_the_kernel_is_the_smaller_one(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("E2B_NODE_PROCESSES", "1024")
    settings = _settings(tmp_path, kernel={"pids.max": "512"})

    assert _node_resources(settings)["totalProcesses"] == 512


def test_processes_stay_env_driven_when_the_kernel_says_max(
    tmp_path, monkeypatch
) -> None:
    """The measured shipped shape: every lane's ``pids.max`` is ``max``.

    Stated plainly rather than dressed up: on k8s and on all three compose
    lanes the process dimension is *still* ``E2B_NODE_PROCESSES`` (or today's
    100x default). The min-rule matters on a deployment that actually sets a
    pod ``pids`` limit; it is not a derivation that fires on the current fleet.
    """
    monkeypatch.setenv("E2B_NODE_PROCESSES", "1024")
    settings = _settings(tmp_path, kernel={"pids.max": "max"})

    assert _node_resources(settings)["totalProcesses"] == 1024


def test_processes_fall_back_to_the_default_when_nothing_names_them(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("E2B_NODE_PROCESSES", raising=False)
    settings = _settings(tmp_path, kernel={"pids.max": "max"})

    assert _node_resources(settings)["totalProcesses"] == (
        settings.default_max_processes * 100
    )


# ------------------------------------------------------------- cpu


def test_cpu_is_the_env_even_above_the_kernel_and_warns_by_name(
    tmp_path, monkeypatch, caplog
) -> None:
    """The user's "cpu 可以超卖": the env sells 800% on a 400% container."""
    monkeypatch.setenv("E2B_NODE_CPU_PERCENT", "800")
    settings = _settings(tmp_path, kernel={"cpu.max": "400000 100000"})

    with caplog.at_level(logging.WARNING, logger=LOG):
        resources = _node_resources(settings)

    assert resources["totalCPUPercent"] == 800
    # Both numbers ride the registration: what is sold, and the physical fact.
    payload = _register_payload(settings, node_id="worker-1")
    assert payload["totalCPUPercent"] == 800
    assert payload["kernelCeiling"]["cpuPercent"] == 400
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == LOG
    ] == [CPU_OVERSELL_WARNING % (800, 400)]


def test_cpu_oversell_warns_once_per_change_not_once_per_heartbeat(
    tmp_path, monkeypatch, caplog
) -> None:
    """A 5 s heartbeat would otherwise train an operator to ignore the line."""
    monkeypatch.setenv("E2B_NODE_CPU_PERCENT", "800")
    settings = _settings(tmp_path, kernel={"cpu.max": "400000 100000"})

    with caplog.at_level(logging.WARNING, logger=LOG):
        for _ in range(3):
            _node_resources(settings)

    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == LOG
    ] == [CPU_OVERSELL_WARNING % (800, 400)]


def test_cpu_uses_the_kernel_when_the_env_names_none(tmp_path, monkeypatch) -> None:
    """Unset env is no longer "every core this host reports".

    In a k8s pod ``os.cpu_count()`` is the *node's* cores; the container's own
    ``cpu.max`` is the number this node may actually use.
    """
    monkeypatch.delenv("E2B_NODE_CPU_PERCENT", raising=False)
    settings = _settings(tmp_path, kernel={"cpu.max": "400000 100000"})

    assert _node_resources(settings)["totalCPUPercent"] == 400


def test_cpu_sells_less_than_the_kernel_when_the_env_says_so(
    tmp_path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("E2B_NODE_CPU_PERCENT", "200")
    settings = _settings(tmp_path, kernel={"cpu.max": "400000 100000"})

    with caplog.at_level(logging.WARNING, logger=LOG):
        resources = _node_resources(settings)

    assert resources["totalCPUPercent"] == 200
    assert [r for r in caplog.records if r.name == LOG] == []


def test_cpu_with_no_kernel_reading_keeps_todays_host_probe(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("E2B_NODE_CPU_PERCENT", raising=False)
    settings = _settings(tmp_path, kernel={"cpu.max": "max"})

    assert _node_resources(settings)["totalCPUPercent"] == (
        os.cpu_count() * 100 or 100
    )


# ------------------------------------------------------------- disk


def test_disk_stays_env_driven(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("E2B_NODE_DISK_MB", "4096")
    settings = _settings(
        tmp_path,
        kernel={
            "cpu.max": "400000 100000",
            "memory.max": "1073741824",
            "pids.max": "64",
        },
    )

    assert _node_resources(settings)["totalDiskMB"] == 4096


# ------------------------------------------------------------- the switch


def test_the_off_lane_never_reads_the_kernel(tmp_path, monkeypatch) -> None:
    """``E2B_SANDBOX_CGROUP=off`` is byte-for-byte today's worker.

    The mount is *there* and carries limits that would change every number; a
    worker that builds no sandbox cgroup has no cgroup business of its own, so
    its node totals stay exactly what they were.
    """
    monkeypatch.setenv("E2B_NODE_MEMORY_MB", "4096")
    monkeypatch.setenv("E2B_NODE_CPU_PERCENT", "200")
    monkeypatch.setenv("E2B_NODE_PROCESSES", "1024")
    settings = _settings(
        tmp_path,
        cgroup="off",
        kernel={
            "cpu.max": "800000 100000",
            "memory.max": "1073741824",
            "pids.max": "64",
        },
    )

    assert _node_resources(settings) == {
        "totalMemoryMB": 4096,
        "totalCPUPercent": 200,
        "totalDiskMB": _node_resources(settings)["totalDiskMB"],
        "totalProcesses": 1024,
    }


def test_a_missing_mount_leaves_the_env_in_charge(tmp_path, monkeypatch) -> None:
    """A heartbeat must not be lost over a cgroup file (best effort, D5b's rule)."""
    monkeypatch.setenv("E2B_NODE_MEMORY_MB", "4096")
    monkeypatch.setenv("E2B_NODE_CPU_PERCENT", "200")
    settings = Settings(
        executor="local",
        workspace_base=tmp_path / "workspaces",
        sandbox_cgroup="required",
        cgroup_mount=tmp_path / "not-mounted",
    )

    resources = _node_resources(settings)
    assert resources["totalMemoryMB"] == 4096
    assert resources["totalCPUPercent"] == 200


@pytest.mark.parametrize("cgroup", ("off", "required"))
def test_the_heartbeats_kernel_pair_is_not_gated_on_the_lane_switch(
    tmp_path, cgroup
) -> None:
    """The *other* kernel reader is ungated, and the record's pair proves it.

    ``_container_kernel_limits`` -- the reader one section up, and the one the
    switch **is** about -- answers ``None`` on the ``off`` lane. The heartbeat's
    ``kernelCeiling`` is a different reader (``kernel_ceiling_payload``) and it
    does not consult the switch at all: whenever ``E2B_CGROUP_MOUNT`` is there,
    it reads it. That asymmetry is the shipped shape -- ``deploy/k8s/worker.yaml``
    sets ``E2B_CGROUP_MOUNT=/pod-cgroup`` and mounts it, and sets no switch, so
    the default lane is ``off`` + a readable mount and its node record still
    carries the physical numbers.

    Task 13's review caught the first version of §2.4.8 listing the switch as a
    source of the record's ``null``s; this pair is what says otherwise, and it
    turns red if the payload is ever gated the way the totals reader is.
    """
    settings = _settings(
        tmp_path,
        cgroup=cgroup,
        kernel={"cpu.max": "400000 100000", "memory.max": "4294967296"},
    )

    assert kernel_ceiling_payload(settings) == {"cpuPercent": 400, "memoryMB": 4096}
    # ...and the totals reader is the one the switch speaks to.
    assert (_container_kernel_limits(settings) is None) is (cgroup == "off")


def test_the_heartbeats_pair_is_null_when_the_mount_cannot_be_read(tmp_path) -> None:
    """"No reading this beat" comes from the mount, never from the lane switch.

    This is the record's *other* ``null`` source (besides a kernel that reports
    ``max``): ``/pod-cgroup`` is not there -- the macOS dev box, or a lane
    without the mount -- and the pair reads ``null`` while the switch is
    ``required``, which is the one value that could not have caused it.
    """
    settings = Settings(
        executor="local",
        workspace_base=tmp_path / "workspaces",
        sandbox_cgroup="required",
        cgroup_mount=tmp_path / "not-mounted",
    )

    assert kernel_ceiling_payload(settings) == {"cpuPercent": None, "memoryMB": None}
