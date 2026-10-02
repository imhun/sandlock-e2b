#!/usr/bin/env python3
"""Snapshot-create probe: a snapshot create's cost, per tier.

``POST /sandboxes`` for a "plain" create is dominated by the two ``prepare`` /
``materialize`` halves, so the two-phase create (design §4.6, shipped in
``0.1.0-877``) takes ``min`` off that path. A **snapshot** create is a
different shape: Task B measured the copy itself at ~25 ms *per entry*
(648 ms for 22 entries, 5137 ms for 202), and the merge happens inside the
same ``materialize``. Re-ordering who waits for whom therefore buys a snapshot
create ~78 ms out of several seconds -- about 1.5% -- and this probe exists so
the next person does not have to re-derive that from the acceptance table.

It is also the **before/after instrument** for the tar task (Task 2): the copy
used to be an exploded ``fs/`` directory (one NFS round trip per entry) and is
now one ``fs.tar`` (one sequential file), so the same three tiers are run
twice and the two cost models are reported side by side:

* ``per_entry_ms`` -- the old shape's unit (and the number Task B measured);
* ``mb_per_s`` -- the new shape's unit, from the same reading.

``--files`` takes a comma-separated list, so one run covers all tiers:

    tmp/venv/bin/python deploy/scripts/acceptance/snapshot_create_probe.py \
        --files 1,40,202 --n 3

It is also the production-shaped guard for the v1 landmine: the payload
(``_snapshots/<id>/fs`` before Task 2, ``fs.tar`` after) is a copy of the tree
**root**, so it must land in the sandbox's ``/workspace`` directly. The v1
shape merged it into ``<root>/workspace`` and put every file one level too deep
(``workspace/workspace/kept.txt``); the ``workspace/workspace/...`` read below
is the 404 that catches it.

    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/snapshot_create_probe.py \
        --files 1,40,202 --n 3

Every sandbox and the snapshot are removed before it exits, so the fleet is
left as it was found. The payload itself is measured from the outside
(``du``/``stat`` on the shared store, see docs/create-local-first-design.md §7.6)
because the SDK never sees the store.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
from typing import Any


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def _tiers(raw: str) -> list[int]:
    """``--files`` as a list of tiers; one integer stays one tier."""
    tiers = [int(part) for part in raw.replace(",", " ").split()]
    if not tiers or any(tier < 1 for tier in tiers):
        raise argparse.ArgumentTypeError("--files needs at least one tier >= 1")
    return tiers


def _create_from_snapshot(
    Sandbox: Any, snapshot_id: str, *, timeout: int, retries: int
) -> tuple[Any, int]:
    """``Sandbox.create(snapshot_id)``, counting the disclosed transient.

    ``create_snapshot`` returning does not mean the *other* control-plane
    replica can see the record yet; for a few hundred ms its ``POST
    /sandboxes`` answers ``400: Template <id> not found`` (design §4.3, hit
    twice on 2026-10-02). That window is recorded -- one ``create_attempts=``
    per tier and a warning per retry -- rather than hidden, exactly like Task 1's
    ``local_first_pagecache_acceptance.py`` does; every other failure raises.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return Sandbox.create(snapshot_id, timeout=timeout), attempts
        except Exception as exc:  # noqa: BLE001 - only the named window is retried
            text = str(exc)
            named = "not found" in text and "Template" in text
            if not named or attempts > retries:
                raise
            print(f"  transient (attempt {attempts}): {text}", flush=True)
            time.sleep(0.5)


def _tier(
    Sandbox: Any, files: int, *, n: int, timeout: int, retries: int, keep: bool
) -> int:
    """One tier: source sandbox -> snapshot -> ``n`` creates from it."""
    # The payload is ``workspace/`` plus one file per entry: the tree root is
    # what gets copied, so the directory itself is an entry too.
    entries = files + 1
    source_bytes = len(b"kept\n") + sum(
        len(f"filler {i}\n".encode("utf-8")) for i in range(max(0, files - 1))
    )
    source = Sandbox.create(timeout=timeout)
    snapshot_id = None
    samples: list[float] = []
    attempts_total = 0
    try:
        # The exact shape the v1 landmine was about: the file lives at
        # ``workspace/kept.txt`` *inside the tree root*, not at the root itself.
        source.files.write("workspace/kept.txt", "kept\n")
        for i in range(max(0, files - 1)):
            source.files.write(f"workspace/filler-{i}.txt", f"filler {i}\n")

        started = time.monotonic()
        snapshot = source.create_snapshot()
        snapshot_id = snapshot.snapshot_id
        capture_ms = (time.monotonic() - started) * 1000
        print(
            f"tier files={files}: snapshot capture {capture_ms:.0f} ms"
            f" -> {snapshot_id}",
            flush=True,
        )

        for i in range(n):
            started = time.monotonic()
            created, attempts = _create_from_snapshot(
                Sandbox, snapshot_id, timeout=timeout, retries=retries
            )
            attempts_total += attempts
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
                    f"  create-from-snapshot {i + 1}/{n}: {samples[-1]:.0f} ms"
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
        if snapshot_id is not None and not keep:
            try:
                Sandbox.delete_snapshot(snapshot_id)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the run
                print(f"warning: could not delete snapshot {snapshot_id}: {exc}")
        elif snapshot_id is not None:
            print(f"KEPT snapshot {snapshot_id} (--keep: delete it by hand)")
        source.kill()

    p50_ms = _pct(samples, 0.5)
    print(
        f"METRIC tier files={files} entries={entries} source_bytes={source_bytes}"
        f" capture_ms={capture_ms:.0f}"
        f" create_p50_ms={p50_ms:.0f}"
        f" create_p95_ms={_pct(samples, 0.95):.0f}"
        f" create_mean_ms={statistics.fmean(samples):.0f} n={len(samples)}"
        f" create_attempts={attempts_total}"
        f" per_entry_ms={p50_ms / entries:.3f}"
        f" mb_per_s={source_bytes / 1e6 / (p50_ms / 1000):.3f}"
        f" capture_per_entry_ms={capture_ms / entries:.3f}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--create-retries",
        type=int,
        default=3,
        help=(
            "how many times to retry the *named* transient window (400 "
            "'Template <id> not found' right after create_snapshot); each one "
            "is printed and counted in METRIC create_attempts"
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help=(
            "leave each tier's snapshot behind (prints its id) so the payload "
            "size can be read off the store; delete it by hand afterwards"
        ),
    )
    parser.add_argument(
        "--files",
        type=_tiers,
        default=_tiers("1"),
        help=(
            "comma-separated tiers of files per snapshot (the copy is ~25 ms "
            "per entry, so a large tier is how you see the copy dominate); "
            "the Task 2 before/after run uses 1,40,202"
        ),
    )
    args = parser.parse_args()

    os.environ.setdefault("E2B_API_KEY", os.environ.get("E2B_API_KEY", ""))
    from e2b import Sandbox

    for files in args.files:
        if (
            _tier(
                Sandbox,
                files,
                n=args.n,
                timeout=args.timeout,
                retries=args.create_retries,
                keep=args.keep,
            )
            != 0
        ):
            return 1
    print(
        "NOTE a snapshot create is copy-bound: with the exploded fs/ payload "
        "the unit is per entry (~25 ms, Task B); with the one-file fs.tar "
        "payload the same bytes move as one sequential file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
