#!/usr/bin/env python3
"""Local-vs-NAS storage probe for the local-first sandbox tree (Task 1 Step 1 ⓪/①).

This is the promoted form of the inline heredoc in
``docs/create-local-first-layout.md`` §3.1 (the 200 x 64 B small-file bench) plus
the sequential-write shape that produced the *older* "local 186 / NAS 381 MB/s"
reading this task had to retire. It exists so the three block sizes of Step 1 ⓪
are one command on any writable root, and so the 1 GB sequential write of Step 1
① can be run on the shared NAS tree and on the worker's node-local disk with the
*same* code.

Both numbers matter because they pull in opposite directions: the NAS mount is
slow per *operation* (one synchronous RPC per create, 3-4 orders of magnitude
above local) and fast per *byte* (its back end is a 10 PiB share, not a 100 GB
node disk). Any argument that "the tree should be local" has to carry both.

Two shapes are measured, and neither is faked:

* **small files** -- 200 files of ``--small-bytes`` bytes, open+write+close each
  (this is the ``npm install`` / dependency-tree shape);
* **sequential write** -- ``--seq-mb`` MiB written in 8 MiB chunks with an
  ``fsync`` every ``--fsync-every-mb`` MiB, ``--repeat`` times per size. The
  periodic fsync is not decoration: an uninterrupted 500 MB local write once
  OOM-killed the agent's ``maint`` container (512 MiB), because dirty page
  cache is charged to the writer's cgroup. Keeping the dirty window bounded is
  what makes the reading about the disk instead of about the cgroup.

Run it where the roots are visible. The worker pod sees both mounts::

    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec -i e2b-worker-0 -- python3 - \
        --root "nas:/var/lib/e2b-sandboxes/workspaces" \
        --root "local:/var/lib/e2b-images" \
        < deploy/scripts/acceptance/local_first_storage_probe.py

Inside a sandbox the tree is the bind mount of ``<workspace base>/<id>``, so the
same script measures the sandbox's ``/workspace`` (see
``deploy/scripts/acceptance/local_first_sequential_write_probe.py`` for the
API-driven wrapper). Everything it creates is ``_lfbench.<pid>`` under the given
root and is removed before it exits.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

CHUNK = 8 << 20


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile; ``q`` is 0–100 (the convention every sibling
    probe in this directory uses).

    This used to read ``round(q * (len - 1))`` while the callers passed ``50``,
    so every "p50" it printed was the **maximum** of the samples. It is called
    out here because the reading it produced (a local 64 MiB write at
    1017 MB/s) is the burst value, not the median -- the exact confusion this
    probe exists to settle.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    k = int(round(q / 100.0 * (len(ordered) - 1)))
    return ordered[min(len(ordered) - 1, max(0, k))]


def parse_root(spec: str) -> tuple[str, Path]:
    label, _, raw = spec.partition(":")
    if not label or not raw:
        raise argparse.ArgumentTypeError("--root wants LABEL:DIR (found %r)" % spec)
    return label, Path(raw)


def small_files(root: Path, n: int, size: int) -> dict[str, object]:
    d = root / ("_lfbench.%d.small" % os.getpid())
    d.mkdir(parents=True, exist_ok=True)
    payload = b"x" * size
    try:
        t0 = time.perf_counter()
        for i in range(n):
            with open(d / ("f%04d" % i), "wb") as fh:
                fh.write(payload)
        total_ms = (time.perf_counter() - t0) * 1000.0
        t0 = time.perf_counter()
        shutil.rmtree(d)
        rmtree_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {
        "op": "small_files",
        "files": n,
        "bytes_each": size,
        "total_ms": round(total_ms, 3),
        "per_file_ms": round(total_ms / n, 4),
        "files_per_s": round(n / (total_ms / 1000.0), 1),
        "rmtree_ms": round(rmtree_ms, 3),
    }


def seq_write(
    root: Path,
    mb: int,
    repeat: int,
    fsync_every_mb: int,
    log_chunks: bool = False,
    settle_s: float = 0.0,
) -> dict[str, object]:
    d = root / ("_lfbench.%d.seq" % os.getpid())
    d.mkdir(parents=True, exist_ok=True)
    n_chunks = mb * (1 << 20) // CHUNK
    fsync_every = max(1, fsync_every_mb * (1 << 20) // CHUNK)
    blob = b"c" * CHUNK
    samples: list[float] = []
    chunk_samples: list[float] = []
    waits: list[float] = []
    need = mb * (1 << 20)
    try:
        for run in range(repeat):
            path = d / ("bulk.%d.%d.bin" % (os.getpid(), run))
            started = time.perf_counter()
            window_start = started
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                for i in range(n_chunks):
                    os.write(fd, blob)
                    if (i + 1) % fsync_every == 0:
                        os.fsync(fd)
                        if log_chunks:
                            now = time.perf_counter()
                            chunk_samples.append(
                                fsync_every_mb / (now - window_start)
                            )
                            window_start = now
                os.fsync(fd)
            finally:
                os.close(fd)
            seconds = time.perf_counter() - started
            samples.append(seconds)
            os.unlink(path)
            if settle_s:
                # A quota'd NFS mount does not free a large unlinked file
                # instantly: measured 2026-10-02, ``df`` inside a sandbox still
                # showed 501M used after ``rm`` + ``sync`` and only 4.0K two
                # seconds later. Without this wait the next run is refused with
                # EFBIG and the series silently becomes "one run".
                wait_started = time.perf_counter()
                deadline = wait_started + settle_s
                while time.perf_counter() < deadline:
                    st = os.statvfs(str(root))
                    if st.f_bavail * st.f_frsize >= need:
                        break
                    time.sleep(0.25)
                waits.append(time.perf_counter() - wait_started)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    mbps = [mb / s for s in samples]
    # The median *rate* and the median *time* are medians of two different
    # arrays, so they are not each other's reciprocal. Report the median rate
    # together with the seconds of the run that produced it, so a table can
    # quote one run instead of two.
    paired = sorted(zip(mbps, samples), key=lambda pair: pair[0])
    median_run = paired[min(len(paired) - 1, max(0, int(round(0.5 * (len(paired) - 1)))))]
    out: dict[str, object] = {
        "op": "seq_write",
        "mb": mb,
        "repeat": repeat,
        "fsync_every_mb": fsync_every_mb,
        "seconds_p50": round(pct(samples, 50), 4),
        "mbps_p50": round(pct(mbps, 50), 1),
        "mbps_p50_run_seconds": round(median_run[1], 4),
        "mbps_min": round(min(mbps), 1),
        "mbps_max": round(max(mbps), 1),
        "mbps_mean": round(statistics.fmean(mbps), 1),
        "mbps_all": [round(v, 1) for v in mbps],
    }
    if log_chunks:
        out["window_mbps"] = [round(v, 1) for v in chunk_samples]
    if waits:
        out["settle_s_max"] = round(max(waits), 3)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", action="append", type=parse_root, required=True,
                        metavar="LABEL:DIR", help="repeatable; e.g. nas:/var/lib/e2b-sandboxes/workspaces")
    parser.add_argument("--seq-mb", default="64,256,1024",
                        help="comma-separated sequential sizes in MiB")
    parser.add_argument("--repeat", type=int, default=10, help="runs per size")
    parser.add_argument("--fsync-every-mb", type=int, default=64)
    parser.add_argument(
        "--chunk-log",
        action="store_true",
        help="record the rate of every fsync window (shows the local burst/plateau)",
    )
    parser.add_argument(
        "--settle-s",
        type=float,
        default=0.0,
        help="after each unlink, wait (up to this long) for the quota'd mount to report the space free",
    )
    parser.add_argument("--small-n", type=int, default=200)
    parser.add_argument("--small-bytes", type=int, default=64)
    parser.add_argument("--skip-small", action="store_true")
    parser.add_argument("--skip-seq", action="store_true")
    args = parser.parse_args()

    sizes = [int(v) for v in args.seq_mb.split(",") if v.strip()]
    out: dict[str, object] = {"pid": os.getpid(), "roots": {}, "argv": sys.argv[1:]}
    for label, root in args.root:
        root.mkdir(parents=True, exist_ok=True)
        probe: dict[str, object] = {"path": str(root), "small": None, "seq": []}
        out["roots"][label] = probe  # type: ignore[assignment]
        if not args.skip_small:
            probe["small"] = small_files(root, args.small_n, args.small_bytes)
            print(json.dumps({"root": label, **probe["small"]}), flush=True)
        if not args.skip_seq:
            for mb in sizes:
                row = seq_write(
                    root, mb, args.repeat, args.fsync_every_mb, args.chunk_log, args.settle_s
                )
                probe["seq"].append(row)  # type: ignore[union-attr]
                print(json.dumps({"root": label, **row}), flush=True)
    print(json.dumps({"summary": out}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
