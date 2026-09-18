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

A gap that overlaps a restart (a container start, or a re-registration in the
log) is reported but not counted: the node really was down, so the window is not
what is being tested there. Everything else that reaches the configured window
is a failure and the script exits non-zero -- that is a node the control plane
would have marked unhealthy *while it was alive*.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import statistics
import subprocess
import sys

#: ``2026-09-18T10:42:24.371601603+08:00 INFO:  10.244.140.40:42194 -
#: "POST /internal/nodes/<id>/heartbeat HTTP/1.1" 204 No Content``
_LINE = re.compile(
    r'^(?P<ts>\S+) .*"POST /internal/nodes/(?P<node>[^/]+)/heartbeat HTTP/1\.1" (?P<code>\d+)'
)

#: A worker that restarted registers before it beats again. The line carries no
#: node id, so it is used as a *time marker* (see ``_overlaps_restart``).
_REGISTER = re.compile(r'^(?P<ts>\S+) .*"POST /internal/nodes/register HTTP/1\.1" 200')

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


def _heartbeats(
    log_text: str,
) -> tuple[dict[str, list[dt.datetime]], list[dt.datetime]]:
    """Heartbeats per node, plus every registration timestamp.

    Registrations are not attributable to a node from the access log alone (the
    path has no id), but they are exactly what a (re)starting worker emits, so
    they are used as time markers when classifying a gap.
    """
    per_node: dict[str, list[dt.datetime]] = collections.defaultdict(list)
    registrations: list[dt.datetime] = []
    for raw in log_text.splitlines():
        registering = _REGISTER.match(raw)
        if registering:
            registrations.append(dt.datetime.fromisoformat(registering["ts"]))
            continue
        match = _LINE.match(raw)
        if not match:
            continue
        # 404 means the control plane had lost the node and the worker is about
        # to re-register: its gap says nothing about the liveness window.
        if match["code"] != "204":
            continue
        # The kubelet stamps every line with nanosecond precision and the pod's
        # UTC offset; keep both (timezone-aware) so these can be compared with
        # the API's UTC timestamps below.
        stamp = dt.datetime.fromisoformat(match["ts"])
        per_node[match["node"]].append(stamp)
    return per_node, registrations


def _pod_started_at(namespace: str) -> dict[str, list[dt.datetime]]:
    """Container start times per pod, for the "was it actually down?" question.

    A worker's node id *is* its pod name, so a gap that overlaps a container
    start is a restart -- the node really was gone, and the control plane was
    right to call it unhealthy. Without this, every rollout shows up as a
    violation of the window and the check becomes noise.
    """
    out = _kubectl(
        ["-n", namespace, "get", "pods", "-o", "json"]
    )
    try:
        pods = json.loads(out).get("items") or []
    except (ValueError, AttributeError):
        return {}
    starts: dict[str, list[dt.datetime]] = {}
    for pod in pods:
        name = (pod.get("metadata") or {}).get("name")
        if not name:
            continue
        stamps: list[dt.datetime] = []
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            raw = ((status.get("state") or {}).get("running") or {}).get(
                "startedAt"
            )
            if not raw:
                continue
            try:
                # ``startedAt`` is RFC3339 in UTC and usually has *no*
                # fractional part at all -- slicing to a fixed width silently
                # dropped every sample (and with it every restart) until this
                # was parsed properly.
                stamps.append(dt.datetime.fromisoformat(raw))
            except ValueError:
                continue
        if stamps:
            starts[name] = stamps
    return starts


def _overlaps_restart(
    node: str,
    previous: dt.datetime,
    current: dt.datetime,
    starts: dict[str, list[dt.datetime]],
    registrations: list[dt.datetime],
) -> bool:
    """Whether the node was *down* during this gap rather than up and silent.

    Two sources, because neither is complete on its own: the pod's container
    start time (which only remembers the *latest* incarnation, so a pod that
    restarted twice inside one window is invisible) and a registration line in
    the control-plane log (which has no node id, but a restart is the only thing
    that produces one).
    """
    if any(previous <= stamp <= current for stamp in starts.get(node, [])):
        return True
    return any(previous <= stamp <= current for stamp in registrations)


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
    per_node, registrations = _heartbeats(
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
    starts = _pod_started_at(args.namespace)
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
    worst_restart_gap = 0.0
    breached: list[str] = []
    restarts = 0
    for node, times in sorted(per_node.items()):
        times.sort()
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        if not gaps:
            print(f"\n{node}: only one heartbeat -- widen --since")
            continue
        # A gap that overlaps a container start is a restart, not a node that
        # was up and silent: the window is not what is being measured there.
        classified = [
            (
                gap,
                _overlaps_restart(
                    node, times[index], times[index + 1], starts, registrations
                ),
            )
            for index, gap in enumerate(gaps)
        ]
        live = [gap for gap, restarted in classified if not restarted]
        restarts += sum(1 for _, restarted in classified if restarted)
        worst_restart_gap = max(
            worst_restart_gap,
            max((gap for gap, restarted in classified if restarted), default=0.0),
        )
        ordered = sorted(gaps, reverse=True)
        worst_live = max(live, default=0.0)
        worst = max(worst, worst_live)
        print(f"\n{node}")
        print(
            f"  beats={len(times)}  median={statistics.median(gaps):.2f}s  "
            f"p95={ordered[int(0.05 * len(ordered))]:.2f}s  "
            f"p99={ordered[int(0.01 * len(ordered))]:.2f}s  "
            f"max={worst_live:.2f}s"
        )
        # The loop is `heartbeat -> (maybe) reconcile round -> sleep(5)`, so the
        # part of the gap above the interval is the round this worker ran.
        print(
            "  implied worst reconcile round: "
            f"{max(worst_live - INTERVAL_S, 0.0):.2f}s"
        )
        print("  worst 5 gaps (when they ended):")
        for gap in ordered[:5]:
            index = gaps.index(gap)
            restarted = _overlaps_restart(
                node, times[index], times[index + 1], starts, registrations
            )
            print(
                f"    {gap:7.2f}s  {times[index + 1]:%m-%d %H:%M:%S}"
                f"{'   (pod restart)' if restarted else ''}"
            )
        if window is not None and worst_live >= window:
            breached.append(node)
        elif window is not None and worst_live >= 0.5 * window:
            print(
                f"  NOTE: worst gap is {worst_live / window:.0%} of the configured "
                f"window -- headroom is thin"
            )
        worst_round = max(worst_round, max(worst_live - INTERVAL_S, 0.0))

    # The window has to hold: the interval the worker sleeps, the round it may be
    # running when the next beat is due, and slack for scheduling and network.
    recommended = worst * args.min_window + worst_round
    print(
        f"\nworst gap across nodes: {worst:.2f}s"
        f" (of which up to {worst_round:.2f}s is a reconcile round)"
        f"\n  ->  {recommended:.0f}s covers this sample with {args.min_window:g}x headroom."
    )
    if restarts:
        print(
            f"  {restarts} gap(s) overlapped a restart and were not counted"
            f" (largest {worst_restart_gap:.2f}s): the node really was down."
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
