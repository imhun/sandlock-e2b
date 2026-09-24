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

import ctypes
import errno
import json
import platform
import subprocess
import sys
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


# ------------------------------------------------- the number it asks for

#: Calls the candidate number with arguments no real `pivot_root` can accept,
#: in a *child* (a number that turned out to be a working `pivot_root` must not
#: be able to relocate the test runner's root), and prints `rc errno`.
_ARCH_PROBE = r"""
import ctypes, json, platform
libc = ctypes.CDLL("libc.so.6", use_errno=True)
ctypes.set_errno(0)
rc = libc.syscall(%d, b"/", b"/")
print(json.dumps({"arch": platform.machine(), "rc": rc, "errno": ctypes.get_errno()}))
"""


def _call_in_child(nr: int) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _ARCH_PROBE % nr],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_probe_asks_for_the_pivot_root_this_architecture_has():
    """`pivot_root` is 155 on x86_64 and 41 on the generic-table arches.

    Pinning the bug rather than the constant: the probe used to ask for a
    hardcoded 155, and on aarch64 that number is `sched_getattr` -- it answered
    ESRCH ("No such process") and the gate reported it as "the seccomp profile
    does not admit pivot_root", so `E2B_REAL_ROOT` could never be armed on the
    architecture production runs (measured 2026-09-24: `syscall(155)` -> ESRCH
    on aarch64, while the real `pivot_root` at 41 answers EINVAL for these
    deliberately-unusable paths). Every assertion below fails on that code on
    an aarch64 machine.
    """
    arch = platform.machine()
    assert arch in sl._PIVOT_ROOT_NR, f"no pivot_root number for {arch}"
    answer = _call_in_child(sl._PIVOT_ROOT_NR[arch])
    assert answer["arch"] == arch
    code = errno.errorcode.get(answer["errno"], str(answer["errno"]))
    assert answer["rc"] == -1, f"pivot_root({arch}) relocated a child's root"
    assert code != "ESRCH", (
        f"{sl._PIVOT_ROOT_NR[arch]} names a different syscall on {arch} (ESRCH)"
    )
    assert code != "ENOSYS", (
        f"{sl._PIVOT_ROOT_NR[arch]} names no syscall at all on {arch} (ENOSYS)"
    )
    # The answer a real pivot_root gives for a root that is its own put_old, or
    # for a caller without the namespace capability.
    assert code in {"EINVAL", "EBUSY", "EPERM"}, f"unexpected pivot_root answer: {code}"


def test_the_probe_carries_the_table_into_the_child():
    """The number has to survive into the probe text, not just the module.

    The probe is a *string* run by `python -c`, so a mapping that only exists
    in this process would leave the child with a NameError -- and a child that
    dies is reported as a reason rather than as a wrong number, which is the
    same silent-failure shape the pivot_root bug had.
    """
    assert "__PIVOT_ROOT_NR__" not in sl._REAL_ROOT_PROBE
    assert repr(sl._PIVOT_ROOT_NR) in sl._REAL_ROOT_PROBE
