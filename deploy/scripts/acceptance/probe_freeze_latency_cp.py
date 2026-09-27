"""Freeze latency without polluting the worker.

The earlier probes polled with `files.write`, i.e. an exec through the worker,
every 0.4 s -- and that (not the accounting) is what made a scan round look like
1.7 s. Here the only thing watched is the control plane's own record of the
sandbox, which is a cheap GET and does not exec anything in the sandbox.

Reported: the elapsed time from "the writer starts" to "the record says paused",
and what the platform had measured at that moment.
"""

import os
import time

import httpx
from e2b import Sandbox

API = os.environ["E2B_API_URL"]
KEY = os.environ["E2B_API_KEY"]
MIB = 1024 * 1024

sb = Sandbox.create(timeout=1800)
try:
    handle = sb.commands.run(
        "for i in 1 2 3; do "
        "dd if=/dev/zero of=/home/user/part$i.bin bs=1M count=900 status=none; "
        "done",
        background=True,
    )
    started = time.monotonic()
    paused_at = None
    line = None
    with httpx.Client(timeout=10) as client:
        while time.monotonic() - started < 60:
            logs = client.get(
                f"{API}/sandboxes/{sb.sandbox_id}/logs",
                headers={"X-API-Key": KEY},
            ).json()
            hits = [e["line"] for e in logs if "paused:" in e.get("line", "")]
            if hits:
                paused_at = time.monotonic() - started
                line = hits[-1]
                break
            time.sleep(0.2)

    if paused_at is None:
        print("NOT paused within 60s")
        raise SystemExit(1)
    metric = sb.get_metrics()
    metric = metric[0] if isinstance(metric, list) else metric
    print(f"frozen {paused_at:.1f}s after the writer started")
    print(f"  {line}")
    print(f"  diskUsed at that moment: {metric.disk_used / MIB:.0f} MiB")

    Sandbox.connect(sb.sandbox_id)
    handle.kill()
finally:
    sb.kill()
