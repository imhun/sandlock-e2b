#!/usr/bin/env python3
"""N13: two workers over one shared base must not touch each other's sandboxes.

``E2B_WORKSPACE_BASE`` is shared by every worker replica, so each pod's reconcile
walks trees it did not create. The mechanism that keeps that safe is in
``envd_service/agent.py``: before it deletes anything the reconciling worker asks
the control plane for the *fleet-wide* sandbox set, deletes only what nobody owns
(``deletable = candidates - fleet_owned``), leaves the rest alone
(``protected_elsewhere``) -- and defers the whole disk sweep when the fleet view
cannot be established. This script is the evidence that it holds on a real
cluster, which is what ``docs/task-backlog.md`` N13 was waiting for.

What it does:

1. creates N sandboxes and checks they landed on >= 2 workers;
2. checks no host uid was leased to two workers at once (the uid pool's flock is
   what makes that impossible, and it only works on storage whose locks are
   shared -- NFSv3 + ``nolock`` is single-node only);
3. restarts one worker, which forces a startup reconcile over a base that the
   *other* worker is holding live sandboxes on;
4. asserts every sandbox still runs and still reads the file it wrote, that the
   restarted worker reports ``deleted=0``, and reports how many trees it protected
   as ``protected_elsewhere``;
5. kills them all and checks both workers' reservations return to zero.

Usage (needs the cluster's kubectl to restart a worker and read its log):

    E2B_API_URL=http://127.0.0.1:49983 \\
    E2B_SANDBOX_URL=http://127.0.0.1:49983 \\
    E2B_API_KEY=... E2B_INTERNAL_API_KEY=... \\
    python deploy/scripts/multiworker_interference.py

``--no-restart`` skips phases 3-4 (and needs no kubectl): useful when the caller
arranges the reconcile itself.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import httpx
from e2b import Sandbox

SANDBOX_COUNT = 4

#: Per-call deadline for the sandbox file/command calls. Generous on purpose: a
#: worker that is running a reconcile pass over the shared base is busy for tens
#: of seconds (the resolver's own notes measure that walk at 24s idle and 43s
#: under load), and this test is about interference, not latency. A worker that is
#: genuinely broken still fails -- just after the longer deadline.
REQUEST_TIMEOUT_S = 180.0


def _internal(url: str, key: str) -> dict:
    resp = httpx.get(f"{url}/internal/nodes", headers={"X-Internal-Key": key}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fleet(url: str, key: str) -> dict[str, dict]:
    return {node["nodeID"]: node for node in _internal(url, key)}


def _healthy(fleet: dict[str, dict]) -> dict[str, dict]:
    return {
        node_id: node
        for node_id, node in fleet.items()
        if node["status"] == "healthy" and not node["draining"]
    }


def _placeable(fleet: dict[str, dict], namespace: str, deployment: str) -> dict[str, dict]:
    """Healthy nodes that a *running pod* actually backs.

    A worker's node id is its pod name, so a restarted worker leaves a record for
    the previous incarnation behind. With the wide orphan window
    (``E2B_NODE_HEARTBEAT_TIMEOUT``) that record can still read ``healthy`` for
    minutes, while the control plane's placement freshness window already refuses
    to send it new sandboxes. A test that only counted ``healthy`` would either
    wait for nothing or create on a fleet of one.
    """
    pods = set(_worker_pods(namespace, deployment))
    return {
        node_id: node for node_id, node in _healthy(fleet).items() if node_id in pods
    }


def _await_placeable(
    api: str, internal: str, namespace: str, deployment: str, want: int, timeout: float = 300
) -> dict[str, dict]:
    deadline = time.time() + timeout
    while True:
        placeable = _placeable(_fleet(api, internal), namespace, deployment)
        if len(placeable) >= want:
            return placeable
        if time.time() > deadline:
            raise AssertionError(
                f"only {len(placeable)} worker(s) with a running pod after {timeout:.0f}s: "
                f"{sorted(placeable)}"
            )
        time.sleep(5)


def _kubectl(args: list[str], namespace: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "-n", namespace, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _worker_pods(namespace: str, deployment: str) -> list[str]:
    """Worker pods that are Running and *not* being deleted.

    `kubectl get pods` still lists a pod that is Terminating (its phase stays
    `Running`, so `--field-selector=status.phase=Running` does not exclude it), and
    a rollout therefore leaves two entries for one worker for a while. Counting
    those made `_placeable` believe a fleet was ready while the *replacement* pod
    was still in `PodInitializing` -- which is how an earlier run walked into a
    half-started worker.

    The fields are separated by `|` rather than spaces because jsonpath renders an
    unset `deletionTimestamp` as *nothing at all*: with a space separator the line
    for a healthy pod collapses to two fields and every pod is discarded (which is
    exactly how an earlier revision of this script silently probed an empty
    fleet). Pod names cannot contain `|`, so the split is unambiguous.
    """
    out = _kubectl(
        [
            "get",
            "pods",
            "-l",
            f"app={deployment}",
            "-o",
            r'jsonpath={range .items[*]}{.metadata.name}|{.metadata.deletionTimestamp}|{.status.phase}{"\n"}{end}',
        ],
        namespace,
    ).stdout or ""
    pods: list[str] = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, deleting, phase = parts
        if not deleting and phase == "Running":
            pods.append(name)
    return pods


def _exec_in_any_worker(namespace: str, deployment: str, script: str) -> str:
    """Run ``script`` in a worker that answers.

    A rolling update leaves the pod we just deleted in the list for a while, and
    ``kubectl exec`` against it fails -- so try every worker and return the first
    non-empty answer instead of trusting the first name.
    """
    for pod in _worker_pods(namespace, deployment):
        result = _kubectl(
            ["exec", pod, "-c", "worker", "--", "sh", "-c", script], namespace
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    return ""


def _summaries_since(namespace: str, deployment: str, since: float) -> list[str]:
    """Reconcile summaries each worker logged at or after ``since`` (epoch seconds).

    The window matters: a pod's log tail also contains the rounds from *earlier*
    runs, and a previous run's legitimate cleanup of its own sandboxes shows up as
    ``deleted=N``. Only this test's rounds may be judged.
    """
    # Compare the RFC3339 prefix as text: both sides carry the same UTC offset, so
    # lexicographic order is chronological order, and no fractional-second or
    # timezone parsing can fail on us (the kubelet writes nanosecond precision).
    since_text = datetime.fromtimestamp(since).strftime("%Y-%m-%dT%H:%M:%S")
    out: list[str] = []
    for pod in _worker_pods(namespace, deployment):
        log = _kubectl(["logs", pod, "--tail=2000", "--timestamps"], namespace)
        for line in (log.stdout or "").splitlines():
            if "reconcile summary" not in line:
                continue
            stamp = line.split(" ", 1)[0]
            if stamp[:19] >= since_text:
                out.append(line.split(" ", 1)[1])
    return out


def _retry(what: str, fn, attempts: int = 3, delay: float = 5.0):
    """Retry a sandbox call that timed out, and *say* that it did.

    The cluster is deliberately churned by this script (phase 3 restarts a
    worker), so a file op can be slow while a fresh worker warms its caches. A
    retry keeps the interference assertions meaningful; silently retrying would
    hide the slowness, so every attempt after the first is printed.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the point is to cope with a timeout
            last = exc
            if attempt < attempts:
                print(f"NOTE: {what} failed ({type(exc).__name__}), retrying in {delay:.0f}s")
                time.sleep(delay)
    raise AssertionError(f"{what} failed {attempts} times: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="sandlock")
    parser.add_argument("--deployment", default="e2b-worker")
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="skip the reconcile trigger (no kubectl needed)",
    )
    args = parser.parse_args()

    api = os.environ["E2B_API_URL"]
    internal = os.environ["E2B_INTERNAL_API_KEY"]
    os.environ.setdefault("E2B_API_KEY", "local-key")

    # The fleet must have >= 2 workers that a running pod actually backs, or the
    # spread assertion below cannot mean anything (see `_placeable`).
    if not args.no_restart:
        _await_placeable(api, internal, args.namespace, args.deployment, want=2)
        print("OK: 2 workers healthy and pod-backed")
        # A worker that just (re)started warms its image caches and reconciles the
        # shared base; creating sandboxes into that costs the first file ops
        # seconds. Let the fleet settle so the assertions below measure
        # interference rather than startup noise.
        print("== letting the fleet settle (20s)")
        time.sleep(20)

    sandboxes: list[Sandbox] = []
    try:
        # 1. Spread across workers, and leave a file behind on each.
        for index in range(SANDBOX_COUNT):
            sandboxes.append(Sandbox.create())
        time.sleep(1)
        routes = {}
        for sb in sandboxes:
            resp = httpx.get(
                f"{api}/internal/routes/{sb.sandbox_id}",
                headers={"X-Internal-Key": internal},
                timeout=15,
            )
            resp.raise_for_status()
            routes[sb.sandbox_id] = resp.json()["nodeID"]
        by_node: dict[str, list[str]] = {}
        for sandbox_id, node_id in routes.items():
            by_node.setdefault(node_id, []).append(sandbox_id)
        print("SANDBOX DISTRIBUTION:", {k[-8:]: len(v) for k, v in by_node.items()})
        assert len(by_node) >= 2, "the fleet must have >= 2 healthy workers to test N13"

        for index, sb in enumerate(sandboxes):
            _retry(
                f"writing the marker into {sb.sandbox_id}",
                lambda sb=sb, index=index: sb.files.write(
                    f"workspace/interference-{index}.txt",
                    f"marker-{index}\n",
                    request_timeout=REQUEST_TIMEOUT_S,
                ),
            )
        print(f"OK: {len(sandboxes)} sandboxes spread over {len(by_node)} workers")

        # 2. The uid pool is what keeps two replicas from handing out the same
        #    host uid; that only works if the shared base's flock is cross-node.
        if not args.no_restart:
            # Read each sandbox tree's owner straight off the shared base: the
            # per-sandbox host uid *is* the allocator's answer, and the tree keeps
            # it for its whole life -- unlike the lease lines, which roll out of a
            # pod's log tail. Two replicas must never hand out the same uid, which
            # is exactly what the allocator's flock on `<base>/.uid_pool.lock` is
            # for (and why the shared storage's locks have to be real: NFSv3 with
            # `nolock` is single-node only).
            owners = _exec_in_any_worker(
                args.namespace,
                args.deployment,
                'for d in /var/lib/e2b-sandboxes/sbx_*; do stat -c "%u %n" "$d"; done',
            )
            uid_of: dict[str, int] = {}
            for line in (owners or "").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].startswith("/"):
                    uid_of[parts[1].rsplit("/", 1)[-1]] = int(parts[0])
            ours = {sb.sandbox_id: uid_of.get(sb.sandbox_id) for sb in sandboxes}
            assert all(uid is not None for uid in ours.values()), (
                f"a sandbox tree had no owner on disk: {ours}"
            )
            assert len(set(ours.values())) == len(ours), (
                f"two sandboxes were given the same host uid: {ours}"
            )
            assert set(ours.values()).isdisjoint({0, 65534}), (
                f"a sandbox was given the worker's own identity: {ours}"
            )
            print(f"OK: distinct pooled host uids, none of them the worker's: {sorted(ours.values())}")

            # 3. Restart one worker. A restarted worker comes up with an empty
            #    in-memory registry (its node id *is* its pod name, so the
            #    replacement registers as a new node), which means every tree on
            #    the shared base -- including the ones the other worker is still
            #    running -- looks unowned to it until it asks the fleet.
            existing = _kubectl(
                [
                    "get",
                    "pods",
                    "-l",
                    f"app={args.deployment}",
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                args.namespace,
            ).stdout.split()
            assert existing, "no worker pod found to restart"
            target_pod = existing[0]
            print(f"== restarting {target_pod} (forces a startup reconcile)")
            restart_at = time.time()
            _kubectl(["delete", "pod", target_pod, "--wait=false"], args.namespace)

            _await_placeable(api, internal, args.namespace, args.deployment, want=2)
            print("OK: both workers healthy and pod-backed again")

        # 4a. N13's claim: the reconcile of one worker must not remove anyone's
        #     tree. Asserted on the shared volume itself, through a running worker,
        #     because a sandbox on the restarted node cannot be asked (4c).
        listing = _exec_in_any_worker(
            args.namespace,
            args.deployment,
            "ls -d /var/lib/e2b-sandboxes/sbx_* 2>/dev/null",
        )
        on_disk = {line.rsplit("/", 1)[-1] for line in (listing or "").split()}
        for sb in sandboxes:
            assert sb.sandbox_id in on_disk, (
                f"{sb.sandbox_id} was removed from the shared base during the "
                f"reconcile (on disk: {sorted(on_disk)})"
            )
        print(f"OK: all {len(sandboxes)} trees still on the shared base (no cross-deletion)")

        # 4b. Sandboxes whose worker is still running must work end to end.
        alive_nodes = set(by_node) & set(_worker_pods(args.namespace, args.deployment))
        for index, sb in enumerate(sandboxes):
            if routes[sb.sandbox_id] not in alive_nodes:
                continue
            marker = _retry(
                f"reading the marker from {sb.sandbox_id}",
                lambda sb=sb, index=index: sb.files.read(
                    f"workspace/interference-{index}.txt",
                    request_timeout=REQUEST_TIMEOUT_S,
                ),
            )
            assert marker == f"marker-{index}\n", (index, marker)
            result = _retry(
                f"running cat in {sb.sandbox_id}",
                lambda sb=sb, index=index: sb.commands.run(
                    f"cat workspace/interference-{index}.txt",
                    timeout=REQUEST_TIMEOUT_S,
                    request_timeout=REQUEST_TIMEOUT_S,
                ),
            )
            assert result.stdout == f"marker-{index}\n", (index, result.stdout)
        print("OK: every sandbox on a surviving worker still runs with its file intact")

        # 4c. Sandboxes that were on the restarted worker are reported, not
        #     asserted: a restart gives the worker a *new* node id (its pod name),
        #     and the records of the sandboxes it re-adopts from the shared base
        #     still name the previous one, so the control plane cannot route to
        #     them until their TTL. That is a separate defect from N13 (tracked as
        #     N20 in docs/task-backlog.md); N13 is the claim that the two workers do
        #     not destroy each other's state, which 4a just checked.
        stranded = [sb.sandbox_id for sb in sandboxes if routes[sb.sandbox_id] not in alive_nodes]
        if stranded:
            print(
                f"NOTE: {len(stranded)} sandbox(es) lost their route when their worker "
                f"restarted (N20, not an N13 failure): {stranded}"
            )

        if not args.no_restart:
            # Both workers' logs: `kubectl logs -l` only returns one pod's log, and
            # the signal we want is on the *restarted* one.
            summaries = _summaries_since(
                args.namespace, args.deployment, restart_at
            )
            deleted = [
                int(match)
                for line in summaries
                for match in re.findall(r"deleted=(\d+)", line)
            ]
            def _max(metric: str) -> int:
                return max(
                    (
                        int(match)
                        for line in summaries
                        for match in re.findall(rf"{metric}=(\d+)", line)
                    ),
                    default=0,
                )

            assert deleted, "no reconcile summary found: the restart did not reconcile?"
            assert max(deleted) == 0, f"a worker deleted trees during the test: {summaries}"
            # How the round classified the trees depends on what that worker's
            # registry still held: a restart under a *new* node id makes trees the
            # fleet owns look unowned (-> `protected_elsewhere`), while trees whose
            # `sandbox.json` could not be read back land in `unmaterialised`. Both
            # are "left alone", so both are safe -- what must never happen is a
            # deletion, which the assertion above (and the on-disk check in 4a)
            # covers. Reported rather than asserted so a reader can see which shape
            # the round took.
            print(
                "OK: reconcile summaries show deleted=0"
                f" (protected_elsewhere={_max('protected_elsewhere')},"
                f" unmaterialised={_max('unmaterialised')},"
                f" disk_sweep_skipped={_max('disk_sweep_skipped')} across {len(summaries)} round(s))"
            )
            if _max("protected_elsewhere") + _max("unmaterialised") + _max("disk_sweep_skipped") == 0:
                print(
                    "NOTE: the round reported no protected/unmaterialised/skipped trees, "
                    "so this run did not observe the guard itself -- only its outcome"
                )
    finally:
        for sb in sandboxes:
            try:
                sb.kill()
            except Exception:  # noqa: BLE001 - cleanup must not mask the assertion
                pass

    # 5. Capacity returns to zero on every worker once the sandboxes are gone.
    deadline = time.time() + 120
    while True:
        fleet = _fleet(api, internal)
        reserved = {k[-8:]: v["reservedMemoryMB"] for k, v in fleet.items() if v["status"] == "healthy"}
        if all(value == 0 for value in reserved.values()):
            break
        if time.time() > deadline:
            raise AssertionError(f"reservations did not return to zero: {reserved}")
        time.sleep(3)
    print("after kill reservations:", reserved)
    print("MULTI-WORKER INTERFERENCE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
