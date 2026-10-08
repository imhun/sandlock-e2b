"""T5 own-identity evidence at the envd layer (backlog #5).

The envd W1 slot pool starts two ``sandlock-supervise`` slots at two
distinct host uids over the shared 1777+sticky directory, and the worker
drives both through the fleet's own channel client -- transport 1, the
handed-over control descriptor (fork F17), so neither a registry socket path
nor a channel token ever appears in the slot's argv:

* uid X's exec creates a file through the mediated path — the host-side
  owner must be X, and X's own chmod must take effect;
* uid Y's exec can read it but its rm/chmod must fail EPERM (real sticky
  semantics), proving per-uid volume protection is reachable from Python on
  the envd worker side.

This is the envd-side counterpart of the fork ``mediation_2uid`` B档, and the
precondition the ``test_uid_permissions`` xfail was waiting on.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from envd_service.own_identity import W1SlotPool, default_supervise_bin
from tests.security.conftest import sandlock_ready


pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not sandlock_ready(),
    reason=(
        "own-identity slot tests need a root worker + sandlock wheel with the "
        "supervise binary (run inside the privileged Docker test runner)"
    ),
)


UID_X = 21100
UID_Y = 21101


X_CODE = """
import os, sys
shared, ev = sys.argv[1], sys.argv[2]
path = os.path.join(shared, "x.txt")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, b"XDATA\\n")
os.close(fd)
os.chmod(path, 0o644)
with open(os.path.join(ev, "x-done"), "w") as f:
    f.write("created\\n")
"""


Y_CODE = """
import json, os, sys
shared, ev = sys.argv[1], sys.argv[2]
path = os.path.join(shared, "x.txt")
out = {}
try:
    with open(path) as f:
        out["read"] = f.read()
except OSError as e:
    out["read_errno"] = e.errno
try:
    os.unlink(path)
    out["rm_errno"] = 0
except OSError as e:
    out["rm_errno"] = e.errno
try:
    os.chmod(path, 0o600)
    out["chmod_errno"] = 0
except OSError as e:
    out["chmod_errno"] = e.errno
out["exists"] = os.path.exists(path)
with open(os.path.join(ev, "y-report.json"), "w") as f:
    json.dump(out, f)
"""


def _chmod(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _run_exec(ch, code: str, shared: str, evidence: str) -> dict:
    devnull = os.open("/dev/null", os.O_RDWR)
    try:
        started = ch.request(
            "exec",
            {
                "argv": [
                    "python3",
                    "-B",
                    "-c",
                    code,
                    shared,
                    evidence,
                ]
            },
            fds=[devnull, devnull, devnull],
        )
    finally:
        os.close(devnull)
    status = ch.request("wait_child", {"child_id": started["child_id"]})
    assert status.get("code") == 0, status
    return status


#: Task 13 item 3: the sizes these cases **declare** for the boxes they build.
#: ``W1SlotPool.acquire`` reads an undeclared ``memory_mb``/``max_processes``
#: as "the caller did not say", and the cgroup module then writes the
#: per-sandbox *ceiling* -- so a case that declares nothing builds (or, on this
#: lane, documents) the biggest box the node allows (4096 MiB / 1024 tasks on
#: the k0s lane) without saying so. These cases are about sticky directories
#: and uid reuse, not sizes, so they declare exactly what the ceiling would
#: have supplied. Neither pool here is built with a cgroup handle, so the two
#: numbers are the declaration and not an applied quota; the pin in
#: ``test_the_hand_built_pool_probes_declare_the_sizes_of_the_box_they_build``
#: keeps every ``acquire`` call in this file saying so.
DECLARED_MEMORY_MB = 4096
DECLARED_MAX_PROCESSES = 1024


def _policy(shared: Path, evidence: Path) -> dict:
    return {
        "fs_readable": [
            "/usr",
            "/usr/local",
            "/lib",
            "/bin",
            "/etc",
            "/proc",
            "/dev",
            "/tmp",
        ],
        "fs_writable": [str(shared), str(evidence)],
        "fs_denied": [str(shared / "secret.txt")],
        "env": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
    }


async def test_w1_slot_pool_two_uids_share_sticky_dir_through_python_client():
    assert default_supervise_bin().exists(), "sandlock-supervise missing from the wheel"
    base = Path(tempfile.mkdtemp(prefix="rb-pool-"))
    _chmod(base, 0o777)
    shared = base / "shared"
    evidence = base / "evidence"
    shared.mkdir()
    evidence.mkdir()
    _chmod(shared, 0o1777)
    _chmod(evidence, 0o1777)

    pool = W1SlotPool(
        uid_start=UID_X,
        size=2,
        tmp_root=base / "slots",
    )
    try:
        sx = await pool.acquire(
            "sbx_x",
            _policy(shared, evidence),
            memory_mb=DECLARED_MEMORY_MB,
            max_processes=DECLARED_MAX_PROCESSES,
        )
        assert sx.uid == UID_X
        sy = await pool.acquire(
            "sbx_y",
            _policy(shared, evidence),
            memory_mb=DECLARED_MEMORY_MB,
            max_processes=DECLARED_MAX_PROCESSES,
        )
        assert sy.uid == UID_Y
        # Transport 1 invariants: the credential is a descriptor, so there is
        # no path to guess and no secret in the slot's argv.
        for slot in (sx, sy):
            assert slot.sock_path is None and slot.token is None
            assert slot.control_socket is not None

        with pool.channel_for(sx) as chx:
            _run_exec(chx, X_CODE, str(shared), str(evidence))

        x_file = shared / "x.txt"
        meta = x_file.stat()
        assert meta.st_uid == UID_X, f"owner must be X, got {meta.st_uid}"
        assert stat.S_IMODE(meta.st_mode) == 0o644
        assert (evidence / "x-done").read_text(encoding="utf-8") == "created\n"

        with pool.channel_for(sy) as chy:
            _run_exec(chy, Y_CODE, str(shared), str(evidence))

        report = json.loads(
            (evidence / "y-report.json").read_text(encoding="utf-8")
        )
        assert report["read"] == "XDATA\n"
        assert report["rm_errno"] == 1
        assert report["chmod_errno"] == 1
        assert report["exists"] is True
        assert x_file.stat().st_uid == UID_X
        assert stat.S_IMODE(x_file.stat().st_mode) == 0o644
    finally:
        await pool.release("sbx_x")
        await pool.release("sbx_y")
        shutil.rmtree(base, ignore_errors=True)


async def test_w1_pool_refuses_live_uid_reuse_and_exhausts_cleanly():
    base = Path(tempfile.mkdtemp(prefix="rb-pool2-"))
    _chmod(base, 0o777)
    pool = W1SlotPool(uid_start=UID_X, size=1, tmp_root=base / "slots")
    try:
        policy = {
            "fs_readable": ["/usr", "/usr/local", "/lib", "/bin", "/etc", "/proc", "/dev"],
            "fs_writable": [str(base)],
            "env": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
        }
        sx = await pool.acquire(
            "sbx_only",
            policy,
            memory_mb=DECLARED_MEMORY_MB,
            max_processes=DECLARED_MAX_PROCESSES,
        )
        assert sx.uid == UID_X
        with pytest.raises(RuntimeError, match="exhausted"):
            await pool.acquire(
                "sbx_second",
                policy,
                memory_mb=DECLARED_MEMORY_MB,
                max_processes=DECLARED_MAX_PROCESSES,
            )
        # Release returns the uid and a second sandbox can take it (W1
        # restart-in-place semantics).
        await pool.release("sbx_only")
        sx2 = await pool.acquire(
            "sbx_second",
            policy,
            memory_mb=DECLARED_MEMORY_MB,
            max_processes=DECLARED_MAX_PROCESSES,
        )
        assert sx2.uid == UID_X
        await pool.release("sbx_second")
    finally:
        await pool.release("sbx_only")
        await pool.release("sbx_second")
        shutil.rmtree(base, ignore_errors=True)
