"""`E2B_REAL_ROOT` refuses to arm where the worker cannot build a root.

The flag and the worker's seccomp profile have to travel together: the profile
has to admit the mount family before a sandbox's own user namespace can use it
(the pre-N35 profile admits neither `pivot_root` at all). Without the check in
`SandlockExecutor.__init__`, a node that was not updated fails every create with
"instance is closed" and no reason -- measured, and the whole point of the
one-fork probe this file pins the wiring of.

The probe itself is exercised end to end by the lane (`E2B_REAL_ROOT=1` running
the security suite, and the same with a profile stripped of the allowance); here
the fleet is faked and only the decision is under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import envd_service.executors.sandlock as sl
from envd_service.route_b import RouteBConfig

WORKSPACE = "/var/lib/e2b-sandboxes/sbx_real_root/workspace"
ROOTFS = Path("tmp/unit-real-root-rootfs")
HOST_UID = 20007


def _config() -> RouteBConfig:
    return RouteBConfig(mode="off", uid_start=HOST_UID, uid_size=2, tmp_root="/tmp/unit-rb")


def _executor(**over) -> sl.SandlockExecutor:
    kwargs = {
        "workspace_dir": WORKSPACE,
        "base_image": "python:3.11-slim",
        "image_rootfs": ROOTFS,
        "host_uid": HOST_UID,
        "per_sandbox_uid": True,
        "memory_mb": 1024,
        "cpu_percent": 100,
        "disk_mb": 2048,
        "max_processes": 128,
        "max_open_files": 1024,
        "allow_internet_access": False,
        "enable_network": False,
        "sandbox_id": "sbx_real_root",
        "route_b": _config(),
    }
    kwargs.update(over)
    return sl.SandlockExecutor(**kwargs)


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    """The probe result is process-wide by design; each test starts clean."""
    sl._real_root_capability.cache_clear()
    yield
    sl._real_root_capability.cache_clear()


def test_the_flag_is_refused_when_the_worker_cannot_build_a_root(monkeypatch):
    monkeypatch.setattr(
        sl, "_real_root_capability", lambda: "pivot_root: Operation not permitted"
    )
    with pytest.raises(RuntimeError) as excinfo:
        _executor(real_root=True)
    message = str(excinfo.value)
    assert "E2B_REAL_ROOT is on, but this worker cannot build a sandbox root" in message
    assert "pivot_root: Operation not permitted" in message
    # The operator's next action has to be in the message, not in a doc.
    assert "deploy/seccomp/sandlock-worker.json" in message


def test_the_probe_is_not_consulted_when_the_flag_is_off(monkeypatch):
    def _boom() -> str:
        raise AssertionError("the probe must not run for the default shape")

    monkeypatch.setattr(sl, "_real_root_capability", _boom)
    assert _executor(real_root=False) is not None


def test_a_sandbox_without_a_rootfs_never_arms_the_flag(monkeypatch):
    def _boom() -> str:
        raise AssertionError("there is nothing to pivot into, so nothing to probe")

    monkeypatch.setattr(sl, "_real_root_capability", _boom)
    executor = _executor(real_root=True, base_image=None, image_rootfs=None)
    # The declaration still reaches the policy document (the fork ignores it
    # without a chroot root); what matters here is that no probe ran.
    assert executor._real_root is True


# --------------------------------------------------------------- the probe


def test_the_probes_stdout_is_the_reason_the_gate_reports(monkeypatch):
    """Whatever the probe prints is the operator's diagnosis, verbatim.

    Measured shape of the reason on a node whose profile has not been updated:
    the pre-N35 profile admits neither ``pivot_root`` nor the un-filtered
    ``umount2``, so that is the step that fails there and the one the message
    has to name.
    """
    monkeypatch.setattr(
        sl,
        "_REAL_ROOT_PROBE",
        "print('pivot_root (the profile must admit it): Operation not permitted')",
    )
    assert sl._real_root_capability() == (
        "pivot_root (the profile must admit it): Operation not permitted"
    )


def test_a_probe_that_says_ok_means_the_worker_can_build_a_root(monkeypatch):
    monkeypatch.setattr(sl, "_REAL_ROOT_PROBE", "print('ok')")
    assert sl._real_root_capability() == ""


def test_a_probe_that_dies_without_a_reason_still_names_one(monkeypatch):
    """A probe killed on the way up (a missing libc, a signal) must not come
    back as a bare empty string: that reads as "this node is fine"."""
    monkeypatch.setattr(sl, "_REAL_ROOT_PROBE", "raise SystemExit(3)")
    assert sl._real_root_capability() == "the probe exited 3 without a reason"


def test_a_probe_that_never_answers_is_a_reason_not_a_wedge(monkeypatch):
    """The child gets a deadline, because the check exists to keep a node from
    wedging -- a probe that blocks the worker would be the disease."""
    monkeypatch.setattr(sl, "_REAL_ROOT_PROBE", "import time; time.sleep(30)")
    monkeypatch.setattr(sl, "_REAL_ROOT_PROBE_TIMEOUT_S", 0.05)
    assert sl._real_root_capability() == "the probe did not finish within 0.05s"
