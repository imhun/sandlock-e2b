#!/usr/bin/env python3
"""Snapshot-create probe: **(b) does not move a snapshot create.**

``POST /sandboxes`` for a "plain" create is dominated by the two ``prepare`` /
``materialize`` halves, so the two-phase create (design §4.6, shipped in
``0.1.0-877``) takes ``min`` off that path. A **snapshot** create is a
different shape: Task B measured the copy itself at ~25 ms *per entry*
(648 ms for 22 entries, 5137 ms for 202), and the merge happens inside the
same ``materialize``. Re-ordering who waits for whom therefore buys a snapshot
create ~78 ms out of several seconds -- about 1.5% -- and this probe exists so
the next person does not have to re-derive that from the acceptance table.

It is also the production-shaped guard for the v1 landmine: ``_snapshots/<id>/fs``
is a copy of the tree **root**, so the merge must land in the sandbox's
``/workspace`` directly. The v1 shape merged it into ``<root>/workspace`` and
put every file one level too deep (``workspace/workspace/kept.txt``); the
``workspace/workspace/...`` read below is the 404 that catches it.

    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/snapshot_create_probe.py --n 3

Every sandbox and the snapshot are removed before it exits, so the fleet is
left as it was found.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--files",
        type=int,
        default=1,
        help=(
            "how many extra files to put in the snapshot (the copy is ~25 ms "
            "per entry, so a large --files is how you see the copy dominate)"
        ),
    )
    args = parser.parse_args()

    os.environ.setdefault("E2B_API_KEY", os.environ.get("E2B_API_KEY", ""))
    from e2b import Sandbox

    source = Sandbox.create(timeout=args.timeout)
    snapshot_id = None
    samples: list[float] = []
    try:
        # The exact shape the v1 landmine was about: the file lives at
        # ``workspace/kept.txt`` *inside the tree root*, not at the root itself.
        source.files.write("workspace/kept.txt", "kept\n")
        for i in range(max(0, args.files - 1)):
            source.files.write(f"workspace/filler-{i}.txt", f"filler {i}\n")

        started = time.monotonic()
        snapshot = source.create_snapshot()
        snapshot_id = snapshot.snapshot_id
        print(
            f"snapshot capture: {(time.monotonic() - started) * 1000:.0f} ms"
            f" -> {snapshot_id}",
            flush=True,
        )

        for i in range(args.n):
            started = time.monotonic()
            created = Sandbox.create(snapshot_id, timeout=args.timeout)
            samples.append((time.monotonic() - started) * 1000)
            try:
                kept = created.files.read("workspace/kept.txt")
                deep = None
                try:
                    created.files.read("workspace/workspace/kept.txt")
                    deep = "PRESENT (the v1 landmine)"
                except Exception as exc:  # noqa: BLE001 - the absence is the assertion
                    deep = f"{type(exc).__name__}"
                print(
                    f"create-from-snapshot {i + 1}/{args.n}: {samples[-1]:.0f} ms"
                    f" kept={kept!r} workspace/workspace={deep}",
                    flush=True,
                )
                if kept != "kept\n":
                    print(f"FAIL: workspace/kept.txt came back as {kept!r}")
                    return 1
                if "PRESENT" in deep:
                    print("FAIL: the snapshot landed one level too deep")
                    return 1
            finally:
                created.kill()
    finally:
        if snapshot_id is not None:
            try:
                Sandbox.delete_snapshot(snapshot_id)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the run
                print(f"warning: could not delete snapshot {snapshot_id}: {exc}")
        source.kill()

    print(
        f"METRIC create_from_snapshot p50_ms={_pct(samples, 0.5):.0f}"
        f" p95_ms={_pct(samples, 0.95):.0f}"
        f" mean_ms={statistics.fmean(samples):.0f} n={len(samples)}"
    )
    print(
        "NOTE a snapshot create is copy-bound (~25 ms per entry, measured by "
        "Task B); the two-phase create does not move it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
