"""A/B of the fix on the live cluster (N25): os.stat vs entry_size.

Runs inside both worker pods while a sandbox rewrites an 800 MiB file in a loop
(so the writeback never stops) and reports, for the file being written and for
the whole accounting walk of that sandbox's tree:

* `os.stat(file)` -- what the accounting used to ask;
* `brief_stat.entry_size(file)` -- what it asks now;
* `priv_helpers.dir_size(tree)` -- the fallback walk, end to end.

The point is the "before" number, not the "after" one: it has to still be
~1400 ms on the writer's node, otherwise the run is not reproducing anything.
"""

import json
import os
import subprocess
import sys
import time

from e2b import Sandbox

KUBECONFIG = os.environ["KUBECONFIG"]
PODS = ["e2b-worker-0", "e2b-worker-1"]
MIB = 1024 * 1024

SAMPLER = r'''
import json, os, sys, time
sys.path.insert(0, "/app")
from envd_service.priv_helpers import dir_size
from envd_service.runtime.brief_stat import entry_size

root, blob, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
out = open("/tmp/sampler.jsonl", "w", buffering=1)
t0 = time.time()

def timed(fn):
    a = time.monotonic()
    try:
        val = fn()
        err = ""
    except OSError as exc:
        val = None
        err = type(exc).__name__
    return round((time.monotonic() - a) * 1000, 2), val, err

while time.time() - t0 < seconds:
    os_ms, os_size, os_err = timed(lambda: os.stat(blob).st_size)
    es_ms, es_size, es_err = timed(lambda: entry_size(blob))
    walk_ms, walk_size, walk_err = timed(lambda: dir_size(root))
    out.write(json.dumps({"t": round(time.time(), 3),
                          "os_ms": os_ms, "os_size": os_size, "os_err": os_err,
                          "entry_ms": es_ms, "entry_size": es_size, "entry_err": es_err,
                          "walk_ms": walk_ms, "walk_size": walk_size, "walk_err": walk_err}) + "\n")
    time.sleep(0.2)
'''


def kubectl(*args: str, timeout: int = 180) -> str:
    proc = subprocess.run(
        ["kubectl", "-n", "sandlock", *args],
        capture_output=True,
        text=True,
        env=dict(os.environ, KUBECONFIG=KUBECONFIG),
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl {args}: {proc.stderr.strip()}")
    return proc.stdout


def sh_in_pod(pod: str, script: str) -> str:
    return kubectl("exec", pod, "-c", "worker", "--", "sh", "-c", script)


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    sb = Sandbox.create(timeout=1800)
    try:
        name = sb.sandbox_id
        root = f"/var/lib/e2b-sandboxes/{name}"
        handle = sb.commands.run(
            "for i in $(seq 1 200); do "
            "dd if=/dev/zero of=/home/user/loop.bin bs=1M count=800 status=none; "
            "done",
            background=True,
        )
        time.sleep(6)
        blob = sh_in_pod(
            PODS[0],
            f"find /var/lib/e2b-sandboxes -maxdepth 4 -name loop.bin -path '*{name}*' | head -1",
        ).strip()
        print(f"sandbox {name}; host path {blob}", flush=True)

        for pod in PODS:
            sh_in_pod(
                pod,
                f"cat > /tmp/sampler.py <<'PYEOF'\n{SAMPLER}\nPYEOF\n"
                f"rm -f /tmp/sampler.jsonl\n"
                f"nohup python3 /tmp/sampler.py {root} {blob} {seconds} >/tmp/sampler.out 2>&1 &\necho started",
            )
        time.sleep(seconds + 3)

        for pod in PODS:
            raw = sh_in_pod(pod, "cat /tmp/sampler.jsonl 2>/dev/null")
            rows = [json.loads(x) for x in raw.splitlines() if x.strip().startswith("{")]
            err = sh_in_pod(pod, "cat /tmp/sampler.out 2>/dev/null").strip()
            print(f"\n== {pod} ({len(rows)} rounds)")
            if err:
                print(f"  stderr: {err[:400]}")
            for row in rows[:5]:
                print(f"  t={row['t'] - rows[0]['t']:+6.1f}s "
                      f"os.stat={row['os_ms']:9.2f}ms entry_size={row['entry_ms']:8.2f}ms "
                      f"dir_size(walk)={row['walk_ms']:9.2f}ms "
                      f"sizes={row['os_size']}/{row['entry_size']}/{row['walk_size']}")
            for key in ("os_ms", "entry_ms", "walk_ms"):
                vals = sorted(r[key] for r in rows)
                if vals:
                    print(f"  {key:10s} p50={vals[len(vals) // 2]:9.2f}ms max={vals[-1]:9.2f}ms")
            sizes = {(r["os_size"], r["entry_size"], r["walk_size"]) for r in rows}
            print(f"  (os.stat, entry_size, dir_size) triples seen: {sorted(sizes)[-3:]}")
        handle.kill()
    finally:
        sb.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
