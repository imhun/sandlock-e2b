#!/usr/bin/env python3
"""Operator repair for the fleet quota ledger (N78).

The global row (`e2b:quota:global`) had no reconciliation path: a release that
landed twice (a deleted episode whose 600 s marker had expired was the window)
decremented it again, and nothing said anything. Measured 2026-10-03 with an
empty fleet: memory -1024 / cpu -100 / disk -1024 / processes -256 -- one
sandbox's worth of *free capacity*, i.e. the over-selling direction.

This script shows the current rows next to the sum of the live records and, with
`--apply`, sets them to that sum (the store does it in one WATCH/MULTI, so a
concurrent `reserve` cannot be interleaved).

**Run it when the fleet is quiet.** A create that has reserved but not yet
written its record is invisible to the records' sum, so applying while one is in
flight would leave that reservation out of the ledger -- the same over-selling
direction this repairs. The default is a dry run; read the deltas before you
pass `--apply`.

Needs `E2B_REDIS_URL` (the one the control plane runs with). Without it there is
no shared ledger to reconcile -- the in-process registry cannot drift this way --
so the script refuses by name, exit code 2, instead of printing a comforting
zero. It is meant to run inside a control-plane pod (or anywhere the store is
reachable) with the same env the deployment uses; the image has no dependency on
this file.

Importing the module has no side effects (`main` runs only under `__main__`), so
both the argument defaults and the refusal are pinned by a unit test.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.redis_backend import create_redis_client


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the reconciled rows (default: dry run, print the deltas)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the plan as JSON instead of the operator-facing table",
    )
    return parser.parse_args(argv)


def _load(settings: Settings) -> SandboxRegistry:
    client = create_redis_client(settings.redis_url)
    return SandboxRegistry(settings, redis_client=client)


def build_plan(registry: SandboxRegistry) -> dict[str, Any]:
    """``{rows: {name: {dim: current}}, target: {...}, deltas: {...}}``."""
    deltas = registry.reconcile_global_ledger(dry_run=True)
    rows: dict[str, dict[str, int]] = {}
    for name in deltas:
        row = registry._quota_store.get(name) if registry._quota_store else {}
        rows[name] = {dim: int(row.get(dim, 0)) for dim in deltas[name]}
    target = {
        name: {dim: rows[name][dim] + delta for dim, delta in deltas[name].items()}
        for name in deltas
    }
    return {"rows": rows, "target": target, "deltas": deltas}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()
    if not settings.redis_url:
        print(
            "reconcile-global-quota: refusing to run -- E2B_REDIS_URL is not set, "
            "and the in-process ledger cannot drift this way (there is nothing "
            "shared to reconcile)",
            file=sys.stderr,
        )
        return 2

    registry = _load(settings)
    plan = build_plan(registry)
    if args.json:
        print(json.dumps(plan, sort_keys=True))
    else:
        for name, row in plan["rows"].items():
            print(f"row {name}: {row}")
            print(f"  target: {plan['target'][name]}")
            print(f"  delta : {plan['deltas'][name]}")
        if not any(any(d.values()) for d in plan["deltas"].values()):
            print("nothing to reconcile: every row already equals the records' sum")
            return 0
        print(
            "\n(the fleet must be quiet: an in-flight create is not in the "
            "records' sum yet)"
        )

    if not args.apply:
        print("dry run; re-run with --apply to write the target rows")
        return 0

    applied = registry.reconcile_global_ledger()
    for name, delta in applied.items():
        print(f"applied {name}: {delta}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the script runs on the cluster
    raise SystemExit(main())
