#!/usr/bin/env python3
"""N83 phase 1 acceptance: the per-sandbox cgroup quota, five checks, one JSON.

The five checks are the plan's Task 7 Step 1 (``docs/superpowers/plans/
2026-10-06-n83-per-sandbox-cgroup.md`` §"Task 7"), and every one of them is
about a *reading*, never about "the call did not error":

1. **The quota is real.** Four spinning processes inside one sandbox must show
   up as ~the declared share in the control plane's own measurement
   (``GET /internal/nodes/{node}/sandboxes`` -> ``measuredCpuPercent``), not the
   ~375% of the un-enforced shape (plan §1.3/N82). A second sandbox on the same
   node must keep its command round trip -- the neighbor must not pay for the
   spinners.
2. **The kernel enforces it.** The sandbox's own ``cpu.stat`` must show
   ``nr_throttled`` growing and ``usage_usec`` of the order of quota x time
   while it spins. This is the only reading that proves enforcement.
3. **The flood spends the sandbox's own budget.** With the notify rate limiter
   turned off on the workers (``E2B_SANDBOX_NOTIFY_RATE_LIMIT=0``, set by the
   caller's override and echoed in this report), the existing N82 probe
   (``probe_n82_traced_syscall_costs.py --op openclose``) must be bounded by the
   sandbox's own ``cpu.max``: the ``sbx_<id>`` cgroup's CPU stays at or below
   the declared quota, whereas the N82 baseline booked the same ~1.02 core to
   the *worker pod* (`docs/open-issues.md` N82).
4. **The narrowing / view shape.** From every worker container: what it sees
   under the mount, ``/proc/self/cgroup``, that its **own** container cgroup is
   the delegated one (owner 65534, ``cgroup.procs``/``cgroup.subtree_control``
   writable), and that a *different* container's cgroup is not writable.
5. **The negative (fail closed).** ``open(<own container cgroup>/cpu.max,
   O_WRONLY)`` must fail ``EACCES``: the delegation deliberately excludes
   ``cpu.max``, so a worker cannot lift its own ceiling.

The script drives **any** E2B endpoint (the same file is meant to be pointed at
the k0s lane later), so the two lane-specific things are parameters:

* ``--api-url`` / ``--api-key``: the E2B API (and gateway) endpoint + key.
* ``--worker-exec-template``: how to run a shell command *inside a worker
  container* for a given node. ``{node}`` is substituted with the node id. The
  local compose lane is

      --worker-exec-template 'docker exec -i n83acc-{node}-1 bash -lc'

  and the k0s lane is

      --worker-exec-template 'kubectl -n sandlock exec {node} -c worker -- bash -lc'

  The worker shell is how each of checks 1, 2, 4 and 5 reads the kernel: the
  control plane's per-node measurement is a **node-scoped** internal endpoint
  (source-IP second factor, N49), so the only place that can read it is the
  worker itself -- exactly the same reason checks 4/5 run there.

What a sandbox cgroup's path is differs by lane and is therefore discovered, not
assumed: the k8s mount is narrowed to the pod (``/pod-cgroup/sbx_<id>``), the
compose mount is the whole Docker-VM tree (``/pod-cgroup/<driver>/<id>/sbx_...``),
so the script walks the mount for ``sbx_<sandbox_id>`` and refuses when it finds
zero or more than one.

Usage (local lane; the override in ``tmp/`` is what sets ``required`` and the
rate limit):

    E2B_API_URL=http://127.0.0.1:3200 E2B_API_KEY=local-key \\
    python3 deploy/scripts/acceptance/cgroup_acceptance.py \\
        --api-url http://127.0.0.1:3200 \\
        --api-key "$E2B_API_KEY" --internal-key internal-key \\
        --internal-url http://control-plane:3000 \\
        --nodes worker-1,worker-2,worker-3 \\
        --worker-exec-template 'docker exec -i n83acc-{node}-1 bash -lc'

It prints one JSON object (every reading, plus ``ok``) and exits non-zero when
any check fails.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

#: Sandbox ids are validated by the platform before they reach a path
#: (``gateway_common.paths.validate_sandbox_id``); the script interpolates them
#: into worker shell scripts, so it holds the same line.
_SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: Four spinners, one sandbox, in the background: bash forks them, so the
#: spinning processes are plain children of the command's process group.
_SPIN_CMD = "sh -c 'for i in 1 2 3 4; do python3 -c \"while True: pass\" & done; wait'"

#: The N82 baseline this acceptance compares against (plan §1.3 / N82 row):
#: limiter off, **no** per-sandbox cgroup -> ~18149 op/s, and the *worker pod*
#: booked ~1.02 core with the sandbox's own cgroup nowhere in the accounting.
_N82_BASELINE = {"ops_per_s": 18149, "cores_on_worker_pod": 1.02}


class Refusal(RuntimeError):
    """A reading the script cannot take -- never confused with a failed check."""


def _probe_inner(probe: Path) -> str:
    """The N82 probe's sandbox-side program, imported from the probe itself."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_n83_probe_n82", probe)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.INNER


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise Refusal(f"command timed out after {timeout}s: {shlex.join(argv[:3])}...") from exc


class WorkerShell:
    """Runs shell scripts inside a worker container, per node id."""

    def __init__(self, template: str) -> None:
        self._template = template
        if "{node}" not in template:
            raise Refusal(
                "--worker-exec-template must contain '{node}' (got "
                f"{template!r}); e.g. 'docker exec -i n83acc-{{node}}-1 bash -lc'"
            )
        self._argv = shlex.split(template)

    def script(self, node: str, script: str, *, timeout: float = 60.0) -> str:
        """Run ``script`` under the lane's shell wrapper and return its stdout."""
        template = " ".join(self._argv)
        argv = shlex.split(template.replace("{node}", node)) + [script]
        done = _run(argv, timeout=timeout)
        if done.returncode != 0:
            raise Refusal(
                f"worker command on {node} exited {done.returncode}: "
                f"{done.stderr.strip()[:400] or done.stdout.strip()[:400]}"
            )
        return done.stdout

    def json(self, node: str, script: str, *, timeout: float = 60.0) -> dict:
        out = self.script(node, script, timeout=timeout)
        payload = out.strip().splitlines()[-1] if out.strip() else ""
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise Refusal(
                f"worker command on {node} did not end in a JSON line: {out!r}"
            ) from exc


# -- worker-side probes ----------------------------------------------------


def locate_cgroup(shell: WorkerShell, node: str, mount: str, sandbox_id: str) -> str:
    """The sandbox's cgroup *under this worker's own delegated cgroup*, or a refusal.

    Finding by name alone is not enough on the compose lane: every worker
    mounts the whole Docker-VM tree, so a bare ``sbx_<id>`` search finds the
    directory *from any worker* -- and "which node hosts this sandbox" is the
    question check 1 has to answer. The owning worker is the one whose own
    delegated container cgroup holds the ``sbx_<id>`` child.
    """
    if not _SANDBOX_ID_RE.match(sandbox_id):
        raise Refusal(f"refusing to interpolate a non-sandbox id: {sandbox_id!r}")
    own = own_cgroup(shell, node, mount)
    target = f"sbx_{sandbox_id}"
    if target not in own["dirs"]:
        raise Refusal(f"{target} is not a child of {own['own']} on {node}")
    return f"{own['own']}/{target}"


_CPU_STAT_SCRIPT = r"""
import json, pathlib
p = pathlib.Path(__CGROUP_PATH__)
stat = {}
for line in (p / "cpu.stat").read_text().splitlines():
    key, _, value = line.partition(" ")
    if value:
        stat[key] = int(value)
out = {"path": str(p), "cpu_stat": stat, "cpu_max": (p / "cpu.max").read_text().strip()}
print(json.dumps(out))
"""


def read_cpu_stat(shell: WorkerShell, node: str, cgroup_path: str) -> dict:
    script = "python3 - <<'PY'\n%s\nPY" % (
        _CPU_STAT_SCRIPT.replace("__CGROUP_PATH__", json.dumps(cgroup_path))
    )
    return shell.json(node, script)


def read_internal_measurement(
    shell: WorkerShell, node: str, internal_url: str, internal_key: str, timeout: float
) -> dict:
    """``GET /internal/nodes/{node}/sandboxes`` -- from inside that worker.

    The endpoint is node-scoped and carries the source-IP second factor (N49),
    so a host-side call is a 403 by construction; the worker is the only caller
    whose address the control plane expects.
    """
    script = (
        "python3 - <<'PY'\n"
        "import json, urllib.request\n"
        "req = urllib.request.Request(\n"
        "    %(url)s,\n"
        "    headers={'X-Internal-Key': %(key)s},\n"
        ")\n"
        "with urllib.request.urlopen(req, timeout=%(timeout)s) as resp:\n"
        "    print(resp.read().decode())\n"
        "PY"
    ) % {
        "url": json.dumps(f"{internal_url.rstrip('/')}/internal/nodes/{node}/sandboxes"),
        "key": json.dumps(internal_key),
        "timeout": repr(float(timeout)),
    }
    return shell.json(node, script, timeout=timeout + 10)


def sandboxes_on_node(
    shell: WorkerShell, node: str, internal_url: str, internal_key: str, timeout: float = 30.0
) -> set[str]:
    """The control plane's own record of which sandboxes run on ``node``.

    This -- not the cgroup layout -- is what decides *which node* a sandbox is
    on: it is the record the placement wrote, it is what the worker reconciles
    against, and it is the only one of the two that still exists when the
    per-sandbox cgroup switch is ``off`` (the RED lane needs a node for check 1
    too).
    """
    payload = read_internal_measurement(shell, node, internal_url, internal_key, timeout)
    return {row.get("sandboxID") for row in payload.get("sandboxes", [])}


#: The lane-neutral identity of "this worker's own container cgroup": the unique
#: directory this worker drained itself into and enabled cpu on -- holder of a
#: ``worker/`` child and of ``cpu`` in ``cgroup.subtree_control`` (plan §3.5 /
#: §4). On k8s that is a direct child of the (subPathExpr-narrowed) mount root;
#: on compose it sits under ``docker/<container-id>`` in the whole VM tree. A
#: name-based rule would be wrong on one lane or the other -- and on compose a
#: bare name search matches from *every* worker, since they all mount the same
#: tree.
_OWN_SCRIPT = r"""
import json, os, pathlib, sys

mount = pathlib.Path(__MOUNT__)
hostname = os.uname().nodename
found = []
for dirpath, dirnames, _files in os.walk(mount):
    here = pathlib.Path(dirpath)
    if len(here.relative_to(mount).parts) > 6:
        dirnames[:] = []
        continue
    if not (here / "worker").is_dir():
        continue
    try:
        enabled = (here / "cgroup.subtree_control").read_text().split()
    except OSError:
        continue
    if "cpu" in enabled and here.stat().st_uid == os.geteuid():
        found.append(here)

# This worker's own container, from the two facts that identify it per lane:
# on compose the hostname *is* the container id and appears verbatim in the
# cgroup path (every worker sees every container's cgroup in the mounted VM
# tree, so this token is what picks ours out); on k8s the mount root is already
# narrowed to this pod, so the container directory is the only candidate and
# the pod-name hostname matches nothing.
named = [path for path in found if hostname in str(path)]
if len(named) == 1:
    own = named[0]
elif not named and len(found) == 1:
    own = found[0]
else:
    sys.exit(
        "cannot pick this worker's delegated container cgroup under %s: %d "
        "candidates, %d matching hostname %s: %s"
        % (mount, len(found), len(named), hostname, [str(path) for path in found])
    )
print(json.dumps({
    "mount": str(mount),
    "hostname": hostname,
    "own": str(own),
    "own_parent": str(own.parent),
    "dirs": sorted(p.name for p in own.iterdir() if p.is_dir()),
    "proc_self_cgroup": pathlib.Path("/proc/self/cgroup").read_text().strip(),
}))
"""


def own_cgroup(shell: WorkerShell, node: str, mount: str) -> dict:
    return shell.json(
        node,
        "python3 - <<'PY'\n%s\nPY"
        % (_OWN_SCRIPT.replace("__MOUNT__", json.dumps(mount))),
    )


_VIEW_SCRIPT = r"""
import errno, json, os, pathlib

mount = pathlib.Path(__MOUNT__)
own = pathlib.Path(__OWN__)
hostname = os.uname().nodename


def owner(path):
    st = path.stat()
    return {"uid": st.st_uid, "gid": st.st_gid, "mode": oct(st.st_mode & 0o7777)}


def write_probe(path):
    try:
        fd = os.open(str(path), os.O_WRONLY)
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    os.close(fd)
    return "WRITABLE"


def mkdir_probe(path):
    try:
        os.mkdir(str(path))
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    os.rmdir(str(path))
    return "WRITABLE"


# A *different* container's cgroup: anything under the mount that looks like a
# real container (has a cpu.max) and is **not** inside our own subtree. On the
# compose lane (whole VM tree) there are several; on the k8s lane the mount is
# narrowed to this pod, so there is deliberately none -- which is itself the
# narrowing, reported as `foreign_visible: false` plus the mount listing.
own_prefix = str(own) + "/"
foreign = []
for dirpath, dirnames, _files in os.walk(mount):
    here = pathlib.Path(dirpath)
    if len(here.relative_to(mount).parts) > 6:
        dirnames[:] = []
        continue
    if str(here) == str(own) or str(here).startswith(own_prefix):
        dirnames[:] = []      # never descend into our own subtree
        continue
    if (here / "cpu.max").exists():
        foreign.append(here)
        dirnames[:] = []      # one entry per container is enough

own_facts = {
    "path": str(own),
    "owner": owner(own),
    "cgroup_procs": write_probe(own / "cgroup.procs"),
    "subtree_control": write_probe(own / "cgroup.subtree_control"),
    "cpu_max": write_probe(own / "cpu.max"),
    "cpu_max_value": (own / "cpu.max").read_text().strip(),
    "subtree_control_value": (own / "cgroup.subtree_control").read_text().strip(),
    "worker_dir_owner": owner(own / "worker") if (own / "worker").exists() else None,
}
foreign_facts = []
for sibling in foreign:
    foreign_facts.append(
        {
            "path": str(sibling),
            "owner": owner(sibling),
            "cpu_max": write_probe(sibling / "cpu.max"),
            "cgroup_procs": write_probe(sibling / "cgroup.procs"),
            "mkdir": mkdir_probe(sibling / "n83_acceptance_probe"),
        }
    )

top = sorted(p.name for p in mount.iterdir())
print(json.dumps({
    "mount": str(mount),
    "ls_mount": top,
    "ls_mount_count": len(top),
    "proc_self_cgroup": pathlib.Path("/proc/self/cgroup").read_text().strip(),
    "hostname": hostname,
    "own": own_facts,
    "foreign": foreign_facts,
}))
"""


def read_view(shell: WorkerShell, node: str, mount: str, own: str) -> dict:
    script = "python3 - <<'PY'\n%s\nPY" % (
        _VIEW_SCRIPT.replace("__MOUNT__", json.dumps(mount)).replace(
            "__OWN__", json.dumps(own)
        )
    )
    return shell.json(node, script)


# -- client-side helpers ---------------------------------------------------


def client_roundtrip_ms(sandbox, samples: int = 5) -> dict:
    """Measure the command round trip through the gateway, in milliseconds."""
    times = []
    for _ in range(samples):
        started = time.perf_counter()
        sandbox.commands.run("true")
        times.append((time.perf_counter() - started) * 1000.0)
    return {
        "samples_ms": [round(value, 2) for value in times],
        "min_ms": round(min(times), 2),
        "median_ms": round(statistics.median(times), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default=os.environ.get("E2B_API_URL", "http://127.0.0.1:3200"))
    parser.add_argument("--api-key", default=os.environ.get("E2B_API_KEY", "local-key"))
    parser.add_argument("--internal-key", default=os.environ.get("E2B_INTERNAL_API_KEY", "internal-key"))
    parser.add_argument(
        "--internal-url",
        default=os.environ.get("N83_ACC_INTERNAL_URL", "http://control-plane:3000"),
        help="the control plane's internal API as reachable *from inside a worker*",
    )
    parser.add_argument(
        "--worker-exec-template",
        default=os.environ.get("N83_ACC_WORKER_EXEC", "docker exec -i n83acc-{node}-1 bash -lc"),
        help="how to run a shell script inside a worker container; '{node}' is substituted",
    )
    parser.add_argument(
        "--nodes",
        default=os.environ.get("N83_ACC_NODES", "worker-1,worker-2,worker-3"),
    )
    parser.add_argument("--template", default=os.environ.get("N83_ACC_TEMPLATE", "base"))
    parser.add_argument("--cgroup-mount", default="/pod-cgroup")
    parser.add_argument("--settle-seconds", type=float, default=20.0, help="wait for the measured-CPU report")
    parser.add_argument("--spin-window-seconds", type=float, default=3.0)
    parser.add_argument("--flood-seconds", type=float, default=40.0)
    parser.add_argument("--sandbox-timeout-s", type=int, default=900)
    parser.add_argument("--out", default=None, help="also write the JSON report here")
    args = parser.parse_args()

    nodes = [node.strip() for node in args.nodes.split(",") if node.strip()]
    shell = WorkerShell(args.worker_exec_template)
    report: dict = {
        "lane": {
            "api_url": args.api_url,
            "internal_url": args.internal_url,
            "worker_exec_template": args.worker_exec_template,
            "nodes": nodes,
            "cgroup_mount": args.cgroup_mount,
            "template": args.template,
            "flood_seconds": args.flood_seconds,
            "sandbox_cgroup_env": "required (the caller's override; see the report)",
            "sandbox_notify_rate_limit_env": "0 (the caller's override; only this acceptance)",
        },
        "n82_baseline": _N82_BASELINE,
        "checks": {},
        "sandboxes": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    started = time.monotonic()
    boxes: list[tuple[object, dict]] = []

    def record(key: str, passed: bool, **readings) -> None:
        existing = report["checks"].get(key, {})
        existing.update({"pass": bool(passed), **readings})
        report["checks"][key] = existing

    def note_box(sandbox, node: str | None, cgroup: str | None) -> dict:
        entry = {
            "sandbox_id": sandbox.sandbox_id,
            "node_id": node,
            "cgroup": cgroup,
        }
        report["sandboxes"].append(entry)
        return entry

    def find_node(sandbox_id: str) -> str:
        """Which node's record lists this sandbox (the control plane's answer)."""
        found = [
            node
            for node in nodes
            if sandbox_id
            in sandboxes_on_node(shell, node, args.internal_url, args.internal_key)
        ]
        if len(found) != 1:
            raise Refusal(
                f"sandbox {sandbox_id} appears in {len(found)} nodes' records: {found}"
            )
        return found[0]

    def locate(sandbox_id: str) -> tuple[str, str | None]:
        """The hosting node, and its ``sbx_<id>`` cgroup when one exists.

        The cgroup is ``None`` on the switch-off lane (or before the first
        command built the slot) -- callers turn that into a *named failed
        reading* rather than a hard refusal, so a RED run still reports check
        1's measurement and check 3's op/s.
        """
        node = find_node(sandbox_id)
        try:
            return node, locate_cgroup(shell, node, args.cgroup_mount, sandbox_id)
        except Refusal:
            return node, None

    def kill(sandbox) -> None:
        try:
            sandbox.kill()
        except Exception as exc:  # noqa: BLE001 - teardown is best effort
            report.setdefault("teardown_errors", []).append(f"{sandbox.sandbox_id}: {exc}")

    from e2b import Sandbox

    def create(metadata: dict) -> object:
        try:
            return Sandbox.create(
                args.template,
                api_url=args.api_url,
                sandbox_url=args.api_url,
                api_key=args.api_key,
                timeout=args.sandbox_timeout_s,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 - a create failure is a named refusal
            raise Refusal(f"sandbox create failed: {type(exc).__name__}: {exc}") from exc

    try:
        # ---- check 1: the quota is real ----------------------------------
        first = create({"n83_acceptance": "spinner"})
        boxes.append((first, {}))
        # Route B's slot -- and with it the sandbox cgroup -- is built by the
        # *first command*, not by create (plan §4): measure the round trip
        # first (which is also the "quiet" baseline for check 1's neighbor
        # comparison), then look for the cgroup the command caused.
        quiet_rtt = client_roundtrip_ms(first)
        first_node, first_cgroup = locate(first.sandbox_id)
        note_box(first, first_node, first_cgroup)
        first.commands.run(_SPIN_CMD, background=True)
        time.sleep(args.settle_seconds)
        measurement = read_internal_measurement(
            shell, first_node, args.internal_url, args.internal_key, timeout=30
        )
        measured = None
        for row in measurement.get("sandboxes", []):
            if row.get("sandboxID") == first.sandbox_id:
                measured = row.get("measuredCpuPercent")
        declared = 100.0
        quota_readback = (
            read_cpu_stat(shell, first_node, first_cgroup) if first_cgroup else None
        )

        # A second sandbox on the *same* node: keep creating until one lands
        # there (the fleet admits at most four at the default share, so the cap
        # is the fleet ceiling, not an arbitrary number).
        second = None
        second_node = None
        second_cgroup = None
        busy_rtt = None
        extras = []
        for _ in range(3):
            candidate = create({"n83_acceptance": "neighbor"})
            boxes.append((candidate, {}))
            # Same rule as above: the command builds the slot, so measure the
            # round trip first and then look for the cgroup it caused.
            candidate_rtt = client_roundtrip_ms(candidate)
            try:
                candidate_node, candidate_cgroup = locate(candidate.sandbox_id)
            except Refusal:
                extras.append(candidate)
                continue
            if candidate_node == first_node:
                second, second_node, second_cgroup, busy_rtt = (
                    candidate,
                    candidate_node,
                    candidate_cgroup,
                    candidate_rtt,
                )
                break
            extras.append(candidate)
            note_box(candidate, candidate_node, candidate_cgroup)
        if second is None:
            record(
                "1_quota_is_real",
                False,
                declared_cpu_percent=declared,
                measured_cpu_percent=measured,
                note="no second sandbox landed on the first sandbox's node",
            )
        else:
            note_box(second, second_node, second_cgroup)
            record(
                "1_quota_is_real",
                measured is not None
                and 50.0 <= float(measured) <= 150.0
                and first_cgroup is not None
                # "Does not degrade": min of five round trips within 2x of the
                # quiet baseline *on the same node*. The min is the signal --
                # each batch's first sample is a cold connect and would other-
                # wise dominate both numbers (and hide a real regression).
                and busy_rtt["min_ms"] <= max(2 * quiet_rtt["min_ms"], 200.0),
                declared_cpu_percent=declared,
                measured_cpu_percent=measured,
                cpu_max_readback=quota_readback["cpu_max"] if quota_readback else None,
                sandbox_cgroup=first_cgroup,
                spinner_node=first_node,
                first_sandbox_rtt_quiet=quiet_rtt,
                second_sandbox_rtt=busy_rtt,
                round_trip_criterion="min-of-5, within 2x of the quiet baseline (>=200ms floor)",
                second_sandbox_node=second_node,
                second_sandbox_same_node=True,
            )

        # ---- check 2: the kernel is the one enforcing it ------------------
        quota_cores = declared / 100.0
        if first_cgroup is None:
            record(
                "2_kernel_enforces",
                False,
                reason="there is no sbx_<id> cgroup under the hosting worker's own "
                "delegated cgroup, so there is nothing to throttle (E2B_SANDBOX_CGROUP off?)",
                cgroup=None,
                quota_cores=quota_cores,
            )
        else:
            before = read_cpu_stat(shell, first_node, first_cgroup)
            window_started = time.monotonic()
            time.sleep(args.spin_window_seconds)
            after = read_cpu_stat(shell, first_node, first_cgroup)
            window = time.monotonic() - window_started
            usage_delta = after["cpu_stat"]["usage_usec"] - before["cpu_stat"]["usage_usec"]
            throttled_delta = after["cpu_stat"]["nr_throttled"] - before["cpu_stat"]["nr_throttled"]
            throttled_us = after["cpu_stat"]["throttled_usec"] - before["cpu_stat"]["throttled_usec"]
            observed_cores = usage_delta / 1e6 / window
            record(
                "2_kernel_enforces",
                throttled_delta > 0
                and usage_delta > 0
                and 0.5 * quota_cores <= observed_cores <= 1.5 * quota_cores,
                cgroup=first_cgroup,
                cpu_max=after["cpu_max"],
                window_s=round(window, 3),
                usage_usec_delta=usage_delta,
                nr_throttled_delta=throttled_delta,
                throttled_usec_delta=throttled_us,
                observed_cores=round(observed_cores, 3),
                quota_cores=quota_cores,
            )

        # Free the fleet's CPU budget for the flood: only check 3's own sandbox
        # is allowed to run from here on (the fleet admits four at 100%).
        for sandbox, _info in boxes:
            kill(sandbox)
        boxes.clear()
        time.sleep(3.0)

        # ---- check 3: the flood spends the sandbox's own quota ------------
        probe = Path(__file__).with_name("probe_n82_traced_syscall_costs.py")
        probe_env = {
            **os.environ,
            "E2B_API_URL": args.api_url,
            "E2B_SANDBOX_URL": args.api_url,
            "E2B_API_KEY": args.api_key,
        }

        def run_flood(seconds: float) -> dict:
            """Run the N82 probe and watch *its own* sandbox cgroup while it runs.

            The probe creates its own sandbox (that is the probe's own shape),
            so the sandbox is discovered by id -- a sandbox id that was not in
            the list when the run started -- and then by *which worker's own
            delegated cgroup holds its ``sbx_<id>`` child*.
            """
            known_before = {
                entry["sandbox_id"] for entry in _list_sandbox_ids(args.api_url, args.api_key)
            }
            started = time.monotonic()
            proc = subprocess.Popen(
                [sys.executable, str(probe), "--op", "openclose", "--seconds", str(int(seconds))],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=probe_env,
            )
            sandbox_id = node = cgroup = None
            samples: list[dict] = []
            while proc.poll() is None:
                time.sleep(2.0)
                if sandbox_id is None:
                    for candidate in _list_sandbox_ids(args.api_url, args.api_key):
                        if candidate in known_before:
                            continue
                        try:
                            candidate_node, candidate_cgroup = locate(candidate)
                        except Refusal:
                            continue
                        sandbox_id, node, cgroup = candidate, candidate_node, candidate_cgroup
                        break
                if sandbox_id is None or cgroup is None:
                    continue  # no per-sandbox cgroup on this lane: sample nothing
                try:
                    reading = read_cpu_stat(shell, node, cgroup)
                except Refusal:
                    continue  # the cgroup is gone: the probe killed its sandbox
                samples.append({"at_s": round(time.monotonic() - started, 2), **reading})
            output = (proc.stdout.read() if proc.stdout else "") or ""
            proc.wait(timeout=30)
            elapsed = time.monotonic() - started
            ops_per_s = None
            for line in output.splitlines():
                if line.startswith("DONE "):
                    match = re.search(r"ops_per_s=(\d+)", line)
                    if match:
                        ops_per_s = int(match.group(1))
            interval_cores = []
            for previous, current in zip(samples, samples[1:]):
                dt = current["at_s"] - previous["at_s"]
                if dt < 1.0:
                    continue
                delta = current["cpu_stat"]["usage_usec"] - previous["cpu_stat"]["usage_usec"]
                interval_cores.append(delta / 1e6 / dt)
            peak_cores = max(interval_cores) if interval_cores else None
            throttled_delta = None
            if len(samples) >= 2:
                throttled_delta = (
                    samples[-1]["cpu_stat"]["nr_throttled"] - samples[0]["cpu_stat"]["nr_throttled"]
                )
            return {
                "label": "the N82 probe (openclose) alone in its own sandbox",
                "probe_output": output.strip(),
                "ops_per_s": ops_per_s,
                "elapsed_s": round(elapsed, 1),
                "sandbox_id": sandbox_id,
                "node_id": node,
                "cgroup": cgroup,
                "cgroup_samples": samples,
                "interval_cores": [round(value, 3) for value in interval_cores],
                "peak_cores": round(peak_cores, 3) if peak_cores is not None else None,
                "nr_throttled_delta": throttled_delta,
            }

        alone = run_flood(args.flood_seconds)

        def run_binding_proof(seconds: float) -> dict:
            """Four concurrent copies of the probe's own openclose program, one sandbox.

            The probe above runs alone and stays *under* its ceiling (the
            workload self-limits), so it shows the spend is now inside the
            sandbox -- not that the ceiling binds. Four clients (the same
            program, one shell command, so no new command has to start on an
            already-saturated sandbox) contest the same 1-core quota and force
            the reading the N82 shape could not produce: total <= quota,
            ``nr_throttled`` growing, and *every* client's rate collapsing.

            The inner program is imported from the probe module itself (not
            copied), so this harness cannot drift from the probe above.
            """
            box = create({"n83_acceptance": "flood+spinners"})
            boxes.append((box, {}))
            box.commands.run("true")  # the first command is what builds the slot + cgroup
            node, cgroup = locate(box.sandbox_id)
            if cgroup is None:
                raise Refusal(
                    "the binding harness found no sbx_<id> cgroup under the hosting "
                    "worker's own delegated cgroup (E2B_SANDBOX_CGROUP off?)"
                )
            inner = _probe_inner(probe)
            holder: dict = {}

            def hunt() -> None:
                flood = (
                    "for i in 1 2 3 4; do "
                    f"N82_OP=openclose N82_SECONDS={int(seconds)} python3 hunt.py > hunt_$i.log 2>&1 & "
                    "done\nwait\ncat hunt_*.log\n"
                )
                holder["result"] = box.commands.run(
                    "cat > hunt.py <<'PYEOF'\n" + inner + "PYEOF\n" + flood,
                    timeout=seconds + 180,
                )

            thread = threading.Thread(target=hunt, daemon=True)
            started = time.monotonic()
            samples: list[dict] = []
            thread.start()
            while thread.is_alive():
                time.sleep(2.0)
                try:
                    samples.append(
                        {"at_s": round(time.monotonic() - started, 2), **read_cpu_stat(shell, node, cgroup)}
                    )
                except Refusal:
                    continue
            thread.join(timeout=60)
            result = holder.get("result")
            output = ((getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")).strip()
            ops_per_s = None
            rates = []
            for line in output.splitlines():
                if line.startswith("DONE "):
                    match = re.search(r"ops_per_s=(\d+)", line)
                    if match:
                        rates.append(int(match.group(1)))
            ops_per_s = sum(rates) if rates else None
            interval_cores = []
            for previous, current in zip(samples, samples[1:]):
                dt = current["at_s"] - previous["at_s"]
                if dt < 1.0:
                    continue
                delta = current["cpu_stat"]["usage_usec"] - previous["cpu_stat"]["usage_usec"]
                interval_cores.append(delta / 1e6 / dt)
            peak_cores = max(interval_cores) if interval_cores else None
            throttled_delta = None
            if len(samples) >= 2:
                throttled_delta = (
                    samples[-1]["cpu_stat"]["nr_throttled"] - samples[0]["cpu_stat"]["nr_throttled"]
                )
            return {
                "label": "4 concurrent copies of the probe's openclose program, one sandbox",
                "harness": "probe_n82_traced_syscall_costs.INNER imported verbatim, run in a sandbox we own",
                "sandbox_id": box.sandbox_id,
                "node_id": node,
                "cgroup": cgroup,
                "ops_per_s": ops_per_s,
                "per_client_ops_per_s": rates,
                "output": output,
                "cgroup_samples": samples,
                "interval_cores": [round(value, 3) for value in interval_cores],
                "peak_cores": round(peak_cores, 3) if peak_cores is not None else None,
                "nr_throttled_delta": throttled_delta,
            }

        quota_cores = declared / 100.0
        try:
            contested = run_binding_proof(min(args.flood_seconds, 20.0))
        except Refusal as exc:
            # A lane without a per-sandbox cgroup cannot run the binding proof.
            # That is a *failed reading*, not a reason to stop: the other
            # checks (and the alone reading) still have something to say.
            contested = {"refusal": str(exc)}
        record(
            "3_flood_spends_own_quota",
            "refusal" not in contested
            and alone["peak_cores"] is not None
            and alone["peak_cores"] <= 1.15 * quota_cores
            and alone["ops_per_s"] is not None
            and contested["peak_cores"] is not None
            and contested["peak_cores"] <= 1.15 * quota_cores
            # The flood must actually have run in the binding harness: a
            # saturated sandbox can refuse to start a *new* command at all
            # (measured: the N82 probe's own sandbox answered "command queue timed out"
            # when 4 spinners had already taken the whole quota), and a reading
            # without the clients' own rates would be about the spinners, not
            # about the flood.
            and contested["ops_per_s"] is not None
            and (contested["nr_throttled_delta"] or 0) > 0,
            probe=probe.name,
            n82_baseline={"ops_per_s": _N82_BASELINE["ops_per_s"], "booked_on": "the worker pod"},
            quota_cores=quota_cores,
            flood_alone=alone,
            flood_under_own_spinners=contested,
        )

        # ---- check 4 + 5: the view shape and the closed negative ----------
        view_facts = {}
        for node in nodes:
            try:
                own = own_cgroup(shell, node, args.cgroup_mount)
                facts = read_view(shell, node, args.cgroup_mount, own["own"])
                facts["own_subtree"] = {
                    "own": own["own"],
                    "children": own["dirs"],
                    "proc_self_cgroup": own["proc_self_cgroup"],
                }
            except Refusal as exc:
                view_facts[node] = {"error": str(exc)}
                continue
            facts["own_delegated"] = (
                facts["own"]["owner"]["uid"] == 65534
                and facts["own"]["cgroup_procs"] == "WRITABLE"
                and facts["own"]["subtree_control"] == "WRITABLE"
            )
            facts["foreign_closed"] = all(
                sibling["cpu_max"] != "WRITABLE"
                and sibling["cgroup_procs"] != "WRITABLE"
                and sibling["mkdir"] != "WRITABLE"
                for sibling in facts["foreign"]
            )
            # A different container's cgroup is either *visible and closed*
            # (compose: the whole VM tree is mounted, so this is a real
            # negative) or *not visible at all* because the mount root is not
            # the node's cgroup tree (k8s: subPathExpr narrowed it to this
            # pod). Any other combination -- a whole-tree mount that shows no
            # foreign cgroup -- is not evidence, so it fails.
            facts["foreign_visible"] = bool(facts["foreign"])
            facts["mount_looks_narrowed"] = not any(
                name == "docker"
                or name.startswith("kubepods")
                or name.endswith(".slice")
                for name in facts["ls_mount"]
            )
            facts["cpu_max_closed"] = facts["own"]["cpu_max"] != "WRITABLE"
            view_facts[node] = facts
        record(
            "4_narrowing_view_shape",
            bool(view_facts)
            and all(
                facts.get("own_delegated")
                and facts.get("foreign_closed")
                and (facts.get("foreign_visible") or facts.get("mount_looks_narrowed"))
                for facts in view_facts.values()
            ),
            workers=view_facts,
        )
        record(
            "5_own_cpu_max_eacces",
            bool(view_facts)
            and all(
                facts.get("own", {}).get("cpu_max") == "EACCES"
                for facts in view_facts.values()
            ),
            workers={
                node: facts.get("own", {}).get("cpu_max")
                for node, facts in view_facts.items()
            },
        )
    except Refusal as exc:
        report["refusal"] = str(exc)
    finally:
        for sandbox, _info in boxes:
            kill(sandbox)

    report["elapsed_s"] = round(time.monotonic() - started, 1)
    report["ok"] = bool(report["checks"]) and all(
        entry.get("pass") for entry in report["checks"].values()
    )
    if len(report["checks"]) != 5:
        report["ok"] = False
        report.setdefault("missing_checks", [])
        report["missing_checks"] = [
            name
            for name in (
                "1_quota_is_real",
                "2_kernel_enforces",
                "3_flood_spends_own_quota",
                "4_narrowing_view_shape",
                "5_own_cpu_max_eacces",
            )
            if name not in report["checks"]
        ]

    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(payload)
    if args.out:
        Path(args.out).write_text(payload + "\n")
    return 0 if report["ok"] else 1


def _list_sandbox_ids(api_url: str, api_key: str) -> list[str]:
    """The public list view -- a *read*, and deliberately not sandbox activity."""
    import urllib.request

    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/sandboxes", headers={"X-API-Key": api_key}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return [row["sandboxID"] for row in json.loads(response.read().decode())]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refusal as exc:
        print(json.dumps({"ok": False, "refusal": str(exc)}, ensure_ascii=False))
        sys.exit(2)
