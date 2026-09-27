#!/usr/bin/env python3
"""O1/T1 re-measurement on the live fleet: who owns a file the sandbox writes?

The pre-fleet measurement (HANDOFF, T1) was taken on overlayfs, where a file the
sandbox wrote came out owned by host uid 0 even though the sandbox's host uid was
20000 -- which is why "the sandbox chmods its own file" and "the 1777+sticky
shared volume" could not be asserted there. The fleet's shared volume is not
overlayfs (it is a NAS: `nfs4`, verified with `df -T` inside a worker pod), so the
answer has to be re-measured rather than carried over -- that is the whole point
of O1.

What this prints, and why each line is needed:

  * inside the sandbox: `id` (its host uid as the sandbox sees it), `pwd`, the
    `stat` of the file it wrote, and the exit code of `chmod 600` on that file
    (EPERM here is the T1 failure mode);
  * on the host: the same file's owner/group/mode as seen from the worker pod,
    which is what decides whether per-uid isolation holds on this volume.

Credentials come from the environment (E2B_API_URL / E2B_API_KEY) and are never
printed. Modes: `--keep` leaves the sandbox up for a human to poke at.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

MARKER = "t1-ownership.txt"
WORKER_PODS = ("e2b-worker-0", "e2b-worker-1")
TREE_ROOT = "/var/lib/e2b-sandboxes/workspaces"

IN_SANDBOX = f"""
set -u
echo "PWD $(pwd)"
echo "ID $(id -u):$(id -g)"
printf 't1\\n' > {MARKER}
echo "WROTE $(pwd)/{MARKER}"
stat -c 'INSIDE owner=%u:%g mode=%a size=%s' {MARKER}
if chmod 600 {MARKER} 2>/dev/null; then echo "CHMOD rc=0"; else echo "CHMOD rc=$?"; fi
stat -c 'AFTER owner=%u:%g mode=%a' {MARKER}
echo "REL {MARKER}"
"""


def _kubectl(args: list[str], kubeconfig: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, KUBECONFIG=kubeconfig)
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, check=False, env=env
    )


def _host_side_stat(rel_path: str, sandbox_id: str, kubeconfig: str) -> None:
    """stat the file from the worker pod that owns this sandbox."""
    for pod in WORKER_PODS:
        probe = _kubectl(
            [
                "-n",
                "sandlock",
                "exec",
                pod,
                "--",
                "sh",
                "-c",
                f"ls -d {TREE_ROOT}/{sandbox_id} 2>/dev/null && "
                f"find {TREE_ROOT}/{sandbox_id} -maxdepth 6 -name {MARKER} "
                "-exec stat -c 'HOST owner=%u:%g mode=%a %n' {} \\;",
            ],
            kubeconfig,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            print(f"HOST-POD {pod}")
            print(probe.stdout.rstrip())
            return
        if probe.stderr.strip():
            print(f"HOST-POD {pod} stderr: {probe.stderr.strip().splitlines()[-1]}")
    print("HOST-FIND MISSING (file not visible from either worker pod)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the sandbox up")
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="create this many sandboxes at once (does the pool hand out distinct uids?)",
    )
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG", ""),
        help="read the host side through this kubeconfig (skip if empty)",
    )
    args = parser.parse_args()

    api_url = os.environ.get("E2B_API_URL")
    api_key = os.environ.get("E2B_API_KEY")
    missing = [n for n, v in (("E2B_API_URL", api_url), ("E2B_API_KEY", api_key)) if not v]
    if missing:
        print(f"VACUOUS: missing {', '.join(missing)} (never printed)")
        return 2

    from e2b import Sandbox

    print(f"API {api_url}")
    sandboxes = [Sandbox.create(timeout=600) for _ in range(max(1, args.count))]
    try:
        for sandbox in sandboxes:
            _one(sandbox, args)
    finally:
        if args.keep:
            print(f"KEPT {[s.sandbox_id for s in sandboxes]} (delete them yourself)")
            return 0
        for sandbox in sandboxes:
            print(f"KILLED {sandbox.sandbox_id} rc={sandbox.kill()}")
    return 0


def _one(sandbox, args: argparse.Namespace) -> None:
    sandbox_id = sandbox.sandbox_id
    print(f"SANDBOX {sandbox_id}")
    try:
        result = sandbox.commands.run(f"sh -c {shlex.quote(IN_SANDBOX)}")
        print("--- in sandbox stdout ---")
        print((result.stdout or "").rstrip())
        if result.stderr and result.stderr.strip():
            print("--- in sandbox stderr ---")
            print(result.stderr.rstrip())
        print(f"EXIT {result.exit_code}")
        if args.kubeconfig:
            _host_side_stat(MARKER, sandbox_id, args.kubeconfig)
    except Exception as exc:  # noqa: BLE001 - report and keep going
        print(f"SANDBOX {sandbox_id} FAILED {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
