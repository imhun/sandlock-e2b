"""N83 phase 1/2: the worker's per-sandbox cgroup primitives (form W).

These cases run against a *synthetic* cgroupfs under ``tmp_path`` plus an
injected ``proc_root``: the module only ever touches the paths it is handed, so
a plain directory tree carrying the kernfs file names is enough to pin its
observable behaviour. Two kernel facts a plain tree cannot reproduce are faked,
and only those two:

* **ownership** -- the delegated directory is the one whose ``st_uid`` equals
  the worker's uid, so a case picks ``worker_uid = os.getuid()`` for the
  positive shape and an uid nothing is owned by for the "not yet delegated"
  shape. ``chown`` is not available in the unit lane, and it does not need to
  be: the real check is ``st_uid == worker_uid`` and the fixture controls which
  uid that is.
* **placement** -- the kernel moves a pid when it is written into a child's
  ``cgroup.procs``; the autouse ``kernel_placement`` fixture installs that same
  rule on top of the real file reader, so the parent's readback empties after
  the drain (``brief``: "inject a way for tests to fake placement").

Everything else -- the cheap precheck, the bounded wait for exactly one
delegated directory, the drain readback, ``cpu.max``, the ``/proc`` placement
check, the idempotent release -- is the real code path.

N83 phase 2 (Task 1) adds the *ceiling* half. Its **owner** is the control
plane (ruling R17, 2026-10-07): the per-sandbox policy is its own
``E2B_MAX_SANDBOX_*``, resolved per node when it is stamped (explicit value >
that node's own reported total > the create default -- never 0), and handed
**down** in every register/heartbeat answer. The worker no longer reads those
envs at all; it *adopts* the hand-down
(``envd_service.agent.adopt_sandbox_ceiling``), and before one arrives it has
no ceiling -- which means no create, never an unbounded run (the kernel read of
its own container cgroup, ``cpu.max``/``memory.max``, is cross-checked against
the hand-down **at adoption time**, D5b, not at worker startup). The cases at
the bottom of this file pin the three mistakes Review Focus §1 names: a
hand-down that never arrived read as "unlimited", a policy ceiling above the
kernel's, and a kernel that sets no ceiling at all (the compose lane's measured
shape).

N83 phase 2 (Task 3) adds the *writing* half: ``setup()`` enables ``memory`` and
``pids`` beside ``cpu`` (same drain, same EBUSY rule), and ``attach()`` writes
``memory.high``/``memory.max``/``pids.max`` beside ``cpu.max`` -- each one read
back **verbatim** -- with a second, worker-side defense gate (R3) that refuses a
declared size above the worker's own ``E2B_MAX_SANDBOX_*`` ceiling by name.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable

import pytest

from envd_service import agent as node_agent
from envd_service.config import Settings
from envd_service.runtime import sandbox_cgroup
from envd_service.runtime.sandbox_cgroup import (
    CgroupRefusal,
    SandboxCeiling,
    SandboxCgroups,
    check_policy_ceiling,
    cpu_max_for,
    memory_max_for,
    pids_max_for,
)
from envd_service.agent import start_cgroup_lane

#: The pid the synthetic cgroup.procs files carry. The module must see its own
#: pid there (``self-placement``), so the tests use the process's real pid.
SELF_PID = os.getpid()

#: The worker's own per-sandbox ceiling (``E2B_MAX_SANDBOX_*``) for the cases
#: that build a handle by hand: generous enough that a declared size below it is
#: the interesting shape. The R3 defense gate compares against *this*, never
#: against the kernel (plan ruling R3).
POLICY_CEILING = SandboxCeiling(cpu_percent=400, memory_mb=4096, processes=1024)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _delegated_dir(mount: Path, name: str) -> Path:
    """A container cgroup the agent has chowned to the worker (Task 3/4)."""
    cg = mount / name
    cg.mkdir(parents=True)
    _write(cg / "cpu.max", "max 100000")
    _write(cg / "cgroup.procs", f"{SELF_PID}\n")
    _write(cg / "cgroup.subtree_control", "")
    return cg


def _pod_mount(tmp_path: Path, *, children: tuple[str, ...] = ("worker-container",)) -> Path:
    """A k8s-shaped mount: the root *is* the pod cgroup, its children the containers."""
    mount = tmp_path / "pod"
    mount.mkdir()
    _write(mount / "cpu.max", "max 100000")
    _write(mount / "cgroup.procs", "")
    _write(mount / "cgroup.subtree_control", "")
    for name in children:
        _delegated_dir(mount, name)
    return mount


@pytest.fixture(autouse=True)
def kernel_placement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the kernel's pid migration on a synthetic cgroupfs.

    The real reader (``sandbox_cgroup._cgroup_pids``) is the default; this wraps
    it with the one rule a plain tree cannot express -- a pid written into a
    child's ``cgroup.procs`` no longer lives in the parent -- so the drain's
    "parent reads back empty" check is observable.
    """
    real: Callable[[Path], list[int]] = sandbox_cgroup._cgroup_pids

    def placed(cgroup_dir: Path) -> list[int]:
        here = real(cgroup_dir)
        moved: set[int] = set()
        for child in cgroup_dir.iterdir():
            if child.is_dir() and (child / "cgroup.procs").is_file():
                moved.update(real(child))
        return [pid for pid in here if pid not in moved]

    monkeypatch.setattr(sandbox_cgroup, "_cgroup_pids", placed)


@pytest.fixture()
def kernel_ebusy_while_occupied(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel's no-internal-process rule, faked like ``kernel_placement``.

    Enabling a domain controller on a cgroup that still holds tasks is
    ``EBUSY``; after the drain it is accepted. Measured on the local Docker VM
    (plan 现场事实): ``+memory`` with the parent still populated gives errno 16,
    and the same write is ``ok`` once the worker has been moved into
    ``worker/``. Only that one rule is faked -- the module's write, the module's
    readback and the module's refusal translation are the real code path.
    """
    real_write = Path.write_text

    def guarded(self: Path, text: str, *args: object, **kwargs: object) -> int:
        if self.name == "cgroup.subtree_control" and str(text).startswith("+"):
            if sandbox_cgroup._cgroup_pids(self.parent):
                raise OSError(16, "Device or resource busy")
        return real_write(self, text, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", guarded)


def test_setup_refuses_when_mount_root_has_no_cpu_max(tmp_path: Path) -> None:
    # The kubelet-fabricated wrong-QoS directory: no cpu.max at the root.
    mount = tmp_path / "pod"
    (mount / "guaranteed-container").mkdir(parents=True)
    _write(mount / "cgroup.procs", "")

    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.2)
    assert str(excinfo.value) == f"cgroup-refusal precheck: {mount}/cpu.max is missing"


def test_setup_refuses_when_mount_root_has_no_child_directories(tmp_path: Path) -> None:
    mount = tmp_path / "pod"
    mount.mkdir()
    _write(mount / "cpu.max", "max 100000")

    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.2)
    assert str(excinfo.value) == (
        f"cgroup-refusal precheck: {mount} has no child cgroup directories"
    )


def test_setup_refuses_when_no_delegated_dir_appears(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    stranger_uid = os.getuid() + 1000

    cg = SandboxCgroups(mount=mount, worker_uid=stranger_uid)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.05)
    assert str(excinfo.value) == (
        f"cgroup-refusal delegation-timeout: no cgroup directory owned by uid "
        f"{stranger_uid} under {mount}"
    )


def test_setup_refuses_when_two_dirs_are_owned_by_the_worker(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path, children=("a", "b"))
    uid = os.getuid()

    cg = SandboxCgroups(mount=mount, worker_uid=uid)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.05)
    assert str(excinfo.value) == (
        f"cgroup-refusal ambiguous-delegation: 2 cgroup directories owned by uid "
        f"{uid} under {mount}: {mount / 'a'}, {mount / 'b'}"
    )


def test_setup_drains_the_parent_and_enables_every_controller(
    tmp_path: Path, kernel_ebusy_while_occupied: None
) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"

    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    line = cg.setup(wait_s=0.2)

    # Drained: the parent no longer holds the worker, and the worker/ dir does.
    # The faked kernel rule above is what makes that ordering load-bearing: the
    # enable below is EBUSY while the parent still holds tasks (plan 现场事实,
    # measured for +cpu in phase 1 and re-measured for +memory/+pids), so a
    # ``setup`` that did not drain first could not reach this assertion.
    assert sandbox_cgroup._cgroup_pids(parent) == []
    assert (parent / "worker" / "cgroup.procs").read_text() == f"{SELF_PID}\n"
    # All three controllers in one command, then verified by readback (the
    # kernel echoes the enabled set, "cpu memory pids").
    assert (parent / "cgroup.subtree_control").read_text() == "+cpu +memory +pids"
    assert line == (
        f"cgroup ready parent={parent} worker_uid={os.getuid()} "
        f"drained=1 subtree_control=cpu memory pids"
    )


def test_setup_with_container_token_narrows_to_the_container_id(tmp_path: Path) -> None:
    # Compose lane: the mount is the whole VM tree, so the search is narrowed by
    # the container id (= hostname) before the ownership check.
    mount = tmp_path / "vm"
    mount.mkdir()
    _write(mount / "cpu.max", "max 100000")
    token = "3f9ab7c1d2e4"
    delegated = _delegated_dir(mount / "docker", f"docker-{token}.scope")
    # Another cgroup the worker owns, but it is not the container's.
    _delegated_dir(mount / "docker", "docker-someone-else.scope")

    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), container_token=token)

    line = cg.setup(wait_s=0.2)
    assert line == (
        f"cgroup ready parent={delegated} worker_uid={os.getuid()} "
        f"drained=1 subtree_control=cpu memory pids"
    )


def test_attach_writes_cpu_max_and_places_the_pid(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=proc_root,
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    pid = 4242
    _write(proc_root / str(pid) / "cgroup", "0::/sbx_alpha\n")

    target = cg.attach(
        sandbox_id="alpha",
        pid=pid,
        cpu_percent=100,
        memory_mb=512,
        max_processes=64,
    )

    assert target == str(parent / "sbx_alpha")
    assert (parent / "sbx_alpha" / "cpu.max").read_text() == "100000 100000"
    assert (parent / "sbx_alpha" / "cgroup.procs").read_text() == f"{pid}\n"


def test_attach_refuses_a_target_that_already_holds_pids(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    target = parent / "sbx_busy"
    target.mkdir()
    _write(target / "cgroup.procs", "999\n")

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="busy",
            pid=4242,
            cpu_percent=50,
            memory_mb=512,
            max_processes=64,
        )
    assert str(excinfo.value) == (
        f"cgroup-refusal sbx-in-use: {target} already holds pids [999]"
    )
    # A refused reuse must not disturb what is already there.
    assert (target / "cgroup.procs").read_text() == "999\n"


def test_attach_removes_what_it_created_when_placement_check_fails(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=proc_root,
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    pid = 4242
    _write(proc_root / str(pid) / "cgroup", "0::/elsewhere\n")

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="alpha",
            pid=pid,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )
    assert str(excinfo.value) == (
        f"cgroup-refusal placement: pid {pid} is in '0::/elsewhere', "
        f"expected '0::/sbx_alpha'"
    )
    assert (parent / "sbx_alpha").exists() is False


def test_release_is_idempotent(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)

    assert cg.release(sandbox_id="gone") is False

    target = parent / "sbx_alpha"
    target.mkdir()
    _write(target / "cgroup.procs", "4242\n")

    assert cg.release(sandbox_id="alpha") is True
    assert target.exists() is False
    assert cg.release(sandbox_id="alpha") is False


def test_release_refuses_when_rmdir_fails(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    target = parent / "sbx_alpha"
    target.mkdir()
    _write(target / "stray", "still here")

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.release(sandbox_id="alpha")
    assert str(excinfo.value) == f"cgroup-refusal release-rmdir: could not rmdir {target}"
    assert target.exists() is True


def test_cpu_max_for_scales_percent_to_a_100ms_period() -> None:
    assert cpu_max_for(100) == "100000 100000"
    assert cpu_max_for(50) == "50000 100000"
    assert cpu_max_for(1) == "1000 100000"


def test_setup_refuses_when_the_worker_cgroup_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    worker = parent / "worker"
    real_mkdir = Path.mkdir

    def deny(self: Path, *args: object, **kwargs: object) -> None:
        if self == worker:
            raise PermissionError(13, "Permission denied")
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", deny)
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.2)
    assert str(excinfo.value) == f"cgroup-refusal worker-mkdir: {worker}"
    assert worker.exists() is False


def test_setup_cleans_up_the_worker_cgroup_when_a_later_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    control = parent / "cgroup.subtree_control"
    real_write = Path.write_text

    def deny(self: Path, *args: object, **kwargs: object) -> int:
        if self == control:
            raise PermissionError(13, "Permission denied")
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", deny)
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.2)
    # Named refusal, the created worker/ cgroup is gone, and the drained pid is
    # back in the parent.
    assert str(excinfo.value) == f"cgroup-refusal subtree-control-write: {control}"
    assert (parent / "worker").exists() is False
    assert (parent / "cgroup.procs").read_text() == f"{SELF_PID}\n"


def test_attach_refuses_when_the_sandbox_cgroup_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    target = parent / "sbx_alpha"
    real_mkdir = Path.mkdir

    def deny(self: Path, *args: object, **kwargs: object) -> None:
        if self == target:
            raise PermissionError(13, "Permission denied")
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", deny)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="alpha",
            pid=4242,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )
    assert str(excinfo.value) == f"cgroup-refusal sbx-mkdir: {target}"
    assert target.exists() is False


def test_attach_refuses_when_an_existing_target_cannot_be_read(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    # Exists, but has no cgroup.procs to read: the reuse probe fails, and that
    # must be a named refusal, not a bare OSError.
    target = parent / "sbx_alpha"
    target.mkdir()

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="alpha",
            pid=4242,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )
    assert str(excinfo.value) == f"cgroup-refusal attach-io: {target}"
    assert target.exists() is True


def test_release_refuses_when_cgroup_kill_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    target = parent / "sbx_alpha"
    target.mkdir()
    _write(target / "cgroup.kill", "")
    real_write = Path.write_text

    def deny(self: Path, *args: object, **kwargs: object) -> int:
        if self == target / "cgroup.kill":
            raise PermissionError(13, "Permission denied")
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", deny)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.release(sandbox_id="alpha")
    assert str(excinfo.value) == f"cgroup-refusal release-kill: {target}"
    # The kill did not take: the cgroup is still there, not half-removed.
    assert target.exists() is True


@pytest.mark.parametrize("bad_id", ["../evil", "a/b", ""])
def test_attach_refuses_a_path_escaping_sandbox_id(tmp_path: Path, bad_id: str) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    before = sorted(child.name for child in parent.iterdir())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id=bad_id,
            pid=4242,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )

    assert str(excinfo.value) == (
        f"cgroup-refusal sandbox-id: {bad_id!r} is not a valid sandbox id"
    )
    # Nothing was created, inside the delegated subtree or out of it.
    assert sorted(child.name for child in parent.iterdir()) == before
    assert (tmp_path / "evil").exists() is False


def test_release_refuses_a_path_escaping_sandbox_id(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    before = sorted(child.name for child in parent.iterdir())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.release(sandbox_id="a/b")

    assert str(excinfo.value) == (
        "cgroup-refusal sandbox-id: 'a/b' is not a valid sandbox id"
    )
    assert sorted(child.name for child in parent.iterdir()) == before


# ------------------------------ the box's three limits (N83 phase 2, Task 3)
#
# D1: one ``sbx_<id>`` carries ``cpu.max`` *and* ``memory.high``/``memory.max``
# *and* ``pids.max``; D2: the two memory files get the same value (reclaim
# first, kill only if the process cannot come down); D3: ``memory.oom.group``
# is deliberately left alone, so an over-budget sandbox does not take its
# neighbours in the same box (or the parent container) with it. Every write is
# read back verbatim, and the second gate (R3) compares a *declared* size
# against the worker's ``E2B_MAX_SANDBOX_*`` ceiling -- never against a kernel
# read: no clamping, no silently running smaller.


def test_the_limit_helpers_spell_the_kernel_files() -> None:
    assert memory_max_for(512) == "536870912"
    assert memory_max_for(4096) == "4294967296"
    assert pids_max_for(64) == "64"
    assert pids_max_for(1024) == "1024"


def test_attach_writes_all_three_limits_and_reads_them_back_verbatim(
    tmp_path: Path,
) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=proc_root,
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    pid = 4242
    _write(proc_root / str(pid) / "cgroup", "0::/sbx_alpha\n")

    target = Path(
        cg.attach(
            sandbox_id="alpha",
            pid=pid,
            cpu_percent=200,
            memory_mb=512,
            max_processes=64,
        )
    )

    assert target == parent / "sbx_alpha"
    assert (target / "cpu.max").read_text() == "200000 100000"
    # D2: the same line for both -- `memory.high` reclaims (throttles) first,
    # and an allocation that still cannot come down hits `memory.max`.
    assert (target / "memory.high").read_text() == "536870912"
    assert (target / "memory.max").read_text() == "536870912"
    assert (target / "pids.max").read_text() == "64"
    # D3: `memory.oom.group` is not written at all -- the default 0 kills only
    # the allocating task, so a neighbour in the same box (and the parent
    # container) is not dragged down with it.
    assert (target / "memory.oom.group").exists() is False


def test_attach_takes_the_ceiling_for_a_dimension_nobody_declared(
    tmp_path: Path,
) -> None:
    """``None`` is "the caller did not say", and the ceiling is the only number
    that is not invented here: writing what one sandbox may have is a bound,
    while any other value would be a silent policy change."""
    mount = _pod_mount(tmp_path)
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=proc_root,
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    pid = 4242
    _write(proc_root / str(pid) / "cgroup", "0::/sbx_alpha\n")

    target = Path(
        cg.attach(
            sandbox_id="alpha",
            pid=pid,
            cpu_percent=100,
            memory_mb=None,
            max_processes=None,
        )
    )

    assert (target / "memory.high").read_text() == memory_max_for(4096)
    assert (target / "memory.max").read_text() == memory_max_for(4096)
    assert (target / "pids.max").read_text() == pids_max_for(1024)


def test_release_removes_a_box_that_carried_all_three_limits(tmp_path: Path) -> None:
    """The synthetic-tree teardown has to drop the new kernfs files too.

    A real cgroupfs removes them with the directory; the unit lane keeps them,
    so ``_KERNFS_FILES`` is the only reason the second ``rmdir`` succeeds here.
    """
    mount = _pod_mount(tmp_path)
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=proc_root,
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    pid = 4242
    _write(proc_root / str(pid) / "cgroup", "0::/sbx_alpha\n")
    box = Path(
        cg.attach(
            sandbox_id="alpha",
            pid=pid,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )
    )
    assert sorted(child.name for child in box.iterdir()) == [
        "cgroup.procs",
        "cpu.max",
        "memory.high",
        "memory.max",
        "pids.max",
    ]

    assert cg.release(sandbox_id="alpha") is True
    assert box.exists() is False


def test_attach_refuses_a_declared_size_above_the_worker_ceiling(
    tmp_path: Path,
) -> None:
    """R3's second gate compares the declared size with the handed-down ceiling.

    The control plane already refused an oversized request (Task 2); this is
    the worker's own defense -- no clamp, no "run smaller silently", and
    nothing half-built is left behind for a size this worker will not promise.
    The ceiling is the *control plane's* (ruling R17), adopted by the worker and
    pushed into the live handle.
    """
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    before = sorted(child.name for child in parent.iterdir())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="alpha",
            pid=4242,
            cpu_percent=800,
            memory_mb=8192,
            max_processes=2048,
        )

    assert str(excinfo.value) == (
        "cgroup-refusal size-exceeds-ceiling: sandbox alpha declares more than "
        "the per-sandbox ceiling the control plane handed down "
        "(cpuPercent 800 > 400; memoryMB 8192 > 4096; "
        "maxProcesses 2048 > 1024) -- refusing the create rather than running a "
        "smaller sandbox silently; lower the request or raise "
        "E2B_MAX_SANDBOX_* on the control plane"
    )
    # No half-built box, and nothing else in the delegated subtree moved.
    assert sorted(child.name for child in parent.iterdir()) == before
    assert (parent / "sbx_alpha").exists() is False


def test_attach_refuses_when_the_handle_carries_no_ceiling(tmp_path: Path) -> None:
    """Fail closed: with nothing to compare against, nothing is written.

    A handle with no hand-down (built by hand, by an embedder, or by a worker
    whose control plane has not answered yet) cannot answer "is this request
    inside what this node may promise?", so the create is refused by name -- the
    same direction as ``E2B_SANDBOX_CGROUP=required`` without a handle.
    """
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc"
    )
    cg.setup(wait_s=0.2)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="alpha",
            pid=4242,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )

    assert str(excinfo.value) == (
        "cgroup-refusal ceiling-unavailable: this handle carries no per-sandbox "
        "ceiling, so sandbox alpha's declared size cannot be checked against "
        "anything (N83 phase 2 R3/R17): the control plane has not handed one "
        "down to this worker (or the one it handed down was refused), and a "
        "create is never run unbounded on this lane -- set "
        "E2B_SANDBOX_CGROUP=off for a lane that deliberately builds no "
        "per-sandbox cgroup"
    )
    assert (parent / "sbx_alpha").exists() is False


def test_attach_refuses_and_removes_the_box_when_a_limit_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused write is a named refusal, and the box it made is gone."""
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    memory_high = parent / "sbx_alpha" / "memory.high"
    real_write = Path.write_text

    def deny(self: Path, *args: object, **kwargs: object) -> int:
        if self == memory_high:
            raise PermissionError(13, "Permission denied")
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", deny)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="alpha",
            pid=4242,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )

    assert str(excinfo.value) == f"cgroup-refusal memory-high-write: {memory_high}"
    assert (parent / "sbx_alpha").exists() is False


def test_attach_refuses_and_removes_the_box_when_a_limit_reads_back_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that does not read back verbatim is a refusal, not a shrug.

    A parent layer is allowed to round a value it cannot represent; this module
    is not allowed to leave a sandbox running with a limit nobody checked. Each
    file gets its own named refusal, so a log names *which* limit disagreed.
    """
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=tmp_path / "proc",
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    memory_max = parent / "sbx_alpha" / "memory.max"
    real_write = Path.write_text

    def tamper(self: Path, *args: object, **kwargs: object) -> int:
        if self == memory_max:
            # Something between us and the kernel stored a different number:
            # the shape the plan's Review Focus 2 names (a parent layer that
            # silently applies "the smaller one").
            return real_write(self, "1073741824")  # type: ignore[arg-type]
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", tamper)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(
            sandbox_id="alpha",
            pid=4242,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )

    assert str(excinfo.value) == (
        f"cgroup-refusal memory-max: wrote '536870912' to {memory_max}, "
        f"read '1073741824'"
    )
    assert (parent / "sbx_alpha").exists() is False


def test_the_controllers_are_refused_while_the_parent_still_holds_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel_ebusy_while_occupied: None,
) -> None:
    """``+memory``/``+pids`` need the same drain ``+cpu`` needs.

    The fake above is the kernel's own rule; skipping the drain is the shortest
    way to show the rule applies to the *whole* command, not only to phase 1's
    word.
    """
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    control = parent / "cgroup.subtree_control"
    monkeypatch.setattr(
        sandbox_cgroup.SandboxCgroups,
        "_drain_into",
        lambda self, parent_dir, worker_dir: [],
    )
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.2)

    assert str(excinfo.value) == f"cgroup-refusal subtree-control-write: {control}"
    # Nothing half-built: the worker/ cgroup this attempt created is gone.
    assert (parent / "worker").exists() is False


def test_setup_refuses_when_the_kernel_does_not_echo_every_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readback is the proof: one missing word is a named refusal."""
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    control = parent / "cgroup.subtree_control"
    real_write = Path.write_text

    def partial(self: Path, text: str, *args: object, **kwargs: object) -> int:
        if self == control and str(text).startswith("+"):
            # A parent layer (or an older kernel) that accepted only phase 1's
            # word: what it echoes is the enabled set it really has.
            return real_write(self, "cpu")  # type: ignore[arg-type]
        return real_write(self, text, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", partial)
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.setup(wait_s=0.2)

    assert str(excinfo.value) == (
        f"cgroup-refusal subtree-control: wrote '+cpu +memory +pids' to "
        f"{control}, read 'cpu' (missing: memory, pids)"
    )
    assert (parent / "worker").exists() is False


# --------------------------- the per-sandbox ceiling (N83 phase 2, Task 1+8)
#
# D5: what a *single* sandbox may be configured to is a configured policy
# (``E2B_MAX_SANDBOX_*``), never a kernel read -- and never 0/infinity. Ruling
# R17 (2026-10-07) moves the *owner* of that policy to the control plane: the
# worker no longer reads the three envs at all, it **adopts** the ceiling the
# control plane hands down in every register/heartbeat answer
# (``envd_service.agent.adopt_sandbox_ceiling``). Before a hand-down there is no
# ceiling, and no ceiling means no create (``cgroup-refusal
# ceiling-unavailable``) -- never "run unbounded".
#
# D5b rides on the adoption: the handed-down policy is cross-checked against the
# kernel's own limits on this worker's container cgroup -- a policy above the
# kernel is refused **by name** (and *not* adopted, so creates stay refused),
# and a kernel that sets no ceiling (the compose lane, measured) gets one
# explicit WARN instead of silence.

#: Every ceiling-shaped env this file touches. Cleared (and, in the
#: ignored-env case, *set*) to prove the worker's own environment no longer
#: shapes the ceiling: the control plane's hand-down is the only source.
_CEILING_ENVS = (
    "E2B_MAX_SANDBOX_CPU_PERCENT",
    "E2B_MAX_SANDBOX_MEMORY_MB",
    "E2B_MAX_SANDBOX_PROCESSES",
)
_NODE_ENVS = (
    "E2B_NODE_CPU_PERCENT",
    "E2B_NODE_MEMORY_MB",
    "E2B_NODE_PROCESSES",
)


def _clear_ceiling_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CEILING_ENVS + _NODE_ENVS:
        monkeypatch.delenv(name, raising=False)


def _kernel_mount(
    tmp_path: Path, *, cpu: str, memory: str, pids: str
) -> Path:
    """A mount root carrying this worker's own cgroup limits (the D5b read)."""
    mount = tmp_path / "pod"
    mount.mkdir()
    _write(mount / "cpu.max", cpu)
    _write(mount / "memory.max", memory)
    _write(mount / "pids.max", pids)
    return mount


#: The control plane's answer, as it arrives on the wire: the ceiling it handed
#: down, built from its own ``E2B_MAX_SANDBOX_*``.
HAND_DOWN = {
    "sandboxCeiling": {"cpuPercent": 200, "memoryMB": 2048, "processes": 256}
}


def _no_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lane whose cgroup mount does not exist: nothing to cross-check."""
    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "off")
    monkeypatch.delenv("E2B_CGROUP_MOUNT", raising=False)


def _adoption_log(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The agent's own lines during one adoption attempt, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == node_agent.logger.name
    ]


def _refusal_line(refusal: str) -> str:
    """The one ERROR a refused hand-down logs, verbatim around its ``refusal``."""
    return (
        "node agent: refusing the per-sandbox ceiling the control plane handed "
        f"down ({refusal}); this worker keeps running with no ceiling, so every "
        "create is refused by name until the control plane's "
        "E2B_MAX_SANDBOX_* fits this container"
    )


def test_the_worker_adopts_the_ceiling_the_control_plane_hands_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """① R17: the hand-down is the ceiling -- nothing else is consulted."""
    _clear_ceiling_env(monkeypatch)
    _no_mount(monkeypatch)
    node_agent.reset_adopted_sandbox_ceiling()

    adopted = node_agent.adopt_sandbox_ceiling(Settings(), dict(HAND_DOWN))

    assert adopted == SandboxCeiling(cpu_percent=200, memory_mb=2048, processes=256)
    assert node_agent.adopted_sandbox_ceiling() == adopted
    node_agent.reset_adopted_sandbox_ceiling()


def test_the_workers_own_env_no_longer_shapes_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three envs on a *worker* grant nothing: the values are the CP's.

    This is the operator mistake ruling R17 has to make harmless -- a worker
    manifest that still carries the trio (one that was not updated, or copied
    from an older release) must not change a single number.
    """
    _clear_ceiling_env(monkeypatch)
    _no_mount(monkeypatch)
    for name in _CEILING_ENVS:
        monkeypatch.setenv(name, "4096")
    node_agent.reset_adopted_sandbox_ceiling()

    assert node_agent.adopt_sandbox_ceiling(Settings(), dict(HAND_DOWN)) == (
        SandboxCeiling(cpu_percent=200, memory_mb=2048, processes=256)
    )
    node_agent.reset_adopted_sandbox_ceiling()


def test_a_non_positive_hand_down_is_refused_rather_than_adopted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``0`` from the control plane must not read as "one box may take all".

    ``0`` never means "unlimited" on this field, and a control plane that
    answered it is not trusted with a number that would end up in
    ``memory.max``. The hand-down is dropped, which leaves the worker with no
    ceiling -- the fail-closed direction, not the fail-open one.
    """
    _clear_ceiling_env(monkeypatch)
    _no_mount(monkeypatch)
    node_agent.reset_adopted_sandbox_ceiling()

    with caplog.at_level(logging.WARNING):
        adopted = node_agent.adopt_sandbox_ceiling(
            Settings(),
            {"sandboxCeiling": {"cpuPercent": 0, "memoryMB": 2048, "processes": 256}},
        )

    assert adopted is None
    assert node_agent.adopted_sandbox_ceiling() is None
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == node_agent.logger.name
    ] == [
        "node agent: the control plane handed down a per-sandbox ceiling with "
        "cpuPercent=0; refusing to adopt it (a non-positive ceiling would read "
        "as unlimited), so every create stays refused by name until a usable "
        "one arrives"
    ]


def test_no_hand_down_means_no_ceiling_and_the_handle_refuses_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the first answer there is no ceiling, and no ceiling is no create.

    The refusal is the handle's own named one, raised *before* anything is
    created: the lane cannot place a box it cannot bound.
    """
    _clear_ceiling_env(monkeypatch)
    node_agent.reset_adopted_sandbox_ceiling()
    # The handle the pool would have built before the first answer: no ceiling.
    cg, _parent, _proc = _live_cgroups(tmp_path)
    cg.set_policy_ceiling(node_agent.adopted_sandbox_ceiling())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(sandbox_id="sbx_none", pid=SELF_PID, cpu_percent=100,
                  memory_mb=None, max_processes=None)

    assert str(excinfo.value) == (
        "cgroup-refusal ceiling-unavailable: this handle carries no per-sandbox "
        "ceiling, so sandbox sbx_none's declared size cannot be checked against "
        "anything (N83 phase 2 R3/R17): the control plane has not handed one "
        "down to this worker (or the one it handed down was refused), and a "
        "create is never run unbounded on this lane -- set "
        "E2B_SANDBOX_CGROUP=off for a lane that deliberately builds no "
        "per-sandbox cgroup"
    )


def test_the_cross_check_runs_once_per_handed_down_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The heartbeat repeats the value; the verdict must not be re-logged.

    The policy rides *every* beat (5 s), so "check and log per beat" is twelve
    identical lines a minute on the compose lane's legal "the kernel sets no
    limit" shape. One line per handed-down value is the whole rule -- and a
    **changed** value is checked again, which is the case that can newly exceed
    this container.
    """
    _clear_ceiling_env(monkeypatch)
    mount = _kernel_mount(tmp_path, cpu="max 100000", memory="max", pids="max")
    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "required")
    monkeypatch.setenv("E2B_CGROUP_MOUNT", str(mount))
    node_agent.reset_adopted_sandbox_ceiling()
    settings = Settings()
    payload = {"sandboxCeiling": dict(HAND_DOWN["sandboxCeiling"])}

    with caplog.at_level(logging.WARNING):
        first = node_agent.adopt_sandbox_ceiling(settings, payload)
        second = node_agent.adopt_sandbox_ceiling(settings, payload)
        third = node_agent.adopt_sandbox_ceiling(settings, payload)

    assert first == second == third == SandboxCeiling(
        cpu_percent=200, memory_mb=2048, processes=256
    )
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == sandbox_cgroup.__name__
    ] == [
        f"cgroup ceiling: {mount} sets no kernel limit for cpu.max, memory.max "
        "(read 'max'): the physical layer caps nothing, so the per-sandbox "
        "ceiling the control plane handed down is the only bound and aggregate "
        "admission rests on the platform's ledger alone"
    ]
    node_agent.reset_adopted_sandbox_ceiling()


def test_kernel_ceiling_reads_the_container_cgroups_own_limits(
    tmp_path: Path,
) -> None:
    """The kernel read: ``max`` is ``None`` (= the kernel sets no ceiling)."""
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    _write(parent / "cpu.max", "400000 100000")
    _write(parent / "memory.max", "4294967296")
    _write(parent / "pids.max", "max")
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())
    cg.setup(wait_s=0.2)

    assert cg.kernel_ceiling == SandboxCeiling(
        cpu_percent=400, memory_mb=4096, processes=None
    )


def test_a_hand_down_above_the_kernel_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """③ cpu: ``cpu.max`` says 2 cores, the control plane hands down 4 ⇒ refused.

    The refusal is the named ``ceiling-exceeds-kernel``, and it is *not* adopted:
    the worker keeps no ceiling, so every create on it stays refused by name --
    the "the control plane promised 4 cores, this container has 2" mistake is
    caught where both numbers are finally in the same hand.
    """
    _clear_ceiling_env(monkeypatch)
    mount = _kernel_mount(
        tmp_path, cpu="200000 100000", memory="4294967296", pids="max"
    )
    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "required")
    monkeypatch.setenv("E2B_CGROUP_MOUNT", str(mount))
    node_agent.reset_adopted_sandbox_ceiling()

    with caplog.at_level(logging.ERROR):
        adopted = node_agent.adopt_sandbox_ceiling(
            Settings(),
            {"sandboxCeiling": {"cpuPercent": 400, "memoryMB": 2048, "processes": 256}},
        )

    assert adopted is None
    assert node_agent.adopted_sandbox_ceiling() is None
    assert _adoption_log(caplog) == [
        _refusal_line(
            "cgroup-refusal ceiling-exceeds-kernel: this worker's cgroup allows "
            "less than the per-sandbox ceiling the control plane handed down "
            "(cpuPercent=400 > 200% (cpu.max)) -- lower E2B_MAX_SANDBOX_* on the "
            "control plane or raise the worker container's limits; refusing the "
            "hand-down rather than accepting sandboxes the container layer would "
            "throttle or OOM-kill"
        )
    ]


def test_a_hand_down_above_the_kernels_memory_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """③ memory: the same rule for ``memory.max`` ("8 GiB promised, 2 given")."""
    _clear_ceiling_env(monkeypatch)
    mount = _kernel_mount(
        tmp_path, cpu="400000 100000", memory="2147483648", pids="max"
    )
    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "required")
    monkeypatch.setenv("E2B_CGROUP_MOUNT", str(mount))
    node_agent.reset_adopted_sandbox_ceiling()

    with caplog.at_level(logging.ERROR):
        adopted = node_agent.adopt_sandbox_ceiling(
            Settings(),
            {"sandboxCeiling": {"cpuPercent": 400, "memoryMB": 4096, "processes": 256}},
        )

    assert adopted is None
    assert node_agent.adopted_sandbox_ceiling() is None
    assert _adoption_log(caplog) == [
        _refusal_line(
            "cgroup-refusal ceiling-exceeds-kernel: this worker's cgroup allows "
            "less than the per-sandbox ceiling the control plane handed down "
            "(memoryMB=4096 > 2048 MiB (memory.max)) -- lower E2B_MAX_SANDBOX_* "
            "on the control plane or raise the worker container's limits; "
            "refusing the hand-down rather than accepting sandboxes the "
            "container layer would throttle or OOM-kill"
        )
    ]


def test_a_kernel_ceiling_that_cannot_be_read_refuses_the_hand_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The kernel's half is a hard input, not an optional one: no read, no adopt."""
    _clear_ceiling_env(monkeypatch)
    mount = tmp_path / "pod"
    mount.mkdir()
    _write(mount / "cpu.max", "400000 100000")
    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "required")
    monkeypatch.setenv("E2B_CGROUP_MOUNT", str(mount))
    node_agent.reset_adopted_sandbox_ceiling()

    with caplog.at_level(logging.ERROR):
        adopted = node_agent.adopt_sandbox_ceiling(
            Settings(),
            {"sandboxCeiling": {"cpuPercent": 400, "memoryMB": 2048, "processes": 256}},
        )

    assert adopted is None
    assert node_agent.adopted_sandbox_ceiling() is None
    assert _adoption_log(caplog) == [
        _refusal_line(f"cgroup-refusal kernel-ceiling-read: {mount / 'memory.max'}")
    ]


async def test_the_compose_shape_starts_and_warns_that_the_kernel_sets_no_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """④ the measured compose shape: kernel ``max`` ⇒ adopt, with one WARN.

    The three compose stacks set no ``cpus``/``mem_limit``, so the physical
    layer caps nothing there -- which is legal, and must be *said*: the
    handed-down policy and the platform's ledger are then the only things
    holding the line. The cgroup lane itself still starts (its task runs to
    completion here) -- the WARN is what the operator gets, not a refusal.
    """
    _clear_ceiling_env(monkeypatch)
    mount = _kernel_mount(
        tmp_path, cpu="max 100000", memory="max", pids="max"
    )
    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "required")
    monkeypatch.setenv("E2B_CGROUP_MOUNT", str(mount))
    monkeypatch.setattr(
        node_agent,
        "request_cgroup_delegate",
        lambda **kwargs: {"op": "delegate-cgroup", "containerCgroup": str(mount)},
    )
    node_agent.reset_adopted_sandbox_ceiling()

    class ReadyCgroups:
        def setup(self, *, wait_s: float) -> str:
            return f"cgroup ready parent={mount}/worker drained=1 subtree_control=cpu"

    with caplog.at_level(logging.WARNING):
        adopted = node_agent.adopt_sandbox_ceiling(
            Settings(), {"sandboxCeiling": dict(HAND_DOWN["sandboxCeiling"])}
        )
        task = start_cgroup_lane(Settings(), sandbox_cgroups=ReadyCgroups())
        await asyncio.wait_for(task, timeout=5)

    assert adopted == SandboxCeiling(cpu_percent=200, memory_mb=2048, processes=256)
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == sandbox_cgroup.__name__
    ] == [
        f"cgroup ceiling: {mount} sets no kernel limit for cpu.max, memory.max "
        "(read 'max'): the physical layer caps nothing, so the per-sandbox "
        "ceiling the control plane handed down is the only bound and aggregate "
        "admission rests on the platform's ledger alone"
    ]
    node_agent.reset_adopted_sandbox_ceiling()


def test_a_ceiling_that_matches_the_kernel_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The k8s shape (limits == the declared ceilings) says nothing at all."""
    _clear_ceiling_env(monkeypatch)
    mount = _kernel_mount(
        tmp_path, cpu="400000 100000", memory="4294967296", pids="max"
    )
    ceiling = SandboxCeiling(cpu_percent=400, memory_mb=4096, processes=1024)

    with caplog.at_level(logging.WARNING):
        kernel = check_policy_ceiling(ceiling, mount=mount)

    assert kernel == SandboxCeiling(
        cpu_percent=400, memory_mb=4096, processes=None
    )
    assert caplog.records == []


# ---------------------------------------------------------------------------
# N83 phase 2 (Task 5): the kernel's own account of a sandbox hitting a wall
#
# ``sbx_<id>/memory.events`` counts the times a charge hit ``memory.max``
# (``oom_kill``, and the whole-group variant ``oom_group_kill`` that stays 0
# because D3 never writes ``memory.oom.group``); ``sbx_<id>/pids.events`` counts
# the times a *task* creation hit ``pids.max`` (``max`` -- tasks, so threads
# count, plan D4). The kernel removes both files with the directory, so the
# worker reads them at teardown (``release``, *before* ``cgroup.kill``/``rmdir``)
# and, for a live box, on the periodic sweep (``sample_events``). Either way the
# numbers ride the heartbeat as ``sandboxEvents``, and the control plane turns
# "the count grew" into one named WARN -- Review Focus §4: the user must not
# just watch the process mysteriously disappear.

#: What one box's account looks like on the wire: the kernel's own counter
#: names, with ``pids.events``'s ``max`` prefixed by its file -- a bare ``max``
#: would collide with ``memory.events``'s own line of that name.
BOX_EVENTS = {"oom_kill": 2, "oom_group_kill": 1, "pids_max": 3}


def _box_events(
    box: Path, *, oom_kill: int, oom_group_kill: int, pids_max: int
) -> None:
    """The two kernfs event files a real cgroup directory carries."""
    _write(
        box / "memory.events",
        "low 0\nhigh 0\nmax 0\noom 0\n"
        f"oom_kill {oom_kill}\noom_group_kill {oom_group_kill}\n",
    )
    _write(box / "pids.events", f"max {pids_max}\n")


def _live_cgroups(tmp_path: Path) -> tuple[SandboxCgroups, Path, Path]:
    """A handle that has run ``setup``: ready for ``attach``/``sample_events``."""
    mount = _pod_mount(tmp_path)
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(
        mount=mount,
        worker_uid=os.getuid(),
        proc_root=proc_root,
        policy_ceiling=POLICY_CEILING,
    )
    cg.setup(wait_s=0.2)
    return cg, mount / "worker-container", proc_root


def _attach_box(
    cg: SandboxCgroups, proc_root: Path, sandbox_id: str, pid: int
) -> Path:
    _write(proc_root / str(pid) / "cgroup", f"0::/sbx_{sandbox_id}\n")
    return Path(
        cg.attach(
            sandbox_id=sandbox_id,
            pid=pid,
            cpu_percent=100,
            memory_mb=512,
            max_processes=64,
        )
    )


def test_release_reads_the_counters_before_it_removes_the_box(tmp_path: Path) -> None:
    """The teardown reading is the last chance: the files go with the directory."""
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    box = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(box, oom_kill=2, oom_group_kill=1, pids_max=3)

    # A live box is read by the periodic sweep ...
    assert cg.sample_events() == {"alpha": BOX_EVENTS}

    # ... and the teardown's own reading survives it: after ``release`` the
    # directory (and both files) are gone, and the numbers still travel.
    assert cg.release(sandbox_id="alpha") is True
    assert box.exists() is False
    assert cg.sample_events() == {"alpha": BOX_EVENTS}


def test_release_still_tears_the_box_down_when_a_counter_cannot_be_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix 1 (review): **teardown completes.**

    Nothing in this worker reclaims a leftover ``sbx_*`` directory, so a reading
    that cannot be taken has to cost the reading, not the teardown: one named
    WARNING carrying the sandbox id, the path and the underlying error, and then
    ``cgroup.kill`` + ``rmdir`` proceed. What is lost is exactly that reading --
    the last chance to see these numbers, in the window where the periodic sweep
    had not already cached them.
    """
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    box = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(box, oom_kill=2, oom_group_kill=0, pids_max=3)
    # A counter file that is there but does not parse: the same class of
    # "cannot take the reading", and (unlike a permission error) reproducible in
    # a plain tree without pretending to be unprivileged.
    _write(box / "memory.events", "oom_kill not-a-number\n")

    with caplog.at_level(logging.WARNING):
        assert cg.release(sandbox_id="alpha") is True

    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == sandbox_cgroup.__name__
    ] == [
        f"cgroup events: sandbox alpha: the last-chance reading of {box} "
        f"failed: cgroup-refusal events-format: {box / 'memory.events'} reads "
        "'oom_kill not-a-number' for oom_kill, expected '<name> <count>'; "
        "tearing the box down anyway -- this reading is lost, because the kernel "
        "removes the counters with the directory (the values the sweep already "
        "cached still reach the control plane)"
    ]
    assert box.exists() is False
    # The honest cost: nothing is remembered for a box whose account was not read.
    assert cg.sample_events() == {}


def test_a_permission_error_is_named_by_the_read_not_by_the_rmdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review Minor #3: the diagnostic names the step that actually failed.

    A permission problem on a counter file is reported as
    ``events-read: <file> (Operation not permitted)`` -- not as a
    ``release-rmdir`` that hides its cause, and not as "this box has no
    account". The read is the only thing faked here (macOS and Linux both let
    the *file* be removed but not read, and a plain tree cannot express that
    without privileges), and the teardown still completes.
    """
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    box = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(box, oom_kill=2, oom_group_kill=0, pids_max=3)
    real_read_text = Path.read_text
    blocked = box / "memory.events"

    def refusing(self: Path, *args: object, **kwargs: object) -> str:
        if self == blocked:
            raise PermissionError(13, "Operation not permitted")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", refusing)

    with caplog.at_level(logging.WARNING):
        assert cg.release(sandbox_id="alpha") is True

    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == sandbox_cgroup.__name__
    ] == [
        f"cgroup events: sandbox alpha: the last-chance reading of {box} "
        f"failed: cgroup-refusal events-read: {blocked} (Operation not "
        "permitted); tearing the box down anyway -- this reading is lost, "
        "because the kernel removes the counters with the directory (the values "
        "the sweep already cached still reach the control plane)"
    ]
    assert box.exists() is False


def test_release_still_tears_the_box_down_when_it_cannot_even_be_looked_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Item 4 (Task 11): "unreadable" is not "absent", and it is not a leak.

    ``if not target.exists(): return False`` used to decide both from the same
    call, and Python 3.12's ``Path.exists()`` re-raises ``EACCES`` (earlier
    versions silently answer ``False``). So a box whose directory could not be
    looked at was reported as ``release-rmdir: could not rmdir …`` -- a step
    that had not run yet -- and the teardown ended there: ``cgroup.kill`` was
    never written and the ``rmdir`` was never attempted, with nothing in the
    worker to reclaim the leftover ``sbx_*`` directory afterwards.

    Now the probe is an explicit ``stat`` that separates the two facts, the
    unreadable case is one named WARNING (sandbox id + path + underlying
    error), and the teardown still finishes: the box is gone and ``release``
    answered ``True``.
    """
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    box = _attach_box(cg, proc_root, "alpha", 4242)
    real_stat = Path.stat

    def refusing(self: Path, *args: object, **kwargs: object):
        if self == box:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", refusing)

    with caplog.at_level(logging.WARNING):
        assert cg.release(sandbox_id="alpha") is True

    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == sandbox_cgroup.__name__
    ] == [
        f"cgroup release: sandbox alpha: cgroup-refusal release-stat: {box} "
        "(Permission denied); tearing the box down anyway -- nothing in this "
        "worker reclaims a leftover sbx_* directory, so a box left standing "
        "here is a permanent leak"
    ]
    # ``Path.stat`` is the faked call here, so the absence is read with the
    # real one (and the box really is gone, not merely unreadable).
    assert os.path.exists(box) is False


def test_the_sampler_reports_only_the_boxes_that_hit_a_wall(tmp_path: Path) -> None:
    """A clean box carries no event, so it is not on the wire every 5 s."""
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    alpha = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(alpha, oom_kill=2, oom_group_kill=1, pids_max=3)
    beta = _attach_box(cg, proc_root, "beta", 4243)
    _box_events(beta, oom_kill=0, oom_group_kill=0, pids_max=0)
    _attach_box(cg, proc_root, "gamma", 4244)  # no event files at all

    assert cg.sample_events() == {"alpha": BOX_EVENTS}


def test_the_sampler_skips_a_box_whose_account_cannot_be_read(
    tmp_path: Path,
) -> None:
    """The sweep never raises -- a heartbeat must not be lost over a cgroup
    file -- and it still reports the boxes it could read."""
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    alpha = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(alpha, oom_kill=2, oom_group_kill=1, pids_max=3)
    beta = _attach_box(cg, proc_root, "beta", 4243)
    _box_events(beta, oom_kill=9, oom_group_kill=0, pids_max=0)
    (beta / "memory.events").unlink()
    (beta / "memory.events").mkdir()

    assert cg.sample_events() == {"alpha": BOX_EVENTS}


def test_a_leftover_box_that_still_carries_an_account_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """Fix 2 (review): **reuse must never inherit an account.**

    The previous occupant's tasks are gone, so the box looks reusable -- but its
    ``*.events`` are still there, and a kernfs counter cannot be cleared in
    place (measured on a real cgroup v2 mount: ``unlink`` is ``EPERM`` and a
    write back to zero is ``EINVAL``). Refusing by name is therefore the only
    answer that neither misattributes the previous occupant's wall to the new
    sandbox nor silently runs it in a directory that carries a history.
    """
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    box = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(box, oom_kill=1, oom_group_kill=0, pids_max=0)
    _write(box / "cgroup.procs", "")  # the previous occupant's tasks are gone

    with pytest.raises(CgroupRefusal) as excinfo:
        _attach_box(cg, proc_root, "alpha", 4243)

    assert str(excinfo.value) == (
        f"cgroup-refusal sbx-stale-account: sandbox alpha's cgroup directory "
        f"{box} still carries a previous generation's kernel event counters "
        "(oom_kill=1), and a kernfs counter cannot be cleared in place -- "
        "refusing to place a new sandbox in it rather than report the previous "
        "occupant's wall as its own"
    )
    assert box.exists() is True
    assert (box / "cgroup.procs").read_text() == ""


def test_a_leftover_box_whose_account_cannot_be_read_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """The same gate, for the case where "clean" cannot be established: an
    unreadable account is refused by name, never read as "no previous events"."""
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    box = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(box, oom_kill=0, oom_group_kill=0, pids_max=0)
    _write(box / "cgroup.procs", "")
    (box / "pids.events").unlink()
    (box / "pids.events").mkdir()  # present, but not readable as a file

    with pytest.raises(CgroupRefusal) as excinfo:
        _attach_box(cg, proc_root, "alpha", 4243)

    assert str(excinfo.value) == (
        f"cgroup-refusal events-read: {box / 'pids.events'} (Is a directory)"
    )


def test_a_later_box_under_the_same_id_starts_with_a_clean_account(
    tmp_path: Path,
) -> None:
    """Fix 2 (review), sampler half: the last-chance reading of a *previous*
    generation must not be reported as the new occupant's own."""
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    first = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(first, oom_kill=5, oom_group_kill=0, pids_max=0)
    assert cg.release(sandbox_id="alpha") is True
    # The teardown reading still travels while this id has no occupant ...
    assert cg.sample_events() == {
        "alpha": {"oom_kill": 5, "oom_group_kill": 0, "pids_max": 0}
    }

    # ... and it does not follow the next occupant of that id.
    second = _attach_box(cg, proc_root, "alpha", 4243)
    _box_events(second, oom_kill=0, oom_group_kill=0, pids_max=0)
    assert cg.sample_events() == {}


def test_a_counter_that_cannot_be_unlinked_is_named_in_the_rmdir_refusal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix 4 (review): the refusal names the step that actually blocked the
    removal. A `pids.events` that cannot be unlinked (a real cgroupfs refuses
    that with `EPERM`; a directory in its place refuses it here) is what stops
    the second `rmdir` -- reporting only "could not rmdir" hides the cause.

    The errno *text* is the platform's (macOS says "Operation not permitted" for
    unlinking a directory, Linux says "Is a directory"), so this case measures it
    here and pins the full message with it: what is being pinned is that the
    message names the file and its error, not the wording of one libc.
    """
    cg, _parent, proc_root = _live_cgroups(tmp_path)
    box = _attach_box(cg, proc_root, "alpha", 4242)
    _box_events(box, oom_kill=0, oom_group_kill=0, pids_max=0)
    probe = tmp_path / "unlink-probe"
    probe.mkdir()
    with pytest.raises(OSError) as probe_error:
        probe.unlink()
    probe.rmdir()
    (box / "pids.events").unlink()
    (box / "pids.events").mkdir()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(CgroupRefusal) as excinfo:
            cg.release(sandbox_id="alpha")

    assert str(excinfo.value) == (
        f"cgroup-refusal release-rmdir: could not rmdir {box}"
        f"; could not unlink pids.events ({probe_error.value.strerror})"
    )
    assert box.exists() is True


def test_the_heartbeat_carries_the_event_section_only_when_there_is_one() -> None:
    """The wire field: ``sandboxEvents``, one entry per sandbox that hit a wall."""
    payload = node_agent._heartbeat_usage_payload(
        Settings(), sandbox_events={"alpha": BOX_EVENTS}
    )

    assert payload["sandboxEvents"] == {"alpha": BOX_EVENTS}
    assert "sandboxEvents" not in node_agent._heartbeat_usage_payload(Settings())
    assert "sandboxEvents" not in node_agent._heartbeat_usage_payload(
        Settings(), sandbox_events={}
    )


def test_the_off_lane_samples_nothing_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """``E2B_SANDBOX_CGROUP=off`` reaches no further than this: there is no
    ``sbx_<id>`` to read, so no handle is built and no cgroup file is touched.

    The construction is *recorded* rather than raised on (review Minor #2):
    ``sample_sandbox_events`` swallows every exception by design -- a heartbeat
    must never fail -- so a stub that raised would be swallowed too and this
    test would pass even if the off lane did build a handle. The list is what
    makes the assertion able to fail.
    """
    from envd_service import route_b

    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "off")
    built: list[dict] = []

    class _Handle:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

        def sample_events(self) -> dict[str, dict[str, int]]:
            return {"alpha": dict(BOX_EVENTS)}

    monkeypatch.setattr(route_b, "SandboxCgroups", _Handle)

    assert node_agent.sample_sandbox_events(Settings()) == {}
    assert built == []


def test_the_sampler_goes_through_the_process_wide_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat's sampler resolves the *one* handle ``setup`` established
    and ``attach`` wrote through -- never a second object with its own view."""
    from envd_service import route_b

    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "required")
    monkeypatch.setenv("E2B_CGROUP_MOUNT", str(tmp_path / "pod"))
    seen: list[dict] = []

    class _Handle:
        def __init__(self, **kwargs: object) -> None:
            seen.append(kwargs)

        def sample_events(self) -> dict[str, dict[str, int]]:
            return {"alpha": dict(BOX_EVENTS)}

    monkeypatch.setattr(route_b, "SandboxCgroups", _Handle)

    assert node_agent.sample_sandbox_events(Settings()) == {"alpha": BOX_EVENTS}
    assert len(seen) == 1
