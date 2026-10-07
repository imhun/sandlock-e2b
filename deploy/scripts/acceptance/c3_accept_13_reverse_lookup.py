#!/usr/bin/env python3
"""Judgment 13 acceptance, on the real compose multinode stack (3 workers, 1 host).

**The judgment.** The agent's container-pid → host-pid reverse lookup must
require *both* halves of the predicate: an ``NSpid`` chain that ends in the
reported pid **and** the target worker's own identity (its pid namespace, the
value the worker reports and the control plane records -- D9.3). ``NSpid`` alone
cannot do it: one host runs three workers, and two of them can each have a child
whose container pid is the same number.

**What this driver arranges.** Every step goes through the production path:

* two workers (``worker-1``, ``worker-2``) are made to hold a live slot child at
  the *same* container pid, by the rent-a-pid harness
  (``c3_accept13_slot_child_harness.py``), which runs the **production** child
  program (``envd_service.identity_grant``) -- only *which* pid it waits for is
  the rig's business;
* three real sandboxes are created (one per worker) so the control plane has
  records to derive the uid and the worker identity from;
* each arm is reported by the worker itself, through the worker's own
  ``envd_service.priv_helpers.request_identity`` -- i.e. worker → CP →
  ``grant-slot`` → agent → ``as_uid``, with the CP's source-IP second factor
  satisfied the way production satisfies it.

**Arms.**

| arm | claim | expectation |
|---|---|---|
| right A | worker-1's identity, the shared pid | grant lands on **worker-1's** child |
| right B | worker-2's identity, the same pid | grant lands on **worker-2's** child, a different host pid |
| counter 1 | worker-3's identity, the same pid | refused **by name**: the ``NSpid`` hit exists but belongs to the other workers |
| counter 2 | worker-1's identity, a pid only worker-2 has | refused by name -- the exact "refuses the other one" case: the hit is real, the identity is what makes it a refusal |

Every refusal is asserted **verbatim** (the plan's rule: exact assertions, no
substring tests). Raw evidence (harness logs, the agent's log, the CP's answer
for every arm) is written under ``--logdir``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "c3_accept13_slot_child_harness.py"
REPORT = HERE / "c3_accept13_slot_report.py"

#: The pool the control plane allocates slot uids from; the probe files cover a
#: window of it (the driver reads the real uid off the sandbox's workspace).
UID_POOL_START = 10000
UID_PROBE_COUNT = 104


def run(cmd: list[str], *, stdin: str | None = None, timeout: float = 300.0,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          timeout=timeout, check=False)
    if check and proc.returncode != 0:
        raise SystemExit(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


class Rig:
    def __init__(self, args, logdir: Path) -> None:
        self.args = args
        project = args.project
        api = args.api
        internal_key = args.internal_key
        self.project = project
        self.api = api.rstrip("/")
        self.internal_key = internal_key
        self.logdir = logdir
        self.client = httpx.Client(timeout=180.0)
        self.arm_records: list[dict] = []

    # ---------------------------------------------------------------- docker
    def worker(self, n: int) -> str:
        return f"{self.project}-worker-{n}-1"

    @property
    def agent(self) -> str:
        return f"{self.project}-c3-agent-1"

    def dexec(self, container: str, argv: list[str], *, user: str | None = "65534:65534",
              stdin: str | None = None, detach: bool = False,
              timeout: float = 300.0, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec"]
        if detach:
            cmd.append("-d")
        if stdin is not None:
            cmd.append("-i")
        if user is not None:
            cmd += ["-u", user]
        cmd.append(container)
        cmd += argv
        return run(cmd, stdin=stdin, timeout=timeout, check=check)

    def pid_namespace(self, worker: int) -> str:
        return self.dexec(
            self.worker(worker), ["readlink", "/proc/self/ns/pid"]
        ).stdout.strip()

    # ------------------------------------------------------------- sandboxes
    def create_sandbox(self) -> str:
        response = self.client.post(
            f"{self.api}/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base"},
        )
        if response.status_code != 201:
            raise SystemExit(f"create failed: {response.status_code} {response.text}")
        return response.json()["sandboxID"]

    def node_of(self, sandbox_id: str) -> str:
        route = self.client.get(
            f"{self.api}/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": self.internal_key},
        )
        route.raise_for_status()
        return route.json()["nodeID"]

    def kill(self, sandbox_id: str) -> None:
        self.client.delete(
            f"{self.api}/sandboxes/{sandbox_id}",
            headers={"X-API-Key": "local-key"},
        )

    def one_sandbox_per_worker(self, tries: int = 6) -> dict[int, str]:
        """Create three at once and keep the run where each worker got exactly one."""
        for attempt in range(1, tries + 1):
            with ThreadPoolExecutor(max_workers=3) as pool:
                ids = list(pool.map(lambda _: self.create_sandbox(), range(3)))
            placement = {worker: (sid, self.node_of(sid)) for worker, sid in zip(
                (1, 2, 3), ids)}
            self.node_of(ids[0])
            mapping: dict[int, str] = {}
            spread = True
            seen: set[str] = set()
            for sid in ids:
                node = self.node_of(sid)
                if node in seen:
                    spread = False
                seen.add(node)
                if node.startswith("worker-"):
                    mapping[int(node.split("-")[1])] = sid
            print(f"  attempt {attempt}: placement "
                  f"{ {sid: self.node_of(sid) for sid in ids} }")
            if spread and len(mapping) == 3:
                return mapping
            for sid in ids:
                self.kill(sid)
        raise SystemExit("could not get exactly one sandbox per worker")

    # ---------------------------------------------------------------- rig bits
    def install_harness(self, worker: int) -> None:
        self.dexec(self.worker(worker), ["sh", "-c", "cat > /tmp/c3h.py"],
                   stdin=HARNESS.read_text())
        self.dexec(self.worker(worker), ["sh", "-c", "cat > /tmp/c3report.py"],
                   stdin=REPORT.read_text())

    def uid_of(self, worker: int, sandbox_id: str) -> int:
        return int(self.dexec(
            self.worker(worker),
            ["stat", "-c", "%u", f"/var/lib/e2b-sandboxes/{sandbox_id}"],
        ).stdout.strip())

    def install_uid_probes(self, worker: int) -> None:
        script = (
            "set -e; mkdir -p /tmp/c3probe; "
            f"for u in $(seq {UID_POOL_START} {UID_POOL_START + UID_PROBE_COUNT - 1}); do "
            "echo slot-policy > /tmp/c3probe/policy-$u.json; "
            "chown 65534:$u /tmp/c3probe/policy-$u.json; "
            "chmod 0440 /tmp/c3probe/policy-$u.json; done; chmod 0755 /tmp/c3probe"
        )
        self.dexec(self.worker(worker), ["sh", "-c", script], user="0:0")

    def start_harness(self, worker: int, floor: int, tag: str, uid: int) -> dict:
        log = f"/tmp/c3h-{tag}.log"
        self.dexec(
            self.worker(worker),
            ["sh", "-c",
             f"nohup python3 /tmp/c3h.py {floor} {tag} /tmp/c3probe {uid} "
             f"/tmp/c3report.py > {log} 2>&1 &"],
            detach=True,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            text = self.dexec(self.worker(worker), ["cat", log], check=False).stdout
            if "HARNESS-READY" in text:
                for line in text.splitlines():
                    if "HARNESS-READY" in line:
                        pid = int(line.split("container_pid=")[1].split()[0])
                        return {"worker": worker, "pid": pid, "log": log,
                                "text": text}
            if "HARNESS-FAIL" in text or "HARNESS-TIMEOUT" in text:
                raise SystemExit(f"harness failed in {self.worker(worker)}:\n{text}")
            time.sleep(0.5)
        raise SystemExit(f"harness did not report readiness in {self.worker(worker)}")

    def harness_log(self, worker: int, log: str) -> str:
        return self.dexec(self.worker(worker), ["cat", log], check=False).stdout

    def stop_harnesses(self) -> None:
        """Kill the rig's processes: the anchor must be the worker *alone*.

        ``ProcLookup.worker_uid_gid`` refuses a pid namespace that holds more
        than one process, on purpose -- so a leftover stand-in child would turn
        every later file operation on that worker into a named 502 (and the
        *other* harness in the same worker would make the anchor ambiguous, so
        this runs before any arm, not only after).

        Because the granted child runs as the slot's uid and the harness as the
        worker's, the sweep runs as root inside the worker -- ``pkill`` from the
        worker's own uid could not signal it, and a pattern match would also
        match the ``docker exec`` shell running the sweep.
        """
        sweep = (
            "import os, signal\n"
            "for name in os.listdir('/proc'):\n"
            "    if not name.isdigit() or int(name) == os.getpid():\n"
            "        continue\n"
            "    try:\n"
            "        cmd = open(f'/proc/{name}/cmdline', 'rb').read().decode(\n"
            "            errors='replace')\n"
            "    except OSError:\n"
            "        continue\n"
            "    if '/tmp/c3h.py' in cmd or '/tmp/c3report.py' in cmd:\n"
            "        print('killing', name, cmd.replace(chr(0), ' ')[:60])\n"
            "        try:\n"
            "            os.kill(int(name), signal.SIGKILL)\n"
            "        except OSError as exc:\n"
            "            print('could not kill', name, exc)\n"
        )
        for worker in (1, 2, 3):
            proc = self.dexec(self.worker(worker), ["python3", "-"], user="0:0",
                              stdin=sweep, check=False)
            for line in proc.stdout.splitlines():
                print(f"   worker-{worker}: {line}")

    def reset_workers(self) -> None:
        """Start from a lab-clean anchor: exactly one process per worker pid ns.

        The acceptance needs the workers' pid namespaces to be *empty but for
        the worker itself*, and that is not a property of a running stack: the
        rig's own stand-in children, a ``docker exec`` an operator left behind,
        or a child nobody reaped (a zombie still counts as a process) each make
        ``ProcLookup.worker_uid_gid`` refuse with "holds more than one process".
        A fresh container gets a fresh pid namespace, so the run recreates them
        and then proves the anchor is clean before arranging anything.
        """
        import os as _os

        env = dict(_os.environ)
        env.update({"WORKER_IMAGE": self.args.worker_image,
                    "AGENT_IMAGE": self.args.agent_image})
        cmd = ["docker", "compose", "-f", self.args.compose]
        for override in self.args.override:
            cmd += ["-f", override]
        cmd += ["-p", self.project, "up", "-d", "--no-build", "--force-recreate",
                "worker-1", "worker-2", "worker-3"]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=300, check=False)
        if proc.returncode != 0:
            raise SystemExit(f"could not recreate the workers: {proc.stderr}")
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                nodes = self.client.get(
                    f"{self.api}/internal/nodes",
                    headers={"X-Internal-Key": self.internal_key},
                ).json()
            except Exception:
                time.sleep(1)
                continue
            if len(nodes) == 3 and all(n.get("status") == "healthy" for n in nodes):
                print("   workers recreated and healthy")
                return
            time.sleep(1)
        raise SystemExit("the workers did not come back healthy")

    def anchor_holders(self, worker: int) -> int:
        """How many processes the agent's own walk finds in that worker's ns."""
        namespace = self.pid_namespace(worker)
        code = (
            "import os, sys\n"
            "namespace = sys.argv[1]\n"
            "count = 0\n"
            "for name in os.listdir('/proc'):\n"
            "    if not name.isdigit():\n"
            "        continue\n"
            "    try:\n"
            "        if os.readlink(f'/proc/{name}/ns/pid') == namespace:\n"
            "            count += 1\n"
            "    except OSError:\n"
            "        pass\n"
            "print(count)\n"
        )
        proc = self.dexec(self.agent, ["python3", "-", namespace], stdin=code)
        return int(proc.stdout.strip().splitlines()[-1])

    def report(self, worker: int, sandbox_id: str, pid: int) -> dict:
        """The worker's own production report: ``{sandbox_id, pid}`` → CP → agent."""
        code = (
            "import json, sys\n"
            "from envd_service.priv_helpers import request_identity\n"
            "pid, sandbox_id, node_id = int(sys.argv[1]), sys.argv[2], sys.argv[3]\n"
            "try:\n"
            "    answer = request_identity(pid, sandbox_id,\n"
            "        control_plane_url='http://control-plane:3000',\n"
            "        node_id=node_id, internal_key='internal-key', timeout_s=30.0)\n"
            "    print(json.dumps({'ok': True, 'answer': answer}))\n"
            "except Exception as exc:  # PrivHelperError and friends\n"
            "    print(json.dumps({'ok': False, 'error': type(exc).__name__,\n"
            "                      'message': str(exc)}))\n"
        )
        proc = self.dexec(
            self.worker(worker), ["python3", "-", str(pid), sandbox_id, f"worker-{worker}"],
            stdin=code, timeout=180,
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def host_pid_namespace(self, host_pid: int) -> str:
        """The namespace the agent sees for a host pid (it is the ``pid: host`` one)."""
        return self.dexec(
            self.agent, ["readlink", f"/proc/{host_pid}/ns/pid"], user="65534:65534",
            check=False,
        ).stdout.strip()

    # ------------------------------------------------------------------- arms
    def arm(self, name: str, worker: int, sandbox_id: str, pid: int) -> dict:
        answer = self.report(worker, sandbox_id, pid)
        record = {"arm": name, "worker": f"worker-{worker}", "sandbox_id": sandbox_id,
                  "container_pid": pid, "result": answer}
        self.arm_records.append(record)
        print(f"  {name}: {json.dumps(answer)[:400]}")
        return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="c3acc")
    parser.add_argument("--api", default="http://127.0.0.1:3100")
    parser.add_argument("--internal-key", default="internal-key")
    parser.add_argument("--logdir", default="tmp/acc-13-16/logs")
    parser.add_argument("--compose",
                        default="deploy/compose/docker-compose.multinode.yml")
    parser.add_argument("--override", action="append",
                        default=["tmp/acc-13-16/compose.override.yml"])
    parser.add_argument("--worker-image", default="e2b-sandlock-worker:c3-acc")
    parser.add_argument("--agent-image", default="e2b-sandlock-agent:c3-acc")
    parser.add_argument("--shared-pid", type=int, default=300)
    args = parser.parse_args()

    logdir = Path(args.logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    rig = Rig(args, logdir)

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'PASS' if condition else 'FAIL'} {name}"
              + (f" -- {detail}" if detail else ""))
        if not condition:
            failures.append(f"{name}: {detail}")

    print("== judgment 13: real compose multinode stack ==")
    # The anchor is "the worker's pid namespace holds exactly one process", so
    # nothing from an earlier run may still be standing in that namespace.
    rig.stop_harnesses()
    rig.reset_workers()
    namespaces = {n: rig.pid_namespace(n) for n in (1, 2, 3)}
    print(f"worker pid namespaces: {namespaces}")
    check("three distinct worker pid namespaces", len(set(namespaces.values())) == 3,
          str(namespaces))
    holders = {n: rig.anchor_holders(n) for n in (1, 2, 3)}
    print(f"processes the agent's walk finds in each worker's namespace: {holders}")
    check("each worker's pid namespace holds exactly the worker itself",
          holders == {1: 1, 2: 1, 3: 1}, str(holders))

    print("-- creating one sandbox per worker")
    sandboxes = rig.one_sandbox_per_worker()
    uids = {n: rig.uid_of(n, sid) for n, sid in sandboxes.items()}
    print(f"sandbox placement {sandboxes} uids {uids}")

    for worker in (1, 2, 3):
        rig.install_harness(worker)
        rig.install_uid_probes(worker)

    print(f"-- two workers, both holding a child at container pid {args.shared_pid}")
    shared = {}
    for worker in (1, 2):
        shared[worker] = rig.start_harness(worker, args.shared_pid, f"shared{worker}",
                                           uids[worker])
        print(f"   worker-{worker}: {shared[worker]['text'].splitlines()[-1]}")
    check("worker-1 and worker-2 report the same container pid",
          shared[1]["pid"] == shared[2]["pid"] == args.shared_pid,
          f"{shared[1]['pid']} / {shared[2]['pid']}")

    print("-- worker-2 alone also holds a child, at a pid worker-1 does not have")
    private = rig.start_harness(2, -1, "private2", uids[2])
    private_pid = private["pid"]
    print(f"   worker-2's private child: container pid {private_pid}")
    check("the private child is not at the shared pid", private_pid != args.shared_pid,
          str(private_pid))

    print("-- right arm A: worker-1's identity for the shared pid")
    right_a = rig.arm("right-A", 1, sandboxes[1], args.shared_pid)
    check("right-A granted", right_a["result"].get("ok") is True,
          json.dumps(right_a["result"])[:300])
    host_a = (right_a["result"].get("answer") or {}).get("agent", {}).get("hostPid")
    check("right-A resolved a host pid",
          isinstance(host_a, int) and host_a > 0, str(host_a))
    if isinstance(host_a, int):
        ns_a = rig.host_pid_namespace(host_a)
        check("right-A's host pid is in worker-1's pid namespace",
              ns_a == namespaces[1], f"{ns_a} != {namespaces[1]}")

    print("-- right arm B: worker-2's identity, the SAME container pid")
    right_b = rig.arm("right-B", 2, sandboxes[2], args.shared_pid)
    check("right-B granted", right_b["result"].get("ok") is True,
          json.dumps(right_b["result"])[:300])
    host_b = (right_b["result"].get("answer") or {}).get("agent", {}).get("hostPid")
    check("right-B resolved a host pid",
          isinstance(host_b, int) and host_b > 0, str(host_b))
    check("the two workers' children have different HOST pids",
          isinstance(host_a, int) and isinstance(host_b, int) and host_a != host_b,
          f"{host_a} vs {host_b}")
    if isinstance(host_b, int):
        ns_b = rig.host_pid_namespace(host_b)
        check("right-B's host pid is in worker-2's pid namespace",
              ns_b == namespaces[2], f"{ns_b} != {namespaces[2]}")

    print("-- counter-arm 1: worker-3's identity for the shared pid (no child there)")
    counter_1 = rig.arm("counter-1", 3, sandboxes[3], args.shared_pid)
    expected_1 = (
        f"the control plane refused the slot-identity report for sandbox "
        f"{sandboxes[3]} (HTTP 502): the agent for node worker-3 refused the "
        f"grant: container pid {args.shared_pid} is not in worker worker-3's pid "
        f"namespace ({namespaces[3]}): refusing"
    )
    check("counter-arm 1 refused with the named message, verbatim",
          counter_1["result"].get("message") == expected_1,
          json.dumps(counter_1["result"].get("message")))

    print("-- counter-arm 2: worker-1's identity for a pid only worker-2 holds")
    counter_2 = rig.arm("counter-2", 1, sandboxes[1], private_pid)
    expected_2 = (
        f"the control plane refused the slot-identity report for sandbox "
        f"{sandboxes[1]} (HTTP 502): the agent for node worker-1 refused the "
        f"grant: container pid {private_pid} is not in worker worker-1's pid "
        f"namespace ({namespaces[1]}): refusing"
    )
    check("counter-arm 2 refused with the named message, verbatim",
          counter_2["result"].get("message") == expected_2,
          json.dumps(counter_2["result"].get("message")))

    print("-- the granted children's own view of their identity")
    identity_facts: dict[str, dict[str, list[int]]] = {}
    for worker in (1, 2):
        text = rig.harness_log(worker, shared[worker]["log"])
        (logdir / f"13-harness-worker{worker}.log").write_text(text)
        print(text.rstrip())
        facts: dict[str, list[int]] = {}
        for line in text.splitlines():
            if "REPORT-IDS" not in line:
                continue
            for field in line.split("REPORT-IDS", 1)[1].split():
                name, _, value = field.partition("=")
                if name in ("Uid", "Gid", "Groups"):
                    facts[name] = [int(item) for item in value.split()]
        identity_facts[f"worker-{worker}"] = facts
        check(f"worker-{worker}'s granted child runs as the granted uid",
              facts.get("Uid", [None])[0] == uids[worker],
              f"Uid={facts.get('Uid')} expected {uids[worker]}")
        print(f"   worker-{worker} identity: Uid={facts.get('Uid')} "
              f"Gid={facts.get('Gid')} Groups={facts.get('Groups')}")

    agent_log = run(["docker", "logs", "--tail", "400", rig.agent]).stdout
    (logdir / "13-agent.log").write_text(agent_log)
    (logdir / "13-arms.json").write_text(
        json.dumps({"namespaces": namespaces, "uids": uids, "sandboxes": sandboxes,
                    "arms": rig.arm_records, "shared_pid": args.shared_pid,
                    "private_pid": private_pid}, indent=2) + "\n"
    )

    print("-- cleanup")
    # Harnesses first: the file operations a kill performs run the *anchor*
    # lookup, and a stand-in child in the namespace would make it refuse.
    rig.stop_harnesses()
    for sid in sandboxes.values():
        rig.kill(sid)

    if failures:
        print(f"\nJUDGMENT 13: {len(failures)} FAILED assertion(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nJUDGMENT 13: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
