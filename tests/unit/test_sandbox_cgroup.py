"""N83 phase 1: the worker's per-sandbox cgroup primitives (form W).

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
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from envd_service.runtime import sandbox_cgroup
from envd_service.runtime.sandbox_cgroup import (
    CgroupRefusal,
    SandboxCgroups,
    cpu_max_for,
)

#: The pid the synthetic cgroup.procs files carry. The module must see its own
#: pid there (``self-placement``), so the tests use the process's real pid.
SELF_PID = os.getpid()


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


def test_setup_drains_the_parent_and_enables_cpu(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"

    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid())

    line = cg.setup(wait_s=0.2)

    # Drained: the parent no longer holds the worker, and the worker/ dir does.
    assert sandbox_cgroup._cgroup_pids(parent) == []
    assert (parent / "worker" / "cgroup.procs").read_text() == f"{SELF_PID}\n"
    # +cpu written literally, then verified by readback (kernel echoes "cpu").
    assert (parent / "cgroup.subtree_control").read_text() == "+cpu"
    assert line == (
        f"cgroup ready parent={parent} worker_uid={os.getuid()} "
        f"drained=1 subtree_control=cpu"
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
        f"drained=1 subtree_control=cpu"
    )


def test_attach_writes_cpu_max_and_places_the_pid(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=proc_root)
    cg.setup(wait_s=0.2)
    pid = 4242
    _write(proc_root / str(pid) / "cgroup", "0::/sbx_alpha\n")

    target = cg.attach(sandbox_id="alpha", pid=pid, cpu_percent=100)

    assert target == str(parent / "sbx_alpha")
    assert (parent / "sbx_alpha" / "cpu.max").read_text() == "100000 100000"
    assert (parent / "sbx_alpha" / "cgroup.procs").read_text() == f"{pid}\n"


def test_attach_refuses_a_target_that_already_holds_pids(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
    cg.setup(wait_s=0.2)
    target = parent / "sbx_busy"
    target.mkdir()
    _write(target / "cgroup.procs", "999\n")

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(sandbox_id="busy", pid=4242, cpu_percent=50)
    assert str(excinfo.value) == (
        f"cgroup-refusal sbx-in-use: {target} already holds pids [999]"
    )
    # A refused reuse must not disturb what is already there.
    assert (target / "cgroup.procs").read_text() == "999\n"


def test_attach_removes_what_it_created_when_placement_check_fails(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    proc_root = tmp_path / "proc"
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=proc_root)
    cg.setup(wait_s=0.2)
    pid = 4242
    _write(proc_root / str(pid) / "cgroup", "0::/elsewhere\n")

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(sandbox_id="alpha", pid=pid, cpu_percent=100)
    assert str(excinfo.value) == (
        f"cgroup-refusal placement: pid {pid} is in '0::/elsewhere', "
        f"expected '0::/sbx_alpha'"
    )
    assert (parent / "sbx_alpha").exists() is False


def test_release_is_idempotent(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
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
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
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
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
    cg.setup(wait_s=0.2)
    target = parent / "sbx_alpha"
    real_mkdir = Path.mkdir

    def deny(self: Path, *args: object, **kwargs: object) -> None:
        if self == target:
            raise PermissionError(13, "Permission denied")
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", deny)

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(sandbox_id="alpha", pid=4242, cpu_percent=100)
    assert str(excinfo.value) == f"cgroup-refusal sbx-mkdir: {target}"
    assert target.exists() is False


def test_attach_refuses_when_an_existing_target_cannot_be_read(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
    cg.setup(wait_s=0.2)
    # Exists, but has no cgroup.procs to read: the reuse probe fails, and that
    # must be a named refusal, not a bare OSError.
    target = parent / "sbx_alpha"
    target.mkdir()

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(sandbox_id="alpha", pid=4242, cpu_percent=100)
    assert str(excinfo.value) == f"cgroup-refusal attach-io: {target}"
    assert target.exists() is True


def test_release_refuses_when_cgroup_kill_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
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
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
    cg.setup(wait_s=0.2)
    before = sorted(child.name for child in parent.iterdir())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.attach(sandbox_id=bad_id, pid=4242, cpu_percent=100)

    assert str(excinfo.value) == (
        f"cgroup-refusal sandbox-id: {bad_id!r} is not a valid sandbox id"
    )
    # Nothing was created, inside the delegated subtree or out of it.
    assert sorted(child.name for child in parent.iterdir()) == before
    assert (tmp_path / "evil").exists() is False


def test_release_refuses_a_path_escaping_sandbox_id(tmp_path: Path) -> None:
    mount = _pod_mount(tmp_path)
    parent = mount / "worker-container"
    cg = SandboxCgroups(mount=mount, worker_uid=os.getuid(), proc_root=tmp_path / "proc")
    cg.setup(wait_s=0.2)
    before = sorted(child.name for child in parent.iterdir())

    with pytest.raises(CgroupRefusal) as excinfo:
        cg.release(sandbox_id="a/b")

    assert str(excinfo.value) == (
        "cgroup-refusal sandbox-id: 'a/b' is not a valid sandbox id"
    )
    assert sorted(child.name for child in parent.iterdir()) == before
