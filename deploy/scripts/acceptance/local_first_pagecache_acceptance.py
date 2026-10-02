#!/usr/bin/env python3
"""End-to-end page-cache acceptance for create and snapshot (Task 1 Step 1 ③).

Drives the public API exactly as a user would -- a plain create, a 900 MiB file
written into ``/workspace`` (the tree on the shared NAS), a snapshot of it, then
a create *from* that snapshot -- while sampling the page cache charged to the
two containers that do the copying:

* the **snapshot** copy runs in the worker (``envd_service.agent
  agent_create_snapshot`` -> ``shutil.copytree``), whose pod limit is 4 GiB
  today (``deploy/k8s-k0s/worker-capacity.patch.yaml``; the pre-N58 baseline was
  2 GiB, which is the number the plan still quotes);
* the **restore** copy runs in the agent's ``maint`` face
  (``c3_agent.materialize.materialize_tree`` -> ``copy_tree``), limited to
  512 MiB.

Requires the repo's kubectl context and an API key; everything it creates is
removed before it exits::

    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \\
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/local_first_pagecache_acceptance.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

SAMPLER = Path(__file__).with_name("local_first_pagecache_probe.py")
SAMPLER_IN_CONTAINER = "/var/lib/e2b-images/.lf_pagecache_probe.py"
SUMMARY_DIR = "/var/lib/e2b-images/.lf_pagecache"


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["kubectl", "-n", "sandlock", *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise RuntimeError("kubectl %s: %s" % (" ".join(args), proc.stderr.strip()))
    return proc


def worker_pods() -> list[tuple[str, str]]:
    raw = kubectl(
        "get", "pods", "-l", "app=e2b-worker",
        "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\t\"}{.spec.nodeName}{\"\\n\"}{end}",
    ).stdout
    return [tuple(line.split("\t")) for line in raw.splitlines() if line.strip()]  # type: ignore[return-value]


def pod_hosting(sandbox_id: str) -> tuple[str, str]:
    """The (worker pod, node) whose shared volume holds this sandbox's tree."""
    for pod, node in worker_pods():
        probe = kubectl(
            "exec", pod, "-c", "worker", "--", "test",
            "-d", "/var/lib/e2b-sandboxes/workspaces/%s" % sandbox_id, check=False,
        )
        if probe.returncode == 0:
            return pod, node
    raise RuntimeError("no worker pod holds the tree for %s" % sandbox_id)


def agent_pod_on(node: str) -> str:
    for pod, pod_node in [
        tuple(line.split("\t"))
        for line in kubectl(
            "get", "pods", "-l", "app=c3-agent",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\t\"}{.spec.nodeName}{\"\\n\"}{end}",
        ).stdout.splitlines()
        if line.strip()
    ]:
        if pod_node == node:
            return pod
    raise RuntimeError("no c3-agent pod on node %s" % node)


def control_plane_pods() -> list[str]:
    raw = kubectl("get", "pods", "-l", "app=control-plane",
                  "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}").stdout
    return [line for line in raw.splitlines() if line.strip()]


def delete_snapshots_on_every_replica(ids: list[str]) -> dict[str, object]:
    """Delete each id on **each replicas's own** registry.

    The public delete drops the record only from the replica that served the
    request (the payload directory is shared, the record dictionary is not --
    see the design doc §4.3), so a client that only goes through the gateway
    leaves a phantom record on the other replica. A probe that creates
    snapshots has to clean up after itself on both.
    """
    report: dict[str, object] = {"deleted": {}, "errors": {}}
    for pod in control_plane_pods():
        code = "\n".join([
            "import json, sys, urllib.request, urllib.error",
            "ids = json.loads(sys.argv[1])",
            "key = sys.argv[2]",
            "out = {}",
            "for sid in ids:",
            "    req = urllib.request.Request(",
            "        'http://127.0.0.1:3000/templates/' + sid,",
            "        method='DELETE', headers={'X-API-Key': key})",
            "    try:",
            "        out[sid] = urllib.request.urlopen(req, timeout=60).status",
            "    except urllib.error.HTTPError as exc:",
            "        out[sid] = exc.code",
            "print(json.dumps(out))",
        ])
        proc = subprocess.run(
            ["kubectl", "-n", "sandlock", "exec", pod, "-c", "control-plane", "--",
             "python3", "-c", code, json.dumps(ids), os.environ.get("E2B_API_KEY", "")],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            report["errors"][pod] = proc.stderr.strip()[:200]  # type: ignore[index]
        else:
            report["deleted"][pod] = json.loads(proc.stdout.strip())  # type: ignore[index]
    return report


def _write_sampler(pod: str, container: str) -> None:
    kubectl("exec", pod, "-c", container, "--", "sh", "-c",
            # 0777: the agent's maint face is root and the worker is uid 65534,
            # and both mount the same hostPath, so whoever runs second must be
            # able to drop its summary next to the first one's.
            "rm -rf %s; mkdir -p -m 0777 %s" % (SUMMARY_DIR, SUMMARY_DIR))
    # The same hostPath is mounted by the agent's root maint face and by the
    # worker (uid 65534), so a file one of them wrote is not writable by the
    # other. The directory itself is owned by 65534, so an unlink works either
    # way; do it before every write.
    proc = subprocess.run(
        ["kubectl", "-n", "sandlock", "exec", "-i", pod, "-c", container, "--", "sh", "-c",
         "rm -f %s; cat > %s" % (SAMPLER_IN_CONTAINER, SAMPLER_IN_CONTAINER)],
        input=SAMPLER.read_text(encoding="utf-8"), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("writing the sampler failed: %s" % proc.stderr)


def run_sampler(pod: str, container: str, label: str, seconds: float, workload) -> dict[str, object]:
    _write_sampler(pod, container)
    sampler = subprocess.Popen(
        ["kubectl", "-n", "sandlock", "exec", "-i", pod, "-c", container, "--",
         "python3", SAMPLER_IN_CONTAINER, "--seconds", str(seconds), "--label", label,
         "--json-out", SUMMARY_DIR, "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.5)
    started = time.monotonic()
    detail = workload()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    sampler_out, sampler_err = sampler.communicate()
    summary_line = next(
        (line for line in sampler_out.splitlines() if line.startswith("SUMMARY ")), None
    )
    result: dict[str, object] = {
        "label": label,
        "pod": pod,
        "container": container,
        "workload_ms": round(elapsed_ms, 1),
        "sampler": json.loads(summary_line[len("SUMMARY "):]) if summary_line else None,
        "sampler_stderr": sampler_err.strip()[:400],
    }
    if detail is not None:
        result["detail"] = detail
    return result


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def gather(rows: list[dict[str, object]]) -> dict[str, object]:
    """Compact per-run rows plus p50/min/max of what the cap decision needs."""
    runs = []
    peaks: list[float] = []
    currents: list[float] = []
    work: list[float] = []
    attempts: list[float] = []
    limit = None
    for row in rows:
        sampler = row.get("sampler") or {}
        limit = sampler.get("memory_max_bytes", limit)
        peak = float(sampler.get("memory_peak_file_bytes", 0)) / 2**20
        current = float(sampler.get("memory_peak_current_bytes", 0)) / 2**20
        detail = row.get("detail")
        failed = isinstance(detail, dict) and "error" in detail
        run = {
            "label": row.get("label"),
            "pod": row.get("pod"),
            "container": row.get("container"),
            "workload_ms": row.get("workload_ms"),
            "limit_mib": round(float(limit or 0) / 2**20, 1),
            "peak_file_mib": round(peak, 1),
            "peak_current_mib": round(current, 1),
            "detail": detail,
        }
        if failed:
            # A run whose workload never happened (an OOM'd maint, a 502) still
            # has a sampler summary -- but that summary is the *idle* container,
            # so it must stay out of the peak statistics.
            run["failed"] = True
        else:
            peaks.append(peak)
            currents.append(current)
            work.append(float(row.get("workload_ms", 0.0)))
            if isinstance(detail, dict) and "attempts" in detail:
                attempts.append(float(detail["attempts"]))
        runs.append(run)
    ok = len(peaks)
    if not peaks:
        peaks = [0.0]
        currents = [0.0]
        work = [0.0]
    out: dict[str, object] = {
        "n": len(runs),
        "ok": ok,
        "limit_mib": round(float(limit or 0) / 2**20, 1),
        "workload_ms": {"p50": round(_pct(work, 0.5), 1), "min": round(min(work), 1), "max": round(max(work), 1)},
        "peak_file_mib": {"p50": round(_pct(peaks, 0.5), 1), "min": round(min(peaks), 1), "max": round(max(peaks), 1)},
        "peak_current_mib": {"p50": round(_pct(currents, 0.5), 1), "min": round(min(currents), 1), "max": round(max(currents), 1)},
        "runs": runs,
    }
    if attempts:
        # A create-from-snapshot that had to be retried is reported, never
        # silently absorbed: it is the visibility window of a freshly written
        # snapshot record across the two control-plane replicas.
        out["create_attempts"] = {
            "values": [int(a) for a in attempts],
            "p50": int(_pct(attempts, 0.5)),
            "max": int(max(attempts)),
            "needed_retry": sum(1 for a in attempts if a > 1),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file-mb", type=int, default=900, help="one file inside /workspace (1024 MiB quota)")
    parser.add_argument("--sampler-seconds", type=float, default=30.0)
    parser.add_argument("--restore-seconds", type=float, default=12.0)
    parser.add_argument("--repeat", type=int, default=10, help="runs per workload (the brief asks for n>=10)")
    parser.add_argument("--snapshot-repeat", type=int, default=None, help="default: --repeat")
    parser.add_argument("--plain-repeat", type=int, default=None, help="default: --repeat")
    parser.add_argument(
        "--max-failures",
        type=int,
        default=3,
        help="stop the restore series after this many failed creates (an OOM'd maint is data)",
    )
    parser.add_argument(
        "--create-retries",
        type=int,
        default=5,
        help=(
            "how many times a create-from-snapshot may be retried while the freshly "
            "written record becomes readable through the gateway (recorded, not hidden)"
        ),
    )
    parser.add_argument("--create-retry-wait-s", type=float, default=2.0)
    parser.add_argument(
        "--skip-replica-cleanup",
        action="store_true",
        help="do not clear this run's snapshot records on every control-plane replica",
    )
    args = parser.parse_args()

    from e2b import Sandbox

    report: dict[str, object] = {"file_mb": args.file_mb}
    snapshot_id = None

    plain_repeat = args.plain_repeat or args.repeat
    snapshot_repeat = args.snapshot_repeat or args.repeat

    # A plain create (no snapshot): the tree skeleton, materialized on the
    # agent's maint face. It is the baseline the restore row is compared
    # against -- the copy is the only difference between them.
    plain: dict[str, object] = {"created": None}
    first = Sandbox.create(timeout=900)
    plain["pod"], plain["node"] = pod_hosting(first.sandbox_id)
    plain["agent_pod"] = agent_pod_on(plain["node"])  # type: ignore[arg-type]
    first.kill()

    def plain_create() -> dict[str, object]:
        created = Sandbox.create(timeout=900)
        previous = plain.get("created")
        plain["created"] = created
        if previous is not None:
            previous.kill()  # type: ignore[union-attr]
        return {"sandbox_id": created.sandbox_id}

    plain_result = gather([
        run_sampler(
            plain["agent_pod"],  # type: ignore[arg-type]
            "maint",
            "maint-plain-create-%d" % i,
            min(args.sampler_seconds, 6.0),
            plain_create,
        )
        for i in range(plain_repeat)
    ])
    report["plain_create"] = plain_result
    print("SECTION " + json.dumps({"plain_create": plain_result}), flush=True)
    if plain["created"] is not None:
        plain["created"].kill()  # type: ignore[union-attr]

    source = Sandbox.create(timeout=900)
    try:
        report["source_sandbox"] = source.sandbox_id
        write = source.commands.run(
            "dd if=/dev/zero of=/workspace/big.bin bs=1M count=%d conv=fsync 2>&1; echo RC=$?; "
            "df -h /workspace | tail -1" % args.file_mb,
            timeout=900,
        )
        report["seed_write"] = write.stdout

        pod, node = pod_hosting(source.sandbox_id)
        report["worker_pod"] = pod
        report["node"] = node

        made: list[str] = []

        def take_snapshot():
            snapshot = source.create_snapshot()
            made.append(snapshot.snapshot_id)
            return {"snapshot_id": snapshot.snapshot_id}

        snapshot_result = gather([
            run_sampler(pod, "worker", "worker-snapshot-%d" % i, args.sampler_seconds, take_snapshot)
            for i in range(snapshot_repeat)
        ])
        report["snapshot"] = snapshot_result
        print("SECTION " + json.dumps({"snapshot": snapshot_result}), flush=True)
        snapshot_id = made[0] if made else None
    finally:
        source.kill()

    if snapshot_id:
        agent = agent_pod_on(report["node"])  # type: ignore[arg-type]
        report["agent_pod"] = agent
        restored: dict[str, object] = {"id": None, "sandbox": None}
        failures: list[dict[str, object]] = []

        def restore() -> dict[str, object]:
            errors: list[str] = []
            created = None
            for attempt in range(1, args.create_retries + 1):
                try:
                    created = Sandbox.create(snapshot_id, timeout=900)
                    break
                except Exception as exc:  # noqa: BLE001 - these are data, not crashes
                    # Two shapes have been seen here and both are recorded:
                    # (a) a freshly created snapshot's record is not yet visible
                    #     through the gateway ("Template ... not found"), and
                    # (b) a 900 MiB restore into the 512 MiB maint face OOMKills
                    #     the container ("agent ... is unreachable").
                    errors.append("%s: %s" % (type(exc).__name__, str(exc)[:200]))
                    time.sleep(args.create_retry_wait_s)
            if created is None:
                failures.append({"kind": "create-failed", "detail": errors[-1]})
                return {"sandbox_id": None, "attempts": len(errors), "errors": errors, "error": errors[-1]}
            previous = restored.get("sandbox")
            restored["id"] = created.sandbox_id
            restored["sandbox"] = created
            if previous is not None:
                previous.kill()  # type: ignore[union-attr]
            return {
                "sandbox_id": created.sandbox_id,
                "attempts": len(errors) + 1,
                "retry_errors": errors,
                "df": created.commands.run("df -h /workspace | tail -1").stdout.strip(),
            }

        try:
            runs = []
            for i in range(args.repeat):
                runs.append(run_sampler(agent, "maint", "maint-restore-%d" % i, args.restore_seconds, restore))
                if len(failures) >= args.max_failures:
                    report["restore_stopped_after_failures"] = len(failures)
                    break
            restore_result = gather(runs)
            restore_result["failures"] = failures
            restore_result["attempted"] = len(runs)
            report["restore"] = restore_result
            print("SECTION " + json.dumps({"restore": restore_result}), flush=True)
        finally:
            # Cleanup goes in a finally: the first version of this probe left
            # ten 900 MiB snapshot payloads on the shared volume when the
            # restore phase died.
            if restored["sandbox"] is not None:
                restored["sandbox"].kill()  # type: ignore[union-attr]
            deleted, delete_errors = [], []
            for made_id in sorted(set(made) | ({snapshot_id} if snapshot_id else set())):
                try:
                    source.delete_snapshot(made_id)
                    deleted.append(made_id)
                except Exception as exc:  # noqa: BLE001 - deletion is best-effort cleanup
                    delete_errors.append("%s: %s" % (type(exc).__name__, exc))
            report["snapshots_deleted"] = deleted
            report["delete_snapshot_errors"] = delete_errors
            if not args.skip_replica_cleanup:
                # One gateway DELETE is not enough: the record lives in *each*
                # replica's memory (design doc §4.3), so clean every replica.
                report["replica_cleanup"] = delete_snapshots_on_every_replica(
                    sorted(set(made) | ({snapshot_id} if snapshot_id else set()))
                )

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
