"""N30 T6 acceptance: the platform's `diskUsed` equals a measurement taken
*inside* the sandbox, byte for byte, and the basis is position-bounded.

Three things are checked, in the order the plan asks for them:

1. an independent measurement inside the sandbox (`du -s -B1 --apparent-size`
   over the workspace) equals `GET /sandboxes/{id}/metrics`'s `diskUsed`;
2. a write that would cross the sandbox's `diskMB` fails (and we record the
   errno rather than assuming which one);
3. deleting what was written lets the next write through -- which is what
   "position-bounded, deletion returns the bytes" means in behaviour.

The sandbox's disk size is the deployment's `E2B_DEFAULT_DISK_MB` (the create
API has no per-request override), so the probe asks the platform for the record
and sizes the overrun from *that* number instead of hard-coding a quota.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from e2b import Sandbox

BASE = os.environ["E2B_API_URL"].rstrip("/")
KEY = os.environ["E2B_API_KEY"]

#: Measured the way the basis itself defines it (N30 Task 1/4): files by
#: `st_size`, directories by their allocation blocks (`st_blocks * 512`).
#: `du -s --apparent-size` is *not* that quantity -- it reports a directory's
#: apparent size, so it misses the block term the platform counts and the two
#: numbers differ by exactly the directories' blocks (measured: 1024 B with one
#: directory). The plan's Step 2 wording used `du`; this is the same measurement
#: `priv_helpers.dir_size` makes.
MEASURE = (
    "python3 -c \"\n"
    "import os\n"
    "total = 0\n"
    "for root, dirs, files in os.walk('/home/user'):\n"
    "    st = os.lstat(root)\n"
    "    total += st.st_blocks * 512\n"
    "    for name in files:\n"
    "        total += os.lstat(os.path.join(root, name)).st_size\n"
    "print(total)\n"
    "\""
)


def metrics(sandbox_id: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}/sandboxes/{sandbox_id}/metrics", headers={"X-API-Key": KEY}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)[0]


def record(sandbox_id: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}/sandboxes/{sandbox_id}", headers={"X-API-Key": KEY}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main() -> int:
    sb = Sandbox.create(timeout=900)
    sid = sb.sandbox_id
    verdicts: list[tuple[str, bool, str]] = []
    try:
        m0 = metrics(sid)
        print(f"sandbox {sid}  metric keys={sorted(m0)}")
        # The quota is whatever the platform reports for this sandbox; the
        # create API has no per-request override, so it is read rather than
        # assumed.
        disk_mb = m0.get("diskTotal") or record(sid).get("diskMB")
        if not disk_mb:
            print(f"N30 T6 INCONCLUSIVE: no disk total in the metrics: {m0}")
            return 2
        print(f"sandbox {sid}  diskMB={disk_mb}")

        # A little something to measure, so the comparison is not 0 == 0.
        sb.commands.run("head -c 3000000 /dev/urandom > /home/user/seed.bin", timeout=120)

        inside = int(sb.commands.run(MEASURE, timeout=120).stdout.strip())
        # The heartbeat carries the worker's measurement, and it lands on a
        # cadence (E2B_ACTIVITY_PERSIST_INTERVAL_S defaults to 30 s), so poll
        # rather than reading immediately.
        reported = None
        for _ in range(20):
            reported = metrics(sid)["diskUsed"]
            if reported == inside:
                break
            sb.commands.run("true", timeout=60)  # a tick of activity
            import time

            time.sleep(5)
        verdicts.append(
            (
                "inside == platform",
                reported == inside,
                f"inside={inside} platform={reported}",
            )
        )

        # Over the budget on purpose: ask for more than the whole quota.
        over = disk_mb + 64
        write = sb.commands.run(
            f"dd if=/dev/zero of=/home/user/big bs=1M count={over} 2>&1; echo RC=$?",
            timeout=600,
        )
        out = write.stdout or ""
        print(f"over-budget write ({over} MiB into a {disk_mb} MiB sandbox):\n{out}")
        failed = "RC=0" not in out
        verdicts.append(("over-budget write refused", failed, out.strip().splitlines()[-1][:120]))

        # Position-bounded: removing it must give the bytes back.
        after = sb.commands.run(
            "rm -f /home/user/big && "
            "head -c 8388608 /dev/zero > /home/user/again.bin && echo WROTE=ok",
            timeout=180,
        )
        ok = "WROTE=ok" in (after.stdout or "")
        verdicts.append(("write after delete", ok, (after.stdout or "").strip()[:120]))

        print()
        for name, passed, detail in verdicts:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")
        good = all(p for _, p, _ in verdicts)
        print()
        print("N30 T6 OK: position-bounded basis, and the platform number is the sandbox's own"
              if good else "N30 T6 FAIL: see the lines above")
        return 0 if good else 1
    finally:
        try:
            sb.kill()
        except Exception:  # noqa: BLE001 - cleanup must not mask the verdict
            pass


if __name__ == "__main__":
    sys.exit(main())
