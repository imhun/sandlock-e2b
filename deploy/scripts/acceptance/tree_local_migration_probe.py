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
    export E2B_INTERNAL_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
        -o jsonpath='{.data.E2B_INTERNAL_API_KEY}' | base64 -d)
    tmp/venv/bin/python deploy/scripts/acceptance/tree_local_migration_probe.py \
        --directions down --files 200 --prepare-source e2b-worker-1 \
        --stop-source e2b-worker-1

``--stop-source <worker pod>`` scales the worker StatefulSet down by one
(``replicas - 1``, which is what removes the highest ordinal --
``e2b-worker-1``) for the *down* leg and restores the previous replica count in
a ``finally``, waiting for the pods to come back. Without it the leg assumes the
operator has already taken that node's worker down. **The sandbox under test has
to be on the pod that disappears**; the probe checks that before it migrates
anything, and ``--prepare-source`` creates one (with ``--files`` files) and
migrates it onto that pod when the scheduler picked the other one -- a
StatefulSet scale-down can only remove the highest ordinal, so the pod has to
be named.
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


def _fleet_headers() -> dict[str, str]:
    """The fleet key for ``GET /internal/fleet/sandboxes`` (which node owns what).

    The public ``GET /sandboxes`` deliberately carries no ``nodeID`` (the SDK
    has no use for it), and impersonating a worker to ask the node-scoped
    endpoint is not something an operator outside the cluster can do -- that
    endpoint's docstring names this script as the reason the fleet view exists.
    """
    key = os.environ.get("E2B_INTERNAL_API_KEY", "")
    if not key:
        raise SystemExit(
            "E2B_INTERNAL_API_KEY is empty: the probe needs the fleet view "
            "(GET /internal/fleet/sandboxes) to attribute a sandbox to a node. "
            "Build it from the secret, never hard-code it."
        )
    return {"X-Internal-Key": key}


def _nodes_with_capacity(base: str, headers: dict[str, str]) -> list[str]:
    resp = httpx.get(f"{base}/nodes", headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    nodes = [
        entry
        for entry in (payload if isinstance(payload, list) else payload.get("nodes", []))
        if entry.get("status") == "healthy"
    ]
    return [entry["nodeID"] for entry in nodes]


def _kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", "-n", "sandlock", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _worker_replicas() -> int:
    raw = _kubectl(
        "get", "statefulset", "e2b-worker", "-o", "jsonpath={.spec.replicas}"
    ).strip()
    return int(raw)


def _scale_worker(replicas: int) -> None:
    _kubectl(
        "scale",
        "statefulset/e2b-worker",
        f"--replicas={replicas}",
    )


def _pod_names() -> list[str]:
    return _kubectl(
        "get", "pods", "-l", "app=e2b-worker", "-o", "jsonpath={.items[*].metadata.name}"
    ).split()


def _wait_for_pod_gone(pod: str, *, timeout_s: float = 120.0) -> float:
    """Wait until the pod is out of the API, polling tightly, and return how long.

    Tight (0.25 s) on purpose: the autoscaler's warm-pool floor
    (``E2B_AS_MIN_REPLICAS=2``) recreates a scaled-down worker within ~1 s
    (measured 2026-10-02: ``scaled up to warm-pool floor 1 -> 2``), so the
    "source is stopped" state is a window of about one second. ``--remove-node``
    exists as the deterministic backstop for the same refusal.
    """
    started = time.monotonic()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pod not in _pod_names():
            elapsed = time.monotonic() - started
            print(f"METRIC stop-source pod={pod} gone after {elapsed:.1f}s")
            return elapsed
        time.sleep(0.25)
    raise SystemExit(f"{pod} is still Running after {timeout_s}s; not running the down leg")


def _remove_node_row(base: str, headers: dict[str, str], node_id: str) -> None:
    """Drop the node's registry row (public API) -- the deterministic variant.

    The worker re-registers on its next heartbeat (~5 s), so this is a bounded,
    self-healing way to make the control plane's view of the source node
    vanish. It is the state a reaped/drained node looks like, and it exercises
    the same named refusal: a local-tree sandbox whose node is not in the
    registry has no reachable tree.

    (``SIGSTOP`` on the worker container's PID 1 would look like a hung-but-
    present node, and it does **not** work here: a process inside a PID
    namespace cannot stop the namespace's init -- measured 2026-10-02, the
    state stayed ``S`` and the app kept answering.)
    """
    # The node registry is **in-memory per control-plane replica** (only some
    # state goes through Redis), and both the delete and the worker's
    # re-registration land on whichever replica the Service picked. A handful of
    # quick deletes is what clears both replicas; the worker puts its row back
    # on the next heartbeat (~5 s), one replica at a time.
    statuses = [
        httpx.delete(f"{base}/nodes/{node_id}", headers=headers, timeout=30).status_code
        for _ in range(6)
    ]
    print(f"METRIC remove-node node={node_id} statuses={statuses}")
    if 204 not in statuses:
        raise SystemExit(f"could not remove the node row: {statuses}")
    print(f"METRIC remove-node row_after_this_replica={_node_row(base, headers, node_id)}")


def _wait_for_workers_running(count: int, *, timeout_s: float = 300.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        out = _kubectl(
            "get",
            "pods",
            "-l",
            "app=e2b-worker",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}={.status.phase} {end}",
        )
        pairs = dict(
            part.split("=", 1) for part in out.split() if "=" in part
        )
        if len(pairs) == count and all(v == "Running" for v in pairs.values()):
            print(f"METRIC restore workers={pairs}")
            return
        time.sleep(3)
    raise SystemExit(f"workers did not come back to {count} Running within {timeout_s}s")


def _sandbox_node(base: str, sandbox_id: str) -> str:
    view = httpx.get(
        f"{base}/internal/fleet/sandboxes", headers=_fleet_headers(), timeout=30
    ).json()["sandboxes"]
    for node_id, ids in view.items():
        if sandbox_id in ids:
            return node_id
    raise SystemExit(f"sandbox {sandbox_id} is not in the fleet view")


def _node_row(base: str, headers: dict[str, str], node_id: str) -> dict | None:
    for entry in httpx.get(f"{base}/nodes", headers=headers, timeout=30).json():
        if entry.get("nodeID") == node_id:
            return {
                "nodeID": entry["nodeID"],
                "status": entry.get("status"),
                "address": entry.get("address"),
            }
    return None


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
        origin = _sandbox_node(base, source.sandbox_id)
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
        print(f"METRIC up kept={kept!r} last={probe!r} files={args.files + 1}")
        if kept != "kept\n":
            print("FAIL up: the tree did not survive the migration")
            return 1
        return 0
    finally:
        source.kill()


def _prepare_source(
    Sandbox: Any, base: str, headers: dict[str, str], pod: str, args
) -> str:
    """Create a sandbox with content and make sure its **tree lands on ``pod``**.

    The down leg needs a tree on the worker that is about to disappear, and
    ``POST /sandboxes`` takes no placement hint -- so the probe creates one and
    then *migrates* it onto ``pod`` (the up leg's own mechanism) when the
    scheduler put it elsewhere. A StatefulSet scale-down can only remove the
    highest ordinal, which is why the pod is named explicitly.
    """
    source = Sandbox.create(timeout=args.timeout)
    try:
        source.files.write("workspace/kept.txt", "kept\n")
        for i in range(args.files):
            source.files.write(f"workspace/small-{i}.txt", f"{i}\n")
        origin = _sandbox_node(base, source.sandbox_id)
        if origin != pod:
            resp = _migrate(base, headers, source.sandbox_id, pod)
            if resp.status_code != 200:
                print(f"FAIL prepare: migrate to {pod} -> {resp.status_code} {resp.text}")
                raise SystemExit(1)
            origin = _sandbox_node(base, source.sandbox_id)
        kept = source.files.read("workspace/kept.txt")
        print(
            f"METRIC prepare sandbox={source.sandbox_id} node={origin} "
            f"files={args.files + 1} kept={kept!r}"
        )
        if origin != pod or kept != "kept\n":
            raise SystemExit("FAIL prepare: the tree is not on the requested pod")
        return source.sandbox_id
    except BaseException:
        source.kill()
        raise


def _leg_down(base: str, headers: dict[str, str], args) -> int:
    if not args.sandbox_id:
        print("SKIP down: pass --sandbox-id (a sandbox whose tree is on the down node)")
        return 0
    source = args.sandbox_id
    down_node = args.remove_node or args.stop_source
    target = args.target_node or next(
        node
        for node in _nodes_with_capacity(base, headers)
        if node != down_node
    )
    resp = _migrate(base, headers, source, target)
    message = resp.text
    print(f"METRIC down status={resp.status_code} target={target} body={message[:200]}")
    if resp.status_code != 502 or "source-node-unreachable" not in message:
        if resp.status_code == 200:
            print(
                "FAIL down: the migration SUCCEEDED -- the source node answered, "
                "so the 'stopped source' window had already closed (the "
                "autoscaler's warm-pool floor, E2B_AS_MIN_REPLICAS=2, recreates "
                "a scaled-down worker in ~1 s). Re-run with --remove-node for the "
                "deterministic form of the same refusal."
            )
        else:
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
        metavar="WORKER_POD",
        help=(
            "the worker pod whose node holds the down-leg sandbox (e.g. "
            "e2b-worker-1). The probe scales the StatefulSet to replicas-1 for "
            "the duration and restores it in a finally; only the highest "
            "ordinal can be removed this way -- and on this cluster the "
            "autoscaler's warm-pool floor (E2B_AS_MIN_REPLICAS=2) recreates the "
            "pod within ~1 s, so the probe polls every 0.25 s and issues the "
            "migration the moment the pod is gone (~2 s window; measured "
            "2026-10-02, 2/2). --remove-node is the deterministic variant."
        ),
    )
    parser.add_argument(
        "--remove-node",
        metavar="WORKER_POD",
        help=(
            "the deterministic variant of the same refusal: drop the source "
            "node's registry row (DELETE /nodes/<id>; the worker re-registers "
            "on its next heartbeat) and migrate immediately"
        ),
    )
    parser.add_argument("--sandbox-id", help="the sandbox the down leg migrates")
    parser.add_argument("--target-node", help="where the down leg tries to move it")
    parser.add_argument(
        "--prepare-source",
        metavar="WORKER_POD",
        help=(
            "create a sandbox with --files files and make sure its tree lands on "
            "this worker pod (migrating it there when the scheduler chose the "
            "other one), then use it for the down leg; it is killed before the "
            "probe returns"
        ),
    )
    args = parser.parse_args()

    base, headers = _api()
    from e2b import Sandbox

    failures = 0
    prepared: str | None = None
    if args.prepare_source:
        prepared = _prepare_source(Sandbox, base, headers, args.prepare_source, args)
        if not args.sandbox_id:
            args.sandbox_id = prepared
    if args.directions in ("down", "both") and not args.sandbox_id:
        parser.error("--sandbox-id (or --prepare-source) is required for the down leg")
    if args.directions in ("up", "both"):
        failures += _leg_up(Sandbox, base, headers, args)
    if args.directions in ("down", "both"):
        if args.remove_node:
            origin = _sandbox_node(base, args.sandbox_id)
            if origin != args.remove_node:
                print(
                    f"FAIL down: sandbox {args.sandbox_id} is on {origin}, but "
                    f"--remove-node is {args.remove_node}"
                )
                return 1
            args.target_node = args.target_node or next(
                node
                for node in _nodes_with_capacity(base, headers)
                if node != args.remove_node
            )
            _remove_node_row(base, headers, args.remove_node)
            failures += _leg_down(base, headers, args)
        elif args.stop_source:
            origin = _sandbox_node(base, args.sandbox_id)
            if origin != args.stop_source:
                print(
                    f"FAIL down: sandbox {args.sandbox_id} is on {origin}, but "
                    f"--stop-source is {args.stop_source}; stopping that pod "
                    "would not make the tree unreachable"
                )
                return 1
            # Pick the target *before* the window opens: the call has to go out
            # the moment the pod is gone (the autoscaler recreates it in ~1 s).
            args.target_node = args.target_node or next(
                node
                for node in _nodes_with_capacity(base, headers)
                if node != args.stop_source
            )
            original = _worker_replicas()
            _scale_worker(original - 1)
            try:
                _wait_for_pod_gone(args.stop_source)
                failures += _leg_down(base, headers, args)
            finally:
                _scale_worker(original)
                _wait_for_workers_running(original)
        else:
            failures += _leg_down(base, headers, args)

    if prepared:
        # The prepared sandbox is the probe's own droppings: kill it and let the
        # fleet converge (the down leg's tree may be on a worker that just came
        # back), so the residue line below is about the *fleet's* state.
        httpx.delete(
            f"{base}/sandboxes/{prepared}", headers=headers, timeout=120
        )
        for _ in range(20):
            fleet = httpx.get(
                f"{base}/internal/fleet/sandboxes", headers=_fleet_headers(), timeout=30
            ).json()["sandboxes"]
            if not any(fleet.values()):
                break
            time.sleep(3)
    residue = httpx.get(f"{base}/sandboxes", headers=headers, timeout=30).json()
    print(f"METRIC residue={[e['sandboxID'] for e in residue]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
