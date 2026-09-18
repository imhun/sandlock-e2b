#!/usr/bin/env python3
"""Measure the *real* heartbeat gap at the control plane, from its access log.

``E2B_NODE_HEARTBEAT_TIMEOUT`` decides when a node is treated as gone -- and a
"gone" node has its live sandboxes reaped as orphans (E6.1), which takes their
route-B slots away. So the window is not a tuning knob, it is the bound on how
long a live worker's heartbeat may be late. Two things make that bound
non-obvious:

* the worker sends heartbeats from the *same coroutine* that runs a reconcile
  round (``envd_service/agent.py::NodeAgent._loop``), so the gap is
  ``interval + round duration`` -- not the interval;
* the round duration scales with the number of trees on the shared base.

This script turns "300s felt right" into a number: it reads the control plane's
access log (one line per heartbeat, and the only place the arrival times exist)
and prints the gap distribution per node, next to the window actually configured.

Usage:

    KUBECONFIG=... python deploy/scripts/heartbeat_gaps.py            # last 30m
    KUBECONFIG=... python deploy/scripts/heartbeat_gaps.py --since 12h

It exits non-zero if any gap reached the configured window: that is a node the
control plane would have marked unhealthy *while it was alive*.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import re
import statistics
import subprocess
import sys

#: ``2026-09-18T10:42:24.371601603+08:00 INFO:  10.244.140.40:42194 -
#: "POST /internal/nodes/<id>/heartbeat HTTP/1.1" 204 No Content``
_LINE = re.compile(
    r'^(?P<ts>\S+) .*"POST /internal/nodes/(?P<node>[^/]+)/heartbeat HTTP/1\.1" (?P<code>\d+)'
)

#: The worker's own cadence (``await asyncio.sleep(5)`` in ``NodeAgent._loop``).
INTERVAL_S = 5.0


def _kubectl(args: list[str]) -> str:
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def _control_plane_pod(namespace: str, deployment: str) -> str:
    out = _kubectl(
        [
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            f"app={deployment}",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    ).strip()
    if not out:
        raise SystemExit(f"no Running pod for {deployment} in {namespace}")
    return out


def _configured_window(namespace: str, deployment: str) -> float | None:
    """``E2B_NODE_HEARTBEAT_TIMEOUT`` as deployed, or None if it is not set.

    Read from the live Deployment rather than from a manifest: the whole point is
    to compare the gap against what this cluster is *actually* enforcing.
    """
    out = _kubectl(
        [
            "-n",
            namespace,
            "get",
            "deploy",
            deployment,
            "-o",
            "jsonpath={.spec.template.spec.containers[*].env[?(@.name=='E2B_NODE_HEARTBEAT_TIMEOUT')].value}",
        ]
    ).strip()
    # ``kubectl set env`` writes to *every* container of the pod template, so the
    # filter can come back with more than one value (the control plane plus its
    # buildkit sidecar). They are meant to be identical; read the first and let
    # a mismatch be visible rather than fatal.
    values = out.split()
    if not values:
        return None
    if len(set(values)) > 1:
        print(f"WARNING: containers disagree about the window: {values}", file=sys.stderr)
    return float(values[0])


def _heartbeats(log_text: str) -> dict[str, list[dt.datetime]]:
    per_node: dict[str, list[dt.datetime]] = collections.defaultdict(list)
    for raw in log_text.splitlines():
        match = _LINE.match(raw)
        if not match:
            continue
        # 404 means the control plane had lost the node and the worker is about
        # to re-register: its gap says nothing about the liveness window.
        if match["code"] != "204":
            continue
        # Nanosecond precision; the kubelet stamps every line.
        stamp = dt.datetime.strptime(match["ts"][:26], "%Y-%m-%dT%H:%M:%S.%f")
        per_node[match["node"]].append(stamp)
    return per_node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="sandlock")
    parser.add_argument("--deployment", default="control-plane")
    parser.add_argument(
        "--since",
        default="30m",
        help="kubectl --since window (the access log holds the whole pod lifetime)",
    )
    parser.add_argument(
        "--min-window",
        type=float,
        default=3.0,
        help="headroom, in heartbeat intervals, on top of the worst observed gap",
    )
    args = parser.parse_args()

    pod = _control_plane_pod(args.namespace, args.deployment)
    window = _configured_window(args.namespace, args.deployment)
    per_node = _heartbeats(
        _kubectl(
            [
                "-n",
                args.namespace,
                "logs",
                pod,
                "-c",
                "control-plane",
                "--timestamps",
                f"--since={args.since}",
            ]
        )
    )
    if not per_node:
        raise SystemExit(f"no heartbeat lines in {pod} for --since={args.since}")

    print(
        f"control plane {pod}: {sum(len(v) for v in per_node.values())} heartbeat(s) "
        f"over --since={args.since}"
    )
    print(
        f"configured window: "
        f"{f'{window:g}s' if window is not None else 'unset (code default 15s)'}"
    )

    worst = 0.0
    worst_round = 0.0
    breached: list[str] = []
    for node, times in sorted(per_node.items()):
        times.sort()
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        if not gaps:
            print(f"\n{node}: only one heartbeat -- widen --since")
            continue
        ordered = sorted(gaps, reverse=True)
        worst = max(worst, ordered[0])
        print(f"\n{node}")
        print(
            f"  beats={len(times)}  median={statistics.median(gaps):.2f}s  "
            f"p95={ordered[int(0.05 * len(ordered))]:.2f}s  "
            f"p99={ordered[int(0.01 * len(ordered))]:.2f}s  max={ordered[0]:.2f}s"
        )
        # The loop is `heartbeat -> (maybe) reconcile round -> sleep(5)`, so the
        # part of the gap above the interval is the round this worker ran.
        print(f"  implied worst reconcile round: {max(ordered[0] - INTERVAL_S, 0.0):.2f}s")
        print("  worst 5 gaps (when they ended):")
        for gap in ordered[:5]:
            index = gaps.index(gap)
            print(f"    {gap:7.2f}s  {times[index + 1]:%m-%d %H:%M:%S}")
        if window is not None and ordered[0] >= window:
            breached.append(node)
        elif window is not None and ordered[0] >= 0.5 * window:
            print(
                f"  NOTE: worst gap is {ordered[0] / window:.0%} of the configured "
                f"window -- headroom is thin"
            )
        worst_round = max(worst_round, max(ordered[0] - INTERVAL_S, 0.0))

    # The window has to hold: the interval the worker sleeps, the round it may be
    # running when the next beat is due, and slack for scheduling and network.
    recommended = worst * args.min_window + worst_round
    print(
        f"\nworst gap across nodes: {worst:.2f}s"
        f" (of which up to {worst_round:.2f}s is a reconcile round)"
        f"\n  ->  {recommended:.0f}s covers this sample with {args.min_window:g}x headroom."
    )
    print(
        "  Caution: a round's duration scales with the number of trees on the shared\n"
        "  base, so this sample bounds it only for the base it was taken on. Re-run\n"
        "  after the base grows, and before lowering the window."
    )
    if breached:
        print(
            "\nFAIL: these nodes went quiet for at least the whole window, so the "
            f"control plane called them unhealthy while they were alive: {breached}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
