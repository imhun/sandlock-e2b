#!/usr/bin/env python3
"""Sample this container's page cache while a workload runs (Task 1 Step 1 ③).

The point of interest is ``memory.stat:file`` -- the page cache the kernel
charges to *this* cgroup -- because that is what a full-tree copy spends, and
because it is charged to whoever wrote the bytes: the agent's ``maint`` face
(512 MiB) does the create-path materialization, the worker (4 GiB today) does
the snapshot copy. One uninterrupted 500 MB local write already OOM-killed
``maint`` once (``docs/create-local-first-layout.md`` §3.2); this probe measures
the real shape so the cap in the design document is a number, not a guess.

It must run **inside** the container whose cgroup is being measured (the cgroup
is whichever one the process is in, so being in the pod is not enough). Run it
in the foreground and drive the workload from another shell::

    kubectl -n sandlock exec -i e2b-worker-0 -c worker -- \
        python3 - --seconds 45 --label worker-snapshot \
        < deploy/scripts/acceptance/local_first_pagecache_probe.py

``--json-out DIR`` also writes the same summary to ``DIR/pagecache-<label>.json``
(a node-local directory survives the container's own exit).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

CG = Path("/sys/fs/cgroup")


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_stat() -> dict[str, int]:
    stats: dict[str, int] = {}
    try:
        raw = (CG / "memory.stat").read_text()
    except OSError:
        return stats
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().lstrip("-").isdigit():
            stats[key] = int(value)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--label", default="container")
    parser.add_argument("--json-out", default=None, help="directory for the summary json")
    parser.add_argument("--quiet", action="store_true", help="no per-sample lines on stdout")
    args = parser.parse_args()

    peak_file = 0
    peak_anon = 0
    peak_current = 0
    peak_kernel = 0
    samples = 0
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        stats = read_stat()
        current = read_int(CG / "memory.current") or 0
        file_bytes = stats.get("file", 0)
        anon_bytes = stats.get("anon", 0)
        peak_file = max(peak_file, file_bytes)
        peak_anon = max(peak_anon, anon_bytes)
        peak_kernel = max(peak_kernel, stats.get("kernel", 0))
        peak_current = max(peak_current, current)
        samples += 1
        if not args.quiet:
            print(json.dumps({"t": round(time.monotonic(), 3), "current": current, "file": file_bytes, "anon": anon_bytes}), flush=True)
        time.sleep(args.interval)

    summary = {
        "label": args.label,
        "samples": samples,
        "seconds": args.seconds,
        "interval_s": args.interval,
        "memory_max_bytes": read_int(CG / "memory.max"),
        "memory_peak_file_bytes": peak_file,
        "memory_peak_anon_bytes": peak_anon,
        "memory_peak_kernel_bytes": peak_kernel,
        "memory_peak_current_bytes": peak_current,
        "cgroup_memory_peak_bytes": read_int(CG / "memory.peak"),
    }
    print("SUMMARY " + json.dumps(summary), flush=True)
    if args.json_out:
        out = Path(args.json_out)
        out.mkdir(parents=True, exist_ok=True)
        (out / ("pagecache-%s.json" % args.label)).write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
