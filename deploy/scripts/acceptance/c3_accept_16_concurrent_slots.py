#!/usr/bin/env python3
"""Judgment 16 acceptance: N concurrent slot starts do not serialise.

**The judgment.** A slot start now goes through the control plane
(worker → CP → ``grant-slot`` → agent), so N workers starting slots at the same
time must not serialise. Three things are asserted, and a fourth is mandatory:

1. every arm **succeeds**, with nothing waiting on ``E2B_CREATE_QUEUE_TIMEOUT_S``;
2. the concurrent round is **not slower than the serial baseline** of the same
   N starts (no superlinear slowdown);
3. the CP→agent hop really is concurrent -- the instrument
   (``c3_compose_hop_proxy.py``) sees more than one ``grant-slot`` in flight;
4. **the counter-arm**: with ``E2B_C3_AGENT_MAX_CONCURRENCY=1`` the *same*
   rounds must reproduce the queueing -- one grant at a time, no overlap.

N is read from the shape's own worker count (three for
``docker-compose.multinode.yml``), not hard-coded.

**What a "slot start" is on this shape.** A create does *not* start a slot: the
worker forks the slot's child when the sandbox is first used (own identity's
``acquire``, ``envd_service/executors/sandlock.py``). So each round is "create
one sandbox on each worker, then start its slot", and the measured phase is the
slot start -- the one that carries the CP→agent hop. Both halves are reported.

Usage:

    python3 deploy/scripts/acceptance/c3_accept_16_concurrent_slots.py \\
        --compose deploy/compose/docker-compose.multinode.yml \\
        --override tmp/acc-13-16/compose.override.yml --project c3acc \\
        --logdir tmp/acc-13-16/logs --rounds 5 --baseline-rounds 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

PROXY_HOP_LOG = "/log/hop.jsonl"


def run(cmd: list[str], *, check: bool = True,
        timeout: float = 600.0) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          check=False)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
                         f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    return proc


def max_overlap(intervals: list[tuple[float, float]]) -> int:
    """How many of these intervals were open at the same moment."""
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()
    current = best = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best


class Driver:
    def __init__(self, args) -> None:
        self.args = args
        self.api = args.api.rstrip("/")
        self.client = httpx.Client(timeout=180.0)
        self.hop_log = Path(args.logdir) / "16-hop-instrument.jsonl"
        self.hop_log.parent.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ api
    def create(self) -> tuple[str, float]:
        start = time.perf_counter()
        response = self.client.post(
            f"{self.api}/sandboxes", headers={"X-API-Key": "local-key"},
            json={"templateID": "base"},
        )
        elapsed = time.perf_counter() - start
        if response.status_code != 201:
            raise RuntimeError(
                f"create answered {response.status_code} after {elapsed:.2f}s: "
                f"{response.text[:200]}"
            )
        return response.json()["sandboxID"], elapsed

    def node_of(self, sandbox_id: str) -> str:
        route = self.client.get(
            f"{self.api}/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": self.args.internal_key},
        )
        route.raise_for_status()
        return route.json()["nodeID"]

    def kill(self, sandbox_id: str) -> None:
        self.client.delete(f"{self.api}/sandboxes/{sandbox_id}",
                           headers={"X-API-Key": "local-key"})

    def create_one_per_worker(self) -> dict[int, str]:
        """N concurrent creates, one landing on each worker."""
        for _ in range(6):
            with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
                ids = list(pool.map(lambda _: self.create()[0],
                                    range(self.args.workers)))
            mapping: dict[int, str] = {}
            for sid in ids:
                node = self.node_of(sid)
                if node.startswith("worker-"):
                    mapping[int(node.split("-")[1])] = sid
            if len(mapping) == self.args.workers:
                return mapping
            for sid in ids:
                self.kill(sid)
        raise SystemExit("could not place one sandbox per worker")

    # ------------------------------------------------------------ slot start
    def start_slot(self, sandbox_id: str) -> float:
        """The first exec: what forks the slot's child and takes the hop."""
        code = (
            "import sys, time\n"
            "from e2b import Sandbox\n"
            "sb = Sandbox.connect(sys.argv[1])\n"
            "start = time.time()\n"
            "proc = sb.commands.run('echo slot-ok', timeout=120)\n"
            "print('SLOT_RESULT %s %s %.4f' % (proc.exit_code, proc.stdout.strip(),\n"
            "                                  time.time() - start), flush=True)\n"
        )
        # The SDK child talks to the same stack this driver does; its env is
        # explicit so a stray shell value cannot point it somewhere else.
        env = dict(os.environ)
        env.update({"E2B_API_KEY": "local-key", "E2B_API_URL": self.api,
                    "E2B_SANDBOX_URL": self.api})
        proc = subprocess.run(
            [self.args.sdk_python, "-c", code, sandbox_id],
            capture_output=True, text=True, timeout=300, env=env, check=False,
        )
        lines = [item for item in proc.stdout.splitlines()
                 if item.startswith("SLOT_RESULT")]
        if not lines:
            raise RuntimeError(f"slot start produced no result: "
                               f"{proc.stdout[-300:]} {proc.stderr[-300:]}")
        _, exit_code, stdout, seconds = lines[-1].split(" ", 3)
        if int(exit_code) != 0 or stdout != "slot-ok":
            raise RuntimeError(f"slot start failed: {lines[-1]}")
        return float(seconds)

    # ------------------------------------------------------------------ hops
    def read_hop_lines(self) -> list[dict]:
        """Read the instrument's file through the container that holds it."""
        proc = run(["docker", "exec", f"{self.args.project}-c3-agent-proxy-1",
                    "cat", PROXY_HOP_LOG], check=False)
        if proc.returncode != 0:
            return []
        entries: list[dict] = []
        for line in proc.stdout.splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def grants_since(self, mark: int) -> list[dict]:
        entries = self.read_hop_lines()
        self.hop_log.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return [entry for entry in entries[mark:] if entry.get("op") == "grant-slot"]

    @staticmethod
    def _hop_row(grants: list[dict]) -> dict:
        return {
            "grants": len(grants),
            "grants_max_in_flight": max_overlap(
                [(g["start"], g["end"]) for g in grants]
            ),
            "grant_statuses": sorted({g["status"] for g in grants}),
        }

    # ------------------------------------------------------------------ arms
    def concurrent_round(self) -> dict:
        mark = len(self.read_hop_lines())
        started = time.perf_counter()
        placement = self.create_one_per_worker()
        create_seconds = time.perf_counter() - started
        try:
            slot_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
                list(pool.map(lambda sid: self.start_slot(sid),
                              list(placement.values())))
            slot_seconds = time.perf_counter() - slot_started
        finally:
            for sid in placement.values():
                self.kill(sid)
        row = {
            "placement": {f"worker-{k}": v for k, v in placement.items()},
            "create_seconds": round(create_seconds, 3),
            "slot_seconds": round(slot_seconds, 3),
            "total_seconds": round(create_seconds + slot_seconds, 3),
        }
        row.update(self._hop_row(self.grants_since(mark)))
        return row

    def serial_round(self) -> dict:
        """The baseline: the same N slot starts, one after another."""
        mark = len(self.read_hop_lines())
        total_create = 0.0
        total_slot = 0.0
        for _ in range(self.args.workers):
            sid, create_seconds = self.create()
            try:
                slot_seconds = self.start_slot(sid)
            finally:
                self.kill(sid)
            total_create += create_seconds
            total_slot += slot_seconds
        row = {
            "create_seconds": round(total_create, 3),
            "slot_seconds": round(total_slot, 3),
            "total_seconds": round(total_create + total_slot, 3),
        }
        row.update(self._hop_row(self.grants_since(mark)))
        return row

    # --------------------------------------------------------------- compose
    def set_pool(self, pool: int) -> None:
        """Recreate the control plane with a different CP→agent pool size."""
        env = dict(os.environ)
        env.update({
            "E2B_C3_AGENT_MAX_CONCURRENCY": str(pool),
            "WORKER_IMAGE": self.args.worker_image,
            "AGENT_IMAGE": self.args.agent_image,
        })
        cmd = ["docker", "compose", "-f", self.args.compose]
        for override in self.args.override:
            cmd += ["-f", override]
        cmd += ["-p", self.args.project, "up", "-d", "--no-build",
                "--force-recreate", "control-plane"]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=300, check=False)
        if proc.returncode != 0:
            raise SystemExit(f"could not set the pool to {pool}: {proc.stderr}")
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                nodes = self.client.get(
                    f"{self.api}/internal/nodes",
                    headers={"X-Internal-Key": self.args.internal_key},
                ).json()
            except Exception:
                time.sleep(1)
                continue
            if len(nodes) == self.args.workers and all(
                node.get("status") == "healthy" for node in nodes
            ):
                return
            time.sleep(1)
        raise SystemExit(f"the control plane did not come back with pool={pool}")

    def reset_workers(self) -> None:
        """Fresh worker pid namespaces: the anchor must be the worker alone.

        ``ProcLookup.worker_uid_gid`` refuses a worker pid namespace that holds
        more than one process (and a zombie counts), so a namespace left dirty
        by an earlier run -- a stand-in child, an operator's ``docker exec`` --
        turns every ownership hand-over into a named 502 and every create into
        "failed to provision". Recreating the workers is what makes the run
        start from the state the shape has after a fresh ``up``.
        """
        env = dict(os.environ)
        env.update({"WORKER_IMAGE": self.args.worker_image,
                    "AGENT_IMAGE": self.args.agent_image})
        cmd = ["docker", "compose", "-f", self.args.compose]
        for override in self.args.override:
            cmd += ["-f", override]
        cmd += ["-p", self.args.project, "up", "-d", "--no-build",
                "--force-recreate"]
        cmd += [f"worker-{n}" for n in range(1, self.args.workers + 1)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=300, check=False)
        if proc.returncode != 0:
            raise SystemExit(f"could not recreate the workers: {proc.stderr}")
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                nodes = self.client.get(
                    f"{self.api}/internal/nodes",
                    headers={"X-Internal-Key": self.args.internal_key},
                ).json()
            except Exception:
                time.sleep(1)
                continue
            if len(nodes) == self.args.workers and all(
                node.get("status") == "healthy" for node in nodes
            ):
                return
            time.sleep(1)
        raise SystemExit("the workers did not come back healthy")

    def set_hop_delay(self, seconds: float) -> None:
        """Set the instrument's per-request delay (same value in every arm)."""
        proc = run(["docker", "exec", f"{self.args.project}-c3-agent-proxy-1",
                    "sh", "-c", f"printf '%s' {seconds} > /log/delay"],
                   check=False)
        if proc.returncode != 0:
            raise SystemExit(f"could not set the hop delay: {proc.stderr}")
        print(f"   hop instrument delay set to {seconds}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose",
                        default="deploy/compose/docker-compose.multinode.yml")
    parser.add_argument("--override", action="append",
                        default=["tmp/acc-13-16/compose.override.yml"])
    parser.add_argument("--project", default="c3acc")
    parser.add_argument("--api", default="http://127.0.0.1:3100")
    parser.add_argument("--internal-key", default="internal-key")
    parser.add_argument("--logdir", default="tmp/acc-13-16/logs")
    parser.add_argument("--workers", type=int,
                        help="the shape's worker count (default: counted from the "
                             "compose file)")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--baseline-rounds", type=int, default=3)
    parser.add_argument("--counter-rounds", type=int, default=3)
    parser.add_argument("--worker-image", default="e2b-sandlock-worker:c3-acc")
    parser.add_argument("--agent-image", default="e2b-sandlock-agent:c3-acc")
    parser.add_argument("--sdk-python", default="tmp/acc-13-16/venv/bin/python")
    parser.add_argument("--hop-delay", type=float, default=0.0,
                        help="the instrument's per-request delay, held constant "
                             "across every arm (0.25s makes an overlap visible "
                             "even when the hop itself is a few milliseconds)")
    parser.add_argument("--warmup-tries", type=int, default=6)
    args = parser.parse_args()

    if args.workers is None:
        text = Path(args.compose).read_text(encoding="utf-8")
        # ``^  worker-<n>:`` -- the top-level service keys, not `worker-data:`
        # (the volume) and not the indented environment keys.
        args.workers = sum(
            1 for line in text.splitlines()
            if re.match(r"^  worker-\d+:\s*$", line)
        )
    if not args.workers:
        raise SystemExit("the compose file names no worker services")
    print(f"shape worker count N = {args.workers}")

    driver = Driver(args)
    failures: list[str] = []
    report: dict = {"workers": args.workers, "args": vars(args)}
    round_errors: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'PASS' if condition else 'FAIL'} {name}"
              + (f" -- {detail}" if detail else ""))
        if not condition:
            failures.append(f"{name}: {detail}")

    def one_round(kind: str) -> dict:
        """Run one round, or record the failure and keep the run going."""
        try:
            if kind == "concurrent":
                return driver.concurrent_round()
            return driver.serial_round()
        except Exception as exc:  # a failed arm is evidence, not a crash
            detail = f"{kind} round failed: {type(exc).__name__}: {exc}"
            print(f"   !! {detail}")
            round_errors.append(detail)
            return {"error": detail}

    print("== judgment 16: N concurrent slot starts through the CP→agent hop ==")
    print("-- lab reset: fresh worker pid namespaces (see Driver.reset_workers)")
    driver.reset_workers()
    driver.set_hop_delay(args.hop_delay)
    print("-- warm-up (image + per-worker uid pool; retried while the freshly "
          "recreated workers settle)")
    warm = {"error": "not attempted"}
    for attempt in range(1, args.warmup_tries + 1):
        warm = one_round("concurrent")
        print(f"   warm-up {attempt}: {warm}")
        if "total_seconds" in warm:
            break
        time.sleep(4)
    check("the warm-up round eventually succeeded", "total_seconds" in warm,
          json.dumps(warm)[:200])
    # A failed warm-up is a settling artifact, not a measurement; a failed
    # *measured* round is (``round_errors`` below).
    round_errors.clear()

    print(f"-- right arm: pool = 64 (the shipped default), {args.rounds} rounds")
    driver.set_pool(64)
    concurrent = [one_round("concurrent") for _ in range(args.rounds)]
    for index, row in enumerate(concurrent, 1):
        print(f"   round {index}: {row}")
    print(f"-- serial baseline: {args.baseline_rounds} rounds of {args.workers} "
          f"one-at-a-time slot starts")
    serial = [one_round("serial") for _ in range(args.baseline_rounds)]
    for index, row in enumerate(serial, 1):
        print(f"   baseline {index}: {row}")
    driver.results["concurrent"] = concurrent
    driver.results["serial"] = serial

    done_concurrent = [row for row in concurrent if "total_seconds" in row]
    done_serial = [row for row in serial if "total_seconds" in row]
    worst_concurrent = max((row["total_seconds"] for row in done_concurrent),
                           default=float("inf"))
    worst_serial = max((row["total_seconds"] for row in done_serial),
                       default=float("inf"))
    check("every concurrent round succeeded (all creates + all slot starts)",
          not round_errors and len(done_concurrent) == args.rounds,
          "; ".join(round_errors[:3]))
    check("no round came near E2B_CREATE_QUEUE_TIMEOUT_S (30s)",
          bool(done_concurrent) and worst_concurrent < 30.0,
          f"worst round {worst_concurrent}s")
    check("no superlinear slowdown vs the serial baseline",
          bool(done_concurrent) and worst_concurrent <= worst_serial,
          f"concurrent {worst_concurrent}s vs serial {worst_serial}s")
    check("the hop really ran concurrently (more than one grant in flight)",
          bool(done_concurrent)
          and max(row["grants_max_in_flight"] for row in done_concurrent) >= 2,
          f"max in flight "
          f"{max((row['grants_max_in_flight'] for row in done_concurrent), default=0)}")
    check("every grant was a 200",
          bool(done_concurrent)
          and all(row["grant_statuses"] == [200] for row in done_concurrent),
          str([row["grant_statuses"] for row in concurrent]))

    print("-- counter-arm: the same rounds with E2B_C3_AGENT_MAX_CONCURRENCY=1")
    driver.set_pool(1)
    counter: list[dict] = []
    for index in range(args.counter_rounds):
        row = one_round("concurrent")
        counter.append(row)
        print(f"   counter round {index + 1}: {row}")
    driver.results["counter"] = counter
    done_counter = [row for row in counter if "total_seconds" in row]
    check("the counter-arm reproduced the queueing (one grant at a time)",
          bool(done_counter)
          and max(row["grants_max_in_flight"] for row in done_counter) == 1,
          f"max in flight {[row['grants_max_in_flight'] for row in counter]}")
    check("the counter-arm still completed every round (no timeout)",
          bool(done_counter)
          and all(row["grant_statuses"] == [200] for row in done_counter),
          str([row["grant_statuses"] for row in counter]))
    check("the counter-arm saw exactly one grant per worker per round",
          bool(done_counter)
          and all(row["grants"] == args.workers for row in done_counter),
          str([row["grants"] for row in counter]))

    print("-- restoring the shipped default (pool = 64)")
    driver.set_pool(64)

    report["right_arm"] = {
        "pool": 64,
        "rounds": concurrent,
        "worst_total_seconds": worst_concurrent,
        "max_grants_in_flight": max(
            (r["grants_max_in_flight"] for r in done_concurrent), default=0
        ),
    }
    report["serial_baseline"] = {"rounds": serial,
                                 "worst_total_seconds": worst_serial}
    report["counter_arm"] = {
        "pool": 1,
        "rounds": counter,
        "max_grants_in_flight": max(
            (r["grants_max_in_flight"] for r in done_counter), default=0
        ),
    }
    report["failed_assertions"] = failures
    report["round_errors"] = round_errors
    Path(args.logdir, "16-summary.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(f"\nraw hop instrument: {driver.hop_log}")
    print(f"summary: {Path(args.logdir, '16-summary.json')}")
    if failures:
        print(f"\nJUDGMENT 16: {len(failures)} FAILED assertion(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nJUDGMENT 16: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
