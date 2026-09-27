"""Why does a *missing* path answer EACCES(13) instead of ENOENT(2)?

The contract pins "missing binary -> exit 127 with no output"; in the phase-1
shape the fork's execvp reports errno 13, and the FUP-26 line then lands on the
guest's stderr. This probe reproduces the exact shape of
tests/contract/test_route_b_executor.py::_executor (pure shape forced onto route
B) and asks the guest for an errno matrix: which paths are *searchable*, which
answer ENOENT, and whether the matrix moves with SANLOCK_REALROOT_TRACE.

    sh deploy/scripts/acceptance/phase1-probe2.sh /workspace/deploy/scripts/acceptance/probe_127_errno.py     # trace unset
    TRACE_ENV="-e SANLOCK_REALROOT_TRACE=/tmp/x" \
        sh deploy/scripts/acceptance/phase1-probe2.sh /workspace/deploy/scripts/acceptance/probe_127_errno.py # trace set
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor
from envd_service.route_b import RouteBConfig

UID = 1000
WORKSPACE = Path("/var/lib/e2b-test-runtime/probe-127")

GUEST = r'''
import json, os, subprocess

PATHS = [
    "/nonexistent-e2b-bin",          # the contract's path: parent is /
    "/usr/nonexistent-e2b-bin",      # parent /usr
    "/bin/nonexistent-e2b-bin",      # parent /bin
    "/etc/nonexistent-e2b-bin",      # parent /etc
    "/workspace/nonexistent-e2b-bin",  # parent /workspace (declared writable)
    "/tmp/nonexistent-e2b-bin",      # parent /tmp
    "/etc/hostname",                 # exists, not executable
    "/bin/sh",                       # exists, executable
]

out = {"uid": os.getuid(), "gid": os.getgid()}

# An executable *inside* the declared writable set: if this one runs, the
# EACCES on everything above is a policy answer about *those* paths; if it is
# refused too, the sandbox refuses guest exec as such.
for candidate in ("/bin/true", "/usr/bin/env"):
    try:
        with open(candidate, "rb") as src:
            blob = src.read()
        target = "/var/lib/e2b-test-runtime/probe-127/owned_bin"
        with open(target, "wb") as dst:
            dst.write(blob)
        os.chmod(target, 0o755)
        PATHS.append(target)
        break
    except OSError as exc:
        out["copy " + candidate] = exc.errno

# NOTE: no exec probes here. Python's spawn machinery forks first, and in this
# shape that fork is what fails -- measuring it would say nothing about the
# exec's own errno. The exec question is answered by the init itself
# (probe_127_init.py), which is the process the contract is about.
for path in PATHS:
    try:
        os.stat(path)
        out["stat " + path] = 0
    except OSError as exc:
        out["stat " + path] = exc.errno
for directory in ("/", "/usr", "/etc", "/tmp", "/var", "/var/lib"):
    try:
        os.listdir(directory)
        out["listdir " + directory] = 0
    except OSError as exc:
        out["listdir " + directory] = exc.errno

# The fork's exec-failure path calls realroot::record_failure(), which opens
# the trace file (default /tmp/sandlock-real-root-error) *after* execvp failed
# and *before* the errno it prints is read again. If that open is denied here,
# the diagnostic reports the open's errno instead of the exec's.
for path, mode in (
    ("/tmp/sandlock-real-root-error", "a"),
    ("/tmp/probe-trace-writable.txt", "w"),
    ("/var/lib/e2b-test-runtime/probe-127/trace.txt", "w"),
):
    try:
        with open(path, mode):
            pass
        out["open " + path] = 0
    except OSError as exc:
        out["open " + path] = exc.errno
print(json.dumps(out, sort_keys=True))
'''


def _executor() -> SandlockExecutor:
    os.makedirs(WORKSPACE, exist_ok=True)
    os.chown(WORKSPACE, UID, UID)
    os.chmod(WORKSPACE, 0o700)
    scratch = WORKSPACE.parent / "route-b" / "sbx_probe_127"
    return SandlockExecutor(
        workspace_dir=str(WORKSPACE),
        base_image=None,
        image_rootfs=None,
        host_uid=UID,
        per_sandbox_uid=True,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=1024,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id="sbx_probe_127",
        route_b=RouteBConfig(mode="on", uid_start=UID, uid_size=2, tmp_root=scratch),
    )


async def main() -> None:
    executor = _executor()
    (WORKSPACE / "probe.py").write_text(GUEST)
    print(
        "policy:",
        json.dumps(executor._policy_ceiling(), sort_keys=True, default=str),
    )
    running = await executor.start(
        ExecConfig(
            cmd=["/usr/local/bin/python3", str(WORKSPACE / "probe.py")],
            env={},
            cwd=str(WORKSPACE),
            stdin_enabled=False,
        )
    )
    out, err = {"stdout": [], "stderr": []}, {"stderr": []}
    chunks = {"stdout": [], "stderr": []}
    async for kind, chunk in running.output():
        if kind in chunks:
            chunks[kind].append(chunk)
    code = await running.exit_code()
    print("exit:", code)
    print("stderr:", b"".join(chunks["stderr"]).decode(errors="replace")[:400])
    text = b"".join(chunks["stdout"]).decode(errors="replace").strip()
    print("guest matrix:")
    try:
        for key, value in sorted(json.loads(text).items()):
            print(f"  {key} = {value}")
    except ValueError:
        print("  <not json>", text[:400])
    # With SANLOCK_EVENT_TRACE=1 the supervisor's own decision trace lands on
    # the slot's stderr, which the drain keeps -- print the tail so the
    # mediator's answer for the execve notification is visible here too.
    handle = getattr(getattr(executor, "_instance", None), "_handle", None)
    if handle is not None and handle.stderr_drain is not None:
        print("slot stderr tail:")
        for line in handle.stderr_drain.text(6000).splitlines()[-40:]:
            print("  " + line)
    executor.close()


if __name__ == "__main__":
    asyncio.run(main())
