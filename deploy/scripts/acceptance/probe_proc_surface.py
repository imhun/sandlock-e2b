#!/usr/bin/env python3
"""What a PID-namespace sandbox can read under `/proc` -- and whose pids it hands back (N85).

`procfs.rs` serves `/proc/<pid>/<read-only metadata>` **on behalf** of the
caller: the numeric path is read as a *sandbox-namespace* pid, translated to the
host pid, and the host file is opened with the supervisor's credentials and
injected as an fd (that is the `ON_BEHALF_READABLE_METADATA` whitelist, narrowed
by F5.3 to the caller's own process group). The *content* is the host file --
including `Pid:`, `PPid:` and `NSpid:`, which therefore carry the host/container
numbering rather than the sandbox's.

This probe prints that surface for one sandbox: `self`, the group leader, the
init, a non-group pid, and `listdir("/proc")`. What it is for:

* `Pid:` of a sandbox process should be the *sandbox* pid. If it is the host pid,
  the pidns numbering is exposed through this path (N85).
* `/proc/1` and `/proc/self` are expected to be refused and `listdir("/proc")`
  empty (that is what `probe_n79_proc_stat_denied.py` pins for `/proc/1`).

Usage (any E2B endpoint):

    export E2B_API_URL=... E2B_SANDBOX_URL=... E2B_API_KEY=...
    python deploy/scripts/acceptance/probe_proc_surface.py
"""

from __future__ import annotations

import argparse
import os
import sys

INNER = r'''
import errno, os


def show(path):
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError as exc:
        return "%s: %s (%s)" % (path, errno.errorcode.get(exc.errno, exc.errno), exc.strerror)
    keep = [
        line.strip()
        for line in text.splitlines()
        if line.startswith(("Name:", "Pid:", "PPid:", "NSpid:", "Uid:"))
    ]
    return "%s: %s" % (path, " | ".join(keep))


print("getpid=%d getppid=%d" % (os.getpid(), os.getppid()))
for path in (
    "/proc/self/status",
    "/proc/1/status",
    "/proc/2/status",
    "/proc/3/status",
    "/proc/1",
    "/proc/self",
    "/proc/self/cgroup",
):
    print(show(path))
print("/proc numeric entries:", sorted(e for e in os.listdir("/proc") if e.isdigit()))
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default="base")
    parser.add_argument("--timeout-s", default="180")
    args = parser.parse_args()

    from e2b import Sandbox

    api_url = os.environ["E2B_API_URL"]
    box = Sandbox.create(
        args.template,
        api_url=api_url,
        sandbox_url=os.environ.get("E2B_SANDBOX_URL", api_url),
        api_key=os.environ["E2B_API_KEY"],
        timeout=int(args.timeout_s),
        metadata={"probe": "proc-surface"},
    )
    try:
        out = box.commands.run(
            "cat > surface.py <<'PYEOF'\n" + INNER + "PYEOF\npython3 surface.py",
            timeout=120,
        )
        print(((out.stdout or "") + (out.stderr or "")).strip())
        print("sandbox_id:", box.sandbox_id)
        return 0
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
