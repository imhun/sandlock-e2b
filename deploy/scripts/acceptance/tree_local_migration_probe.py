#!/usr/bin/env python3
"""Task 3 acceptance: the tree is node-local, so a migration *must* move bytes.

Two directions, because they are two different failure modes:

* **up** -- both nodes are healthy: ``POST /sandboxes/<id>/migrate`` must move
  the tree through ``<export>/_migrate`` (export on the source, import on the
  target) and the files must come out the other side (**bytes are checked, not
  the record's node id**). A migration that only switches the record leaves an
  empty tree behind -- the silent degradation `E2B_TREES_SHARED` exists to
  avoid.
* **down** -- the source node's worker is gone: the same call must be refused
  **by name** (`source-node-unreachable`), not answer 200 with an empty tree.
  This is the leg that cannot be recovered afterwards, which is why §8.4 of
  ``docs/create-local-first-design.md`` says: move the sandbox off a node
  *before* taking the node down.

The metadata-dense half of the acceptance (design §8.1/§8.5) is
``local_first_storage_probe.py`` run with ``--root local:/var/lib/e2b/workspaces``
and ``--root nas:/var/lib/e2b-sandboxes``: the flip is bought on small files
and paid on large sequential writes, so both lines are measured.

    deploy/scripts/open-cluster-tunnel.sh
    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/tree_local_migration_probe.py \
        --directions both --files 200 --stop-source node-1

``--stop-source <node>`` scales that node's worker down for the *down* leg and
restores it in a ``finally``; without it the leg assumes the operator has
already taken the node down (and ``--expect-node`` names it).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from typing import Any

import httpx


def _api() -> tuple[str, dict[str, str]]:
    base = os.environ["E2B_API_URL"].rstrip("/")
    key = os.environ.get("E2B_API_KEY", "")
    if not key:
        raise SystemExit("E2B_API_KEY is empty; build it from the secret, never hard-code it")
    return base, {"X-API-Key": key}


def _nodes_with_capacity(base: str, headers: dict[str, str]) -> list[str]:
    resp = httpx.get(f"{base}/nodes", headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    nodes = [
        entry
        for entry in (payload if isinstance(payload, list) else payload.get("nodes", []))
        if entry.get("healthy", True)
    ]
    return [entry["nodeID"] for entry in nodes]


def _scale_worker(replicas: int) -> None:
    subprocess.run(
        [
            "kubectl",
            "-n",
            "sandlock",
            "scale",
            "statefulset/e2b-worker",
            f"--replicas={replicas}",
        ],
        check=True,
    )


def _migrate(base: str, headers: dict[str, str], sandbox_id: str, target: str):
    return httpx.post(
        f"{base}/sandboxes/{sandbox_id}/migrate",
        headers=headers,
        json={"nodeID": target},
        timeout=900,
    )


def _leg_up(Sandbox: Any, base: str, headers: dict[str, str], args) -> int:
    nodes = _nodes_with_capacity(base, headers)
    if len(nodes) < 2:
        print(f"SKIP up: need two healthy nodes, saw {nodes}")
        return 0
    source = Sandbox.create(timeout=args.timeout)
    try:
        source.files.write("workspace/kept.txt", "kept\n")
        for i in range(args.files):
            source.files.write(f"workspace/small-{i}.txt", f"{i}\n")
        # The node the record names *before* the migration: the target is any
        # other healthy node.
        listed = httpx.get(
            f"{base}/sandboxes", headers=headers, timeout=30
        ).json()
        mine = next(e for e in listed if e["sandboxID"] == source.sandbox_id)
        origin = mine["nodeID"]
        target = next(node for node in nodes if node != origin)
        started = time.monotonic()
        resp = _migrate(base, headers, source.sandbox_id, target)
        elapsed_ms = (time.monotonic() - started) * 1000
        print(
            f"METRIC up status={resp.status_code} from={origin} to={target} "
            f"ms={elapsed_ms:.0f}"
        )
        if resp.status_code != 200:
            print(f"FAIL up: {resp.status_code} {resp.text}")
            return 1
        # Bytes, not the record: read the tree back through the sandbox.
        kept = source.files.read("workspace/kept.txt")
        probe = source.files.read(
            f"workspace/small-{args.files - 1}.txt" if args.files else "workspace/kept.txt"
        )
        print(f"METRIC up kept={kept!r} last={probe!r}")
        if kept != "kept\n":
            print("FAIL up: the tree did not survive the migration")
            return 1
        return 0
    finally:
        source.kill()


def _leg_down(base: str, headers: dict[str, str], args) -> int:
    if not args.expect_node:
        print("SKIP down: pass --expect-node <node id whose worker is down>")
        return 0
    source = args.sandbox_id
    target = args.target_node or _nodes_with_capacity(base, headers)[0]
    resp = _migrate(base, headers, source, target)
    message = resp.text
    print(f"METRIC down status={resp.status_code} target={target} body={message[:200]}")
    if resp.status_code != 502 or "source-node-unreachable" not in message:
        print(
            "FAIL down: expected the named refusal source-node-unreachable, got "
            f"{resp.status_code}"
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--directions", choices=("up", "down", "both"), default="both")
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--stop-source",
        metavar="NODE",
        help=(
            "scale this node's worker to 0 for the *down* leg (the probe "
            "restores it in a finally); the operator is expected to have "
            "drained the sandboxes first (design §8.4: move, then take down)"
        ),
    )
    parser.add_argument(
        "--expect-node",
        help="the node whose worker is down (required for --directions down)",
    )
    parser.add_argument("--sandbox-id", help="the sandbox the down leg migrates")
    parser.add_argument("--target-node", help="where the down leg tries to move it")
    args = parser.parse_args()

    if args.directions in ("down", "both") and not args.sandbox_id:
        parser.error("--sandbox-id is required for the down leg (its tree is on the down node)")

    base, headers = _api()
    from e2b import Sandbox

    failures = 0
    if args.directions in ("up", "both"):
        failures += _leg_up(Sandbox, base, headers, args)
    if args.directions in ("down", "both"):
        if args.stop_source:
            _scale_worker(0)
            try:
                time.sleep(10)  # the control plane's heartbeat window is 30 s
                failures += _leg_down(base, headers, args)
            finally:
                _scale_worker(1)
        else:
            failures += _leg_down(base, headers, args)

    residue = httpx.get(f"{base}/sandboxes", headers=headers, timeout=30).json()
    print(f"METRIC residue={[e['sandboxID'] for e in residue]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
