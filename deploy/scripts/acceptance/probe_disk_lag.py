#!/usr/bin/env python3
"""S4 (N14): how long until `diskUsed` converges, at a realistic tree count?

The disk gate in this deployment is "the *measured* number -> per-exec
``RLIMIT_FSIZE`` + the mediator's refusal of create-type entries" (there is no
quota-agent in the k8s manifest set, so XFS project quota is not the enforcing
layer here). The lag between a write and the platform's number is therefore the
window in which a sandbox can exceed its budget -- and it is the quantity S4
needs before the interruption-fed dirty-dir fast path
(``E2B_DISK_ENFORCE_DIRTY``) can be retired.

Two arms, one env var apart:

* ``E2B_DISK_ENFORCE_DIRTY=1`` (the shipped shape): the round asks each
  sandbox's mediator which directories changed and re-walks only those;
* ``=0``: every round walks every tree (the path S5 wants to be the only one).

Measured 2026-10-04 on ``0.1.0-982``, 4 trees (this deployment's ceiling: the
fleet CPU ledger is 400% and a sandbox books 100%), 4 MiB blob each:

| arm | min | median | max | byte-exact? |
|---|---|---|---|---|
| dirty=1 | 0.03 s | 0.39 s | 2.65 s | yes |
| dirty=0 | 0.05 s | 0.36 s | 3.19 s | yes |

so at this scale the walk is as fresh as the fast path. **What is still
missing** is the *cost* side on a file-heavy tree (the walk's own numbers:
~3.5 us/file, ~2.4 ms/dir, against a 1 s round budget): ``FILES=20000`` seeds
that shape but needs a longer window than a lazy run gives you -- see the
`--files` knob and keep the sandbox awake.

Needs ``E2B_API_URL`` + ``E2B_API_KEY``. ``N`` (trees), ``PAYLOAD_MIB`` and
``FILES`` come from the environment; the default N=4 is this deployment's
concurrency ceiling, not a round number.

**Keep-alive note (learned the hard way, 2026-10-04)**: a sandbox that sits
idle past ``E2B_IDLE_PAUSE_AFTER_S`` (300 s) is paused by the platform, and the
next ``commands.run`` answers 403-ish ``Code.FAILED_PRECONDITION: Sandbox is
paused: idle 302s``. Seeding several trees sequentially therefore stalls the
earlier ones -- this probe reconnects (``Sandbox.connect``, which resumes) right
before it uses a sandbox.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

from e2b import Sandbox

#: The platform's own definition (``priv_helpers.dir_size``, N31): file
#: ``st_size`` **plus** each directory's ``st_blocks * 512``. A file-only sum
#: matches until a directory gets big enough to matter -- measured 2026-10-04:
#: one 20 000-entry directory contributes 796 KiB, and the file-only truth read
#: it as "never converged" while the platform was already right.
TREE_SIZE = (
    "python3 -c \"import os;t='/home/user';f=0;d=0\n"
    "for r,_ds,fs in os.walk(t):\n"
    "    d += os.stat(r).st_blocks * 512\n"
    "    for n in fs:\n"
    "        f += os.stat(os.path.join(r, n)).st_size\n"
    "print(f + d)\""
)


def metric_bytes(sandbox: Sandbox) -> int:
    metric = sandbox.get_metrics()
    metric = metric[0] if isinstance(metric, list) else metric
    return int(metric.disk_used)


def _awake(sandbox: Sandbox) -> Sandbox:
    """Resume if the platform parked this sandbox while we worked elsewhere."""
    info = sandbox.get_info()
    if getattr(info, "state", None) == "paused":
        sandbox = Sandbox.connect(
            sandbox.sandbox_id,
            api_url=os.environ["E2B_API_URL"].rstrip("/"),
            sandbox_url=os.environ["E2B_API_URL"].rstrip("/"),
        )
    return sandbox


def main() -> int:
    api = os.environ["E2B_API_URL"].rstrip("/")
    count = int(os.environ.get("N", "4"))
    payload_mib = int(os.environ.get("PAYLOAD_MIB", "4"))
    files = int(os.environ.get("FILES", "0"))
    boxes: list[Sandbox] = []
    try:
        for _ in range(count):
            boxes.append(Sandbox.create(timeout=900, api_url=api, sandbox_url=api))
        print(f"created {len(boxes)} sandboxes (N={count})", flush=True)
        if files:
            for index, box in enumerate(boxes):
                box = _awake(box)
                boxes[index] = box
                box.commands.run(
                    "python3 -c \"import os;os.makedirs('/home/user/many',"
                    f"exist_ok=True);[open(f'/home/user/many/f{{i}}','w').write('x')"
                    f" for i in range({files})]\"",
                    timeout=900,
                )
            print(f"seeded {files} files per tree", flush=True)
        for index, box in enumerate(boxes):
            box = _awake(box)
            boxes[index] = box
            box.commands.run(
                f"head -c {payload_mib}M /dev/zero > /home/user/blob.bin", timeout=300
            )

        lags: list[float] = []
        for index, box in enumerate(boxes):
            box = _awake(box)
            boxes[index] = box
            truth = int(box.commands.run(TREE_SIZE, timeout=300).stdout.strip())
            started = time.monotonic()
            deadline = started + 120
            reported = metric_bytes(box)
            while reported != truth and time.monotonic() < deadline:
                time.sleep(0.25)
                reported = metric_bytes(box)
            lag = time.monotonic() - started
            lags.append(lag)
            print(
                f"  {box.sandbox_id}: truth={truth} reported={reported} "
                f"lag={lag:.2f}s",
                flush=True,
            )
            if reported != truth:
                print(
                    f"probe_disk_lag: FAIL -- {box.sandbox_id} never converged "
                    f"(off by {reported - truth} bytes after 120 s)",
                    file=sys.stderr,
                )
                return 1
        print(
            f"probe_disk_lag: OK -- lags min={min(lags):.2f}s "
            f"median={statistics.median(lags):.2f}s max={max(lags):.2f}s "
            f"over {len(lags)} trees, byte-exact"
        )
        return 0
    finally:
        for box in boxes:
            box.kill()


if __name__ == "__main__":  # pragma: no cover - the probe runs on the cluster
    raise SystemExit(main())
