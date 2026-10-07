#!/usr/bin/env python3
"""Name every task in a sandbox's own cgroup -- which one appeared, and whose thread it is.

Creates one sandbox whose payload forks a child and holds both, then lists every
**thread** in that sandbox's own cgroup from inside the worker container
(`cgroup.threads` + `/proc/<tid>/status`), so the extra task can be named --
`sandlock-superv` thread, payload thread, or something else.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PAYLOAD = (
    "import os,time\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    time.sleep(60)\n"
    "    os._exit(0)\n"
    "open('/tmp/forked', 'w').write(str(pid))\n"
    "time.sleep(60)\n"
)

DUMP = r"""
set -e
d=$(ls -td /pod-cgroup/*/sbx_* 2>/dev/null | head -1)
[ -n "$d" ] || { echo "no sbx_* cgroup"; exit 1; }
echo "cgroup=$d"
echo "pids.current=$(cat $d/pids.current 2>/dev/null) cgroup.procs=$(tr '\n' ' ' < $d/cgroup.procs)"
for t in $(cat $d/cgroup.threads); do
  name=$(cat /proc/$t/comm 2>/dev/null || echo '?')
  tgid=$(awk '/^Tgid:/{print $2}' /proc/$t/status 2>/dev/null || echo '?')
  ppid=$(awk '/^PPid:/{print $2}' /proc/$t/status 2>/dev/null || echo '?')
  echo "  tid=$t comm=$name tgid=$tgid ppid=$ppid"
done
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--container-template",
        default="docker exec -i {node}-1 bash -lc",
        help="how to run a command inside a worker container; '{node}' is substituted",
    )
    parser.add_argument("--hold-s", type=float, default=6.0)
    args = parser.parse_args()

    from e2b import Sandbox

    api_url = os.environ["E2B_API_URL"]
    box = Sandbox.create(
        "base",
        api_url=api_url,
        sandbox_url=os.environ.get("E2B_SANDBOX_URL", api_url),
        api_key=os.environ["E2B_API_KEY"],
        timeout=120,
        metadata={"probe": "task-delta"},
    )
    try:
        box.commands.run(
            "cat > hold.py <<'PYEOF'\n" + PAYLOAD + "PYEOF\nnohup python3 hold.py >/dev/null 2>&1 & echo started",
            timeout=60,
        )
        time.sleep(args.hold_s)
        # The sandbox lives on one of the three workers; ask each until the
        # cgroup is there.
        for node in ("worker-1", "worker-2", "worker-3"):
            argv = args.container_template.format(node=node).split()
            out = subprocess.run(argv + [DUMP], capture_output=True, text=True)
            if out.returncode == 0 and "cgroup=" in out.stdout:
                print(f"[{node}] {box.sandbox_id}")
                print(out.stdout.rstrip())
                break
        else:
            print("sandbox cgroup not found on any worker")
            return 1
        return 0
    finally:
        box.kill()


if __name__ == "__main__":
    sys.exit(main())
