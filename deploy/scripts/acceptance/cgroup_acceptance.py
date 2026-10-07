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
   off for this lane, the existing N82 probe
   (``probe_n82_traced_syscall_costs.py --op openclose``) must be bounded by the
   sandbox's own ``cpu.max``: the ``sbx_<id>`` cgroup's CPU stays at or below
   the declared quota, whereas the N82 baseline booked the same ~1.02 core to
   the *worker pod* (`docs/open-issues.md` N82). The limiter being off is read,
   not assumed: the probe's own stall counter must not carry the cap's
   signature (see ``flood_is_capped``) -- since 2026-10-07 a ``required`` lane
   drops the cap by itself, so a run is free to leave
   ``E2B_SANDBOX_NOTIFY_RATE_LIMIT`` at its shipped default, and this check is
   what refuses a verdict that is satisfiable with the cap still in place.
4. **The narrowing / view shape.** From every worker container: what it sees
   under the mount, ``/proc/self/cgroup``, and which cgroups are writable.

   **The rule, stated once**: a cgroup is writable by this worker exactly when
   the one-shot delegation gave it to **this worker's uid** — its own
   container cgroup (``cgroup.procs``/``cgroup.subtree_control``), and, on a
   lane where every worker runs as the *same* host uid, the other workers'
   container cgroups too (compose: all of them are 65534). ``cpu.max`` is
   never writable — the delegation deliberately excludes it, for a peer
   exactly as for ourselves. Every other cgroup in view (the root-owned
   containers — control plane, redis, agents — and the mounted root itself) is
   EACCES on every write.

   "Peer" means an actual container directory beside ours under the same
   parent — a sibling container directory under the pod directory on k8s, or
   ``/pod-cgroup/docker/<other-container-id>`` on a lane whose mount is *not*
   narrowed (what the compose lanes were before 2026-10-06) — **not** the
   mounted root, which is a cgroup of its own and is probed separately for
   exactly that reason (fix round 1, finding 1).

   Check ④ therefore asserts: (a) our own delegated cgroup is writable on
   ``cgroup.procs``/``cgroup.subtree_control`` and **not** on ``cpu.max``;
   (b) **every** visible peer's ``cpu.max`` is EACCES; (c) every *non*-
   delegated peer refuses all three writes (``cpu.max``, ``cgroup.procs``,
   ``mkdir``); and (d) if no peer is visible at all (a narrowed mount), the
   mount root refuses ``cpu.max``/``cgroup.procs``/``mkdir``. The delegated
   peers that *are* writable — the same-uid case above — are reported per peer
   as ``delegated_peers`` rather than hidden.

   **Both shipped lanes narrow the mount**, by different devices, so both
   assert (d) and neither can produce (b)/(c) today: k8s takes
   ``hostPath …/kubepods/<qos>`` with ``subPathExpr: pod$(POD_UID)``, and each
   compose worker declares a static ``cgroup_parent:
   /e2b-${COMPOSE_PROJECT_NAME}-worker-<n>`` with the matching
   ``/sys/fs/cgroup<that path>`` bind (compose **has** ``volume.subpath``, but
   for ``type: bind`` it is silently ignored, and it interpolates at parse
   time, so it can never name a container id). The ``peer-container`` branch —
   (b) plus (c), and the same-uid ``delegated_peers`` reading — is what a
   *non-narrowed* mount produces, i.e. what the compose lane was before
   2026-10-06.
5. **The negative (fail closed).** ``open(<own container cgroup>/cpu.max,
   O_WRONLY)`` must fail ``EACCES``: the delegation deliberately excludes
   ``cpu.max``, so a worker cannot lift its own ceiling.

Phase 2 (``E2B_SANDBOX_CGROUP=required`` with N83 phase 2's Tasks 1-5) adds
four more, same rule -- each one a reading:

6. **The memory ceiling kills, and only the allocator.**
   ``memory.max``/``memory.high`` read back exactly the declared ``memoryMB``;
   a process that allocates past it is **SIGKILLed** (the shell that ran it
   reports ``hog_exit=137``, i.e. 128 + SIGKILL, and the box's own
   ``memory.events`` shows ``oom_kill=1`` with ``oom_group_kill=0`` -- D3: the
   rest of the box survives). A second sandbox on the same node keeps its
   command round trip.
7. **The task ceiling answers ``EAGAIN``, from the kernel.** A fork bomb in one
   box forks until it is refused, and that refusal is the **kernel's**:
   ``pids.current`` reaches ``pids.max`` exactly while ``cgroup.procs`` (the
   process count the mediator itself counts) is still *under* the limit, and
   the box's ``pids.events`` shows ``max >= 1``. Both are readings the
   mediator's own counter cannot produce -- it does not count threads, which is
   why the kernel's wall arrives first. A neighbour sandbox stays healthy.
8. **An over-ceiling create is a named ``400``.** ``cpuCount: 8`` (or a
   ``memoryMB`` past the node's promise) answers ``400`` with the exact message
   the node's own ceiling implies, while a request *at* the ceiling is
   accepted -- so "always refuses" cannot pass either. The ceiling is the
   *control plane's* policy (ruling R17): the expected text is built from the
   ``E2B_MAX_SANDBOX_*`` the **control-plane container** declares. This check's
   RED is the **no-hand-down lane** -- a control plane built before R17, so the
   worker never gets a ceiling and every sized create is refused -- and *not*
   the ``off`` lane, where it stays green: the ceiling and the request-side
   refusals are lane-independent by design (measured: `tmp/task8/red-off.json`
   has ⑧ pass, `tmp/task8/red-oldcp.json` has it fail).
9. **Peak and task unit.** ``memory.max``/``memory.high`` equal the declared
   ``memoryMB`` byte for byte and ``pids.max`` equals **this box's own recorded
   declaration** -- the worker's ``_runtime/<id>/sandbox.json``
   ``max_processes``, read out of the worker container, never hard-coded and
   never a deployment-wide env (the lane's ``E2B_DEFAULT_MAX_PROCESSES`` is
   printed beside it as a secondary cross-check); a held 64 MiB allocation
   shows in ``memory.peak``; and ``pids.current`` counts a **thread** as a task
   exactly like a process (two threads add 2, one forked process adds 1 -- the
   unit Task 5's probe read as ``pids.current = 3`` for 2 threads + 1 process).

The script drives **any** E2B endpoint, so everything lane-specific is a
parameter — and a real cluster run needs **all five** of them (fix round 1,
finding 4: the earlier "just swap two flags" recipe could not work, because
three of the defaults are compose-only):

* ``--api-url`` / ``--api-key``: the E2B API (and gateway) endpoint + key.
* ``--internal-url``: the control plane's **internal** API as reachable *from
  inside a worker container* (compose: ``http://control-plane:3000``; k8s: the
  control-plane Service, i.e. ``http://control-plane.sandlock.svc:3000`` --
  ``e2b-control-plane`` is the NetworkPolicy's name, not the Service's).
  It is only ever called from inside a worker (node-scoped endpoint + source-IP
  second factor), so a host-side value would be wrong even if it resolved.
* ``--internal-key``: the ``X-Internal-Key`` the worker holds
  (``E2B_INTERNAL_API_KEY`` / the per-node ``E2B_INTERNAL_NODE_KEYS``).
* ``--nodes``: the node ids the control plane knows (compose: ``worker-1``…;
  k8s: ``e2b-worker-0``…), used both for the internal per-node record read and
  as the ``{node}`` substitution in ``--worker-exec-template``.
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

  The lane's own switches (``E2B_SANDBOX_CGROUP``, ``E2B_CGROUP_MOUNT``,
  ``E2B_SANDBOX_NOTIFY_RATE_LIMIT``) are read **out of the worker containers**
  at run time and reported per node — never taken from the caller's shell and
  never hard-coded, so the archived artifact cannot claim a lane it did not
  observe.

What a sandbox cgroup's path is differs by lane and is therefore discovered, not
assumed. Both shipped lanes narrow the mount to *this worker's own* parent, by
different devices (k8s: ``subPathExpr: pod$(POD_UID)`` into the pod directory;
compose: a static ``cgroup_parent: /e2b-${COMPOSE_PROJECT_NAME}-worker-<n>``
with the matching bind), so the sandbox sits at
``/pod-cgroup/<container-id>/sbx_<sandbox_id>`` on both. The script still does
not assume that depth: it locates this worker's own delegated container cgroup
first (see ``_OWN_SCRIPT``) and then requires ``sbx_<sandbox_id>`` to be its
direct child, refusing on zero or more than one.

Usage (local lane; the override in ``tmp/`` is what sets ``required`` and the
rate limit):

    E2B_API_URL=http://127.0.0.1:3200 E2B_API_KEY=local-key \\
    python3 deploy/scripts/acceptance/cgroup_acceptance.py \\
        --api-url http://127.0.0.1:3200 \\
        --api-key "$E2B_API_KEY" --internal-key internal-key \\
        --internal-url http://control-plane:3000 \\
        --nodes worker-1,worker-2,worker-3 \\
        --worker-exec-template 'docker exec -i n83acc-{node}-1 bash -lc' \\
        --control-plane-exec-template 'docker exec -i n83acc-control-plane-1 bash -lc'

``--control-plane-exec-template`` (with ``--control-plane-node``, default
``control-plane``) is how check 8 reaches the manifest that declares the
per-sandbox ceilings: ruling R17 moved them to the control plane, and the
expected refusal text is built from what that container declares. The default
template names an ``n83acc``-project control-plane container, so any lane with
another project name passes it explicitly.

It prints one JSON object (every reading, plus ``ok``) and exits non-zero when
any check fails.

A full k0s run -- all five lane flags, with the internal URL and both keys
taken from that cluster (never from this lane's defaults). The control-plane
node is **looked up**, not spelled out: the control plane is a two-replica
Deployment, so its pods are named ``control-plane-<replicaset>-<pod>`` and any
name written here by hand would rot into a ``kubectl exec`` NotFound the first
time the Deployment rolled -- which check 8 reports as
``lane.control_plane_env.error``, i.e. a tooling red that reads like a platform
failure. Every replica runs the same pod spec, so either one answers with the
same ``E2B_MAX_SANDBOX_*``:

    python3 deploy/scripts/acceptance/cgroup_acceptance.py \\
        --api-url "$E2B_API_URL" \\
        --api-key "$E2B_API_KEY" \\
        --internal-url http://control-plane.sandlock.svc.cluster.local:3000 \\
        --internal-key "$E2B_INTERNAL_API_KEY" \\
        --nodes e2b-worker-0,e2b-worker-1 \\
        --worker-exec-template 'kubectl -n sandlock exec {node} -c worker -- bash -lc' \\
        --control-plane-exec-template 'kubectl -n sandlock exec {node} -c control-plane -- bash -lc' \\
        --control-plane-node "$(kubectl -n sandlock get pod -l app=control-plane \\
            -o jsonpath='{.items[0].metadata.name}')"

``--control-plane-exec-template`` has to be passed on any lane whose
control-plane container is not the compose default (``docker exec -i
n83acc-control-plane-1``): check 8 reads the per-sandbox ceilings out of *that*
container, and a template that reaches nothing shows up as
``lane.control_plane_env.error`` -- a tooling gap that would otherwise read like
a platform failure.
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

#: The neighbour round trip's allowance: a small multiple of the quiet
#: baseline (min of five, which already drops the cold connect). Fix round 1,
#: finding 3: the old bound was ``max(2 * quiet, 200ms)`` -- with a ~30 ms
#: baseline that is ~6x, wide enough that a starved neighbour still "passed".
#: 3x with a 50 ms floor is: > 10x below the N82 stall signature (860 ms), and
#: several times the observed jitter (a few ms). What it can detect is gross
#: starvation, not a subtle one -- the report says so in as many words.
_NEIGHBOUR_RTT_FACTOR = 3.0
_NEIGHBOUR_RTT_FLOOR_MS = 50.0

#: The notification cap's signature in the N82 probe. With the cap on, the
#: supervisor sleeps out what is left of the second once its window is spent,
#: so single ops land in the hundreds of milliseconds and the probe's stall
#: counter (a round whose worst op crossed 20 ms) fires about once a second --
#: N82 read **40 stalls in 51 rounds** (the capped ``off`` lane reads the same
#: shape). Off, it does not fire at all (N82's uncapped leg: 18149 op/s, 0
#: stalls; N83's local lane: 9794 op/s, 0 stalls). One round in four is well
#: clear of both, so that is the line check 3 draws.
_CAPPED_STALL_SHARE = 0.25


def flood_is_capped(stalls: int | None, rounds: int | None) -> bool:
    """Does this flood still show the notification cap's stall signature?

    ``True`` for an unread flood as well (``None`` or too few rounds): not
    having looked is not evidence of an uncapped one, and check 3's verdict is
    about the reading.
    """
    if stalls is None or rounds is None or rounds < 4:
        return True
    return stalls > rounds * _CAPPED_STALL_SHARE


#: The nine checks a complete run owes a verdict on: phase 1's five (plan Task
#: 7 Step 1, Phase 1) plus phase 2's four (``docs/superpowers/plans/
#: 2026-10-06-n83-phase2-memory-pids.md``, Task 7 Step 1). A run that is
#: missing any of them is not ``ok``, whatever the ones it did take say --
#: including the RED legs, where a check that could not take its reading must
#: still appear by name.
_EXPECTED_CHECKS = (
    "1_quota_is_real",
    "2_kernel_enforces",
    "3_flood_spends_own_quota",
    "4_narrowing_view_shape",
    "5_own_cpu_max_eacces",
    "6_memory_ceiling_kills",
    "7_task_budget_eagain",
    "8_oversize_named_400",
    "9_peak_and_task_unit",
)


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

    Finding by name alone is not enough on a lane whose mount is shared: before
    the 2026-10-06 narrowing, every compose worker mounted the whole Docker-VM
    tree, so a bare ``sbx_<id>`` search found the directory *from any worker* --
    and "which node hosts this sandbox" is the question check 1 has to answer.
    The owning worker is the one whose own delegated container cgroup holds the
    ``sbx_<id>`` child, which is also exactly what this function requires: it
    resolves that worker's own delegated cgroup first and then demands
    ``sbx_<id>`` be its direct child. Both shipped lanes are narrowed today
    (k8s ``subPathExpr``, compose static ``cgroup_parent`` + matching bind), but
    the rule is the same there, so no lane needs its own spelling.
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


#: The three switches this acceptance's verdict depends on. Read from *inside
#: the worker container* -- never from the caller's shell and never hard-coded:
#: a run must not be able to print a value it did not observe (fix round 1,
#: finding 2: the archived RED artifact, which ran with the lane off, printed
#: "required" because the report field was a canned string).
#:
#: N83 phase 2 / ruling R17: the three per-sandbox ceilings are **not** read
#: here any more. They are the control plane's policy now and they are read out
#: of the *control-plane* container (``control_plane_env`` below) -- the worker
#: containers do not declare them at all, and reading a key the deployment moved
#: is exactly how check 8's expectation follows the move.
_LANE_ENV_SCRIPT = r"""
import json, os, pathlib, sys

WANTED = (
    "E2B_SANDBOX_CGROUP",
    "E2B_CGROUP_MOUNT",
    "E2B_SANDBOX_NOTIFY_RATE_LIMIT",
    # N83 phase 2 / Task 7: the deployment-wide per-sandbox task default, read
    # as a **secondary** datum only. Checks 7/9 take their declared number from
    # the worker's own record for the box (``_runtime/<id>/sandbox.json``),
    # because the shipped manifests never set this key and a check that needs
    # it to be set is the environment satisfying the check; when a lane does
    # set it, the report shows whether the two agree.
    "E2B_DEFAULT_MAX_PROCESSES",
)


def from_proc1():
    try:
        raw = pathlib.Path("/proc/1/environ").read_bytes()
    except OSError:
        return {}
    out = {}
    for entry in raw.split(b"\0"):
        key, _, value = entry.partition(b"=")
        name = key.decode("utf-8", "replace")
        if name in WANTED:
            out[name] = value.decode("utf-8", "replace")
    return out


proc1 = from_proc1()
observed = {}
for name in WANTED:
    if name in proc1:
        observed[name] = {"value": proc1[name], "source": "/proc/1/environ"}
    elif name in os.environ:
        observed[name] = {"value": os.environ[name], "source": "exec env"}
    else:
        observed[name] = {"value": None, "source": "unset"}
print(json.dumps({"pid1": pathlib.Path("/proc/1/cmdline").read_bytes().split(b"\0")[0].decode("utf-8", "replace"),
                  "observed": observed}))
"""


def lane_env(shell: WorkerShell, node: str) -> dict:
    """The three switches as this *worker container* actually sees them."""
    return shell.json(node, "python3 - <<'PY'\n%s\nPY" % _LANE_ENV_SCRIPT)


#: The three per-sandbox ceilings check 8's expected refusal text is built from.
#: N83 phase 2 / ruling R17 moved them to the **control plane**: it is the one
#: that owns the policy, writes it into every node record and quotes it back in
#: the ``400``. So the expectation is read *there* -- never hard-coded, and never
#: from a place the deployment no longer declares them.
_CEILING_ENV_SCRIPT = r"""
import json, os, pathlib

WANTED = (
    "E2B_MAX_SANDBOX_CPU_PERCENT",
    "E2B_MAX_SANDBOX_MEMORY_MB",
    "E2B_MAX_SANDBOX_PROCESSES",
)

proc1 = {}
try:
    for entry in pathlib.Path("/proc/1/environ").read_bytes().split(b"\0"):
        key, _, value = entry.partition(b"=")
        name = key.decode("utf-8", "replace")
        if name in WANTED:
            proc1[name] = value.decode("utf-8", "replace")
except OSError:
    pass

observed = {}
for name in WANTED:
    if name in proc1:
        observed[name] = {"value": proc1[name], "source": "/proc/1/environ"}
    elif name in os.environ:
        observed[name] = {"value": os.environ[name], "source": "exec env"}
    else:
        observed[name] = {"value": None, "source": "unset"}
print(json.dumps({
    "pid1": pathlib.Path("/proc/1/cmdline").read_bytes().split(b"\0")[0].decode("utf-8", "replace"),
    "observed": observed,
}))
"""


def control_plane_env(template: str, node: str, *, timeout: float = 60.0) -> dict:
    """The three per-sandbox ceilings as the **control plane** container sees them.

    The lane names how to reach that container (``--control-plane-exec-template``,
    with ``{node}`` substituted by ``--control-plane-node``) for the same reason
    the worker reads use a template: the script is lane-neutral, and on the k8s
    lane reaching the control-plane pod is a different incantation from the
    compose ``docker exec``.
    """
    argv = shlex.split(template.replace("{node}", node))
    argv.append("python3 - <<'PY'\n%s\nPY" % _CEILING_ENV_SCRIPT)
    done = _run(argv, timeout=timeout)
    if done.returncode != 0:
        raise Refusal(
            f"control-plane command on {node} exited {done.returncode}: "
            f"{done.stderr.strip()[:400] or done.stdout.strip()[:400]}"
        )
    out = done.stdout.strip()
    payload = out.splitlines()[-1] if out else ""
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise Refusal(
            f"control-plane command on {node} did not end in a JSON line: {out!r}"
        ) from exc


#: The lane-neutral identity of "this worker's own container cgroup": the unique
#: directory this worker drained itself into and enabled cpu on -- holder of a
#: ``worker/`` child and of ``cpu`` in ``cgroup.subtree_control`` (plan §3.5 /
#: §4). Both lanes narrow the mount to this worker's own parent, so it is a
#: direct child of the mount root on both (k8s: the ``subPathExpr``-narrowed
#: pod directory; compose: the static ``cgroup_parent`` slice). Before the
#: 2026-10-06 compose narrowing it sat under ``docker/<container-id>`` in the
#: whole VM tree -- a shape where a bare name search matched from *every*
#: worker, since they all mounted the same tree. A name-based rule would still
#: be wrong on one lane or the other, which is why the identity is the
#: ``worker/`` + ``cpu`` + owner-uid triple below.
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


# *Peer containers*: the other entries next to our own container cgroup under
# the same parent (compose: `/pod-cgroup/docker/<other-container-id>`; k8s: a
# sibling container directory under the pod directory, when the pod has any).
# Deliberately **not** a top-down walk of the mount: the mounted root itself is
# a cgroup with a `cpu.max`, so a walk that stops at the first hit stops at the
# root and never reaches a peer (that was fix round 1's Spec finding).
peers = []
for sibling in sorted(own.parent.iterdir()):
    if sibling == own or not sibling.is_dir():
        continue
    if (sibling / "cpu.max").exists() and (sibling / "cgroup.procs").exists():
        peers.append(sibling)

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
peer_facts = []
for peer in peers:
    peer_facts.append(
        {
            "path": str(peer),
            "owner": owner(peer),
            # True when this peer's *directory* was chowned to our own uid --
            # i.e. it is another worker's delegated cgroup on a lane where
            # every worker runs as the same host uid. Reported, never hidden:
            # uid-based DAC cannot tell those apart (see the docstring).
            "delegated_to_our_uid": peer.stat().st_uid == os.geteuid(),
            "cpu_max": write_probe(peer / "cpu.max"),
            "cgroup_procs": write_probe(peer / "cgroup.procs"),
            "subtree_control": write_probe(peer / "cgroup.subtree_control"),
            "mkdir": mkdir_probe(peer / "n83_acceptance_probe"),
        }
    )

# The mounted root is *not* a peer container (on compose it is the Docker VM's
# cgroup root, owner 0:0); it is probed on its own so the two are never
# conflated again. The boolean below says only what it measures -- that this
# directory carries the two kernfs files a container cgroup has -- because
# "is_container_cgroup" claimed more than the probe can know (fix round 2,
# residual 4: the VM's cgroup root carries them both and is not a container).
mount_root_facts = {
    "path": str(mount),
    "owner": owner(mount),
    "has_cpu_max_and_procs": (mount / "cpu.max").exists() and (mount / "cgroup.procs").exists(),
    "cpu_max": write_probe(mount / "cpu.max"),
    "cgroup_procs": write_probe(mount / "cgroup.procs"),
    "mkdir": mkdir_probe(mount / "n83_acceptance_probe"),
}

top = sorted(p.name for p in mount.iterdir())
print(json.dumps({
    "mount": str(mount),
    "ls_mount": top,
    "ls_mount_count": len(top),
    "proc_self_cgroup": pathlib.Path("/proc/self/cgroup").read_text().strip(),
    "hostname": hostname,
    "own": own_facts,
    "peer_containers": peer_facts,
    "mount_root": mount_root_facts,
}))
"""


def read_view(shell: WorkerShell, node: str, mount: str, own: str) -> dict:
    script = "python3 - <<'PY'\n%s\nPY" % (
        _VIEW_SCRIPT.replace("__MOUNT__", json.dumps(mount)).replace(
            "__OWN__", json.dumps(own)
        )
    )
    return shell.json(node, script)


# -- phase 2: the memory / pids readings -----------------------------------
#
# Everything a check needs to turn "the kernel is enforcing this" into a
# number: one worker-side read of the box's own limit files and event
# counters (same device as ``read_cpu_stat``: the worker is the only role whose
# delegated view holds the box).

#: Check 6's box. A 64 MiB sandbox makes "allocate past the budget" a
#: sub-second reading instead of a multi-GiB one, and the declared number is
#: what the kernel's ``memory.max`` is compared against.
_HOG_MEMORY_MB = 64
#: Check 7's box. Big enough that the *task* wall is the only thing that can
#: stop the fork bomb (a memory wall would be a different reading).
_FORK_MEMORY_MB = 512
#: Check 9's box, and the allocation held inside it: the ceiling stays far
#: away, so ``memory.peak`` is a reading about the allocation, not the wall.
_PEAK_MEMORY_MB = 256
_PEAK_ALLOC_BYTES = 64 * 1024 * 1024
#: What a shell reports for a child the kernel OOM-killed (128 + SIGKILL).
_SIGKILL_EXIT = 128 + 9
#: ``EAGAIN`` **as the sandbox's kernel defines it**. The reading is the
#: Linux sandbox's ``errno``, so this must not come from the harness host's
#: ``errno`` module: on macOS ``errno.EAGAIN`` is 35, and comparing the
#: sandbox's 11 against it would report a correct EAGAIN as a failure.
_LINUX_EAGAIN = 11

_LIMITS_SCRIPT = r"""
import json, pathlib

p = pathlib.Path(__CGROUP_PATH__)


def rd(name):
    try:
        return (p / name).read_text().strip()
    except OSError as exc:
        return "ERR:%s" % exc


def events(name):
    out = {}
    try:
        for line in (p / name).read_text().splitlines():
            key, _, value = line.partition(" ")
            if value:
                out[key] = int(value)
    except OSError as exc:
        out = {"ERR": str(exc)}
    return out


print(json.dumps({
    "path": str(p),
    "memory_max": rd("memory.max"),
    "memory_high": rd("memory.high"),
    "memory_current": rd("memory.current"),
    "memory_peak": rd("memory.peak"),
    "memory_events": events("memory.events"),
    "pids_max": rd("pids.max"),
    "pids_current": rd("pids.current"),
    "pids_events": events("pids.events"),
    "cgroup_procs": len((p / "cgroup.procs").read_text().split()),
}))
"""


def read_limits(shell: WorkerShell, node: str, cgroup_path: str) -> dict:
    """The box's memory/pids limits, its peak, and both event counters.

    ``memory.max``/``memory.high``/``pids.max`` are the kernel's own readback
    of what the worker wrote; ``memory.events``/``pids.events`` are the
    counters Task 5 ships. Deliberately one read: the three limits and the
    counters come from the same instant of the same directory.
    """
    script = "python3 - <<'PY'\n%s\nPY" % (
        _LIMITS_SCRIPT.replace("__CGROUP_PATH__", json.dumps(cgroup_path))
    )
    return shell.json(node, script)


#: The worker's *own* per-sandbox runtime record, read inside the worker
#: container: ``<state base>/_runtime/<id>/sandbox.json`` (``envd_service/
#: runtime/registry.py``: ``_record_path`` / ``sandbox_record_path``, the same
#: file the worker's own runtime reads back). The create's payload was applied
#: into it, so its ``max_processes`` is **what this box was declared** -- the
#: number checks 7/9 compare the kernel's ``pids.max`` against. The base is
#: resolved exactly as ``gateway_common.paths.resolve_state_base`` does:
#: ``E2B_STATE_BASE`` when set (the k8s manifests sink the tree root), the
#: workspace base otherwise; both candidates are tried. Only the fields this
#: check reads are projected out of the record -- it also holds the sandbox's
#: access token, which has no business in an acceptance report.
_RECORD_SCRIPT = r"""
import json, os, pathlib

sandbox_id = __SANDBOX_ID__
WANTED = ("sandbox_id", "max_processes", "memory_mb", "cpu_percent", "disk_mb", "state")
roots = []
for name in ("E2B_STATE_BASE", "E2B_WORKSPACE_BASE"):
    value = (os.environ.get(name) or "").strip()
    if value and value not in roots:
        roots.append(value)
candidates = [
    pathlib.Path(root) / "_runtime" / sandbox_id / "sandbox.json" for root in roots
]
out = {
    "sandbox_id": sandbox_id,
    "bases": roots,
    "candidates": [str(path) for path in candidates],
}
for path in candidates:
    if path.is_file():
        out["path"] = str(path)
        raw = json.loads(path.read_text())
        out["record"] = {key: raw[key] for key in WANTED if key in raw}
        break
print(json.dumps(out))
"""


def read_worker_record(shell: WorkerShell, node: str, sandbox_id: str) -> dict:
    """This sandbox's own record, as the hosting worker wrote it.

    A missing record is a named refusal, never a default: the declared number
    is the whole point of the comparison it feeds.
    """
    if not _SANDBOX_ID_RE.match(sandbox_id):
        raise Refusal(f"refusing to interpolate a non-sandbox id: {sandbox_id!r}")
    script = "python3 - <<'PY'\n%s\nPY" % (
        _RECORD_SCRIPT.replace("__SANDBOX_ID__", json.dumps(sandbox_id))
    )
    payload = shell.json(node, script)
    if not payload.get("record"):
        raise Refusal(
            f"the worker on {node} holds no runtime record for {sandbox_id} "
            f"(tried {payload.get('candidates')})"
        )
    return payload


#: A process that touches fresh anonymous memory until the kernel takes it out.
_HOG_SCRIPT = r"""
blocks = []
while True:
    block = bytearray(4 * 1024 * 1024)
    for offset in range(0, len(block), 4096):
        block[offset] = 1
    blocks.append(block)
"""

#: ``hog_exit`` is the *only* thing the shell says, and it says it after the
#: allocator is gone -- so the reading is "the child ended with 137", not "the
#: command failed somehow". When the shell does not get to say anything (the
#: OOM kill races the command's own bookkeeping), the e2b result carries the
#: kill instead; check 6 reads either shape, exactly (see ``run_phase2_checks``).
_HOG_COMMAND = (
    "cat > hog.py <<'PYEOF'\n"
    + _HOG_SCRIPT
    + "PYEOF\npython3 hog.py; printf '{\"hog_exit\": %d}\\n' $?"
)

#: Fork until the box refuses, report what the *caller* saw, then hold the
#: children so the harness can read ``pids.current`` while the box is full
#: (``__PACE__`` spaces the forks so a poller can watch the wall arrive).
_FORK_SCRIPT = r"""
import errno, json, os, signal, time

kids = []
err = None
for _ in range(600):
    try:
        pid = os.fork()
    except OSError as exc:
        err = exc.errno
        break
    if pid == 0:
        signal.pause()
        os._exit(0)
    kids.append(pid)
    time.sleep(__PACE__)
print(json.dumps({"forks": len(kids), "errno": err}), flush=True)
time.sleep(__SECONDS__)
for pid in kids:
    try:
        os.kill(pid, 9)
    except OSError:
        pass
"""

#: ``threads`` Python threads and ``procs`` forked children, then hold -- the
#: one program check 9 varies to read the unit ``pids.current`` counts in.
#: Run with ``python3 -c`` (no heredoc, no ``cat``): the command's own plumbing
#: is then exactly the shell envd spawns plus this interpreter, and nothing
#: else that could be counted as a task while the reading is taken.
_HOLD_SCRIPT = r"""
import json, os, sys, threading, time

threads = __THREADS__
procs = __PROCS__
for _ in range(procs):
    pid = os.fork()
    if pid == 0:
        time.sleep(10)
        os._exit(0)
for _ in range(threads):
    threading.Thread(target=time.sleep, args=(12,)).start()
print(json.dumps({"pid": os.getpid()}), flush=True)
time.sleep(12)
"""


def hold_command(threads: int, procs: int) -> str:
    """``python3 -c`` with the holder's counts baked in (see ``_HOLD_SCRIPT``)."""
    code = _HOLD_SCRIPT.replace("__THREADS__", str(threads)).replace(
        "__PROCS__", str(procs)
    )
    return "python3 -c " + shlex.quote(code)


#: Allocate ``argv[1]`` bytes of fresh anonymous memory, touch every page, and
#: hold -- so ``memory.peak`` moves and ``memory.current`` stays there.
_ALLOC_HOLD_SCRIPT = r"""
import sys, time

size = int(sys.argv[1])
block = bytearray(size)
for offset in range(0, len(block), 4096):
    block[offset] = 1
print("allocated", len(block), flush=True)
time.sleep(int(sys.argv[2]))
"""


def api_request(
    api_url: str,
    api_key: str,
    *,
    method: str = "GET",
    path: str,
    body: dict | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict | None, str]:
    """One raw HTTP call to the E2B API: ``(status, json, raw text)``.

    A refusal is a *reading* on this path, never an exception: checks 8's
    whole point is the difference between a ``400``, a ``503`` and a ``201``,
    and an ``HTTPError`` carries the body the same way the success path does.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None
    return status, payload, raw


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
        "--control-plane-exec-template",
        default=os.environ.get(
            "N83_ACC_CP_EXEC", "docker exec -i n83acc-control-plane-1 bash -lc"
        ),
        help=(
            "how to run a shell script inside the control-plane container (check 8 "
            "reads the per-sandbox ceilings there since ruling R17); '{node}' is "
            "substituted with --control-plane-node when present"
        ),
    )
    parser.add_argument(
        "--control-plane-node",
        default=os.environ.get("N83_ACC_CP_NODE", "control-plane"),
        help=(
            "the name '{node}' takes in --control-plane-exec-template (compose: "
            "the container name, the default; k8s: the control-plane *pod*, which "
            "is a Deployment pod named control-plane-<rs>-<pod> -- look it up with "
            "'kubectl -n sandlock get pod -l app=control-plane')"
        ),
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
            "control_plane_exec_template": args.control_plane_exec_template,
            "control_plane_node": args.control_plane_node,
            "nodes": nodes,
            "cgroup_mount": args.cgroup_mount,
            "template": args.template,
            "flood_seconds": args.flood_seconds,
            # Filled in below from inside the workers -- see lane_env().
            "worker_env": {},
            # ... and from inside the control plane -- the per-sandbox ceilings
            # check 8's expected refusal text is built from (ruling R17).
            "control_plane_env": {},
        },
        "n82_baseline": _N82_BASELINE,
        "checks": {},
        "sandboxes": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    started = time.monotonic()
    boxes: list[tuple[object, dict]] = []

    # The lane's own switches, read out of the worker containers (finding 2):
    # a value the run could not observe is never printed.
    for node in nodes:
        try:
            report["lane"]["worker_env"][node] = lane_env(shell, node)
        except Refusal as exc:
            report["lane"]["worker_env"][node] = {"error": str(exc)}
    # ...and the three per-sandbox ceilings out of the control plane (R17):
    # same rule, same reason -- the number check 8 quotes has to be one the run
    # observed, in the container that owns it.
    try:
        report["lane"]["control_plane_env"] = control_plane_env(
            args.control_plane_exec_template, args.control_plane_node
        )
    except Refusal as exc:
        report["lane"]["control_plane_env"] = {"error": str(exc)}

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

    def create_sized(metadata: dict, *, cpu_count: int, memory_mb: int):
        """Create a sandbox through the *public* API, naming its own size.

        The SDK's ``Sandbox.create`` has no ``cpuCount``/``memoryMB``
        arguments, so checks 6/9 drive the endpoint the client's own JSON
        would: a raw ``POST /sandboxes``. The create's own answer is a thin
        handle (id + token), so the *declaration* is read back with
        ``GET /sandboxes/{id}`` -- the record's own ``cpuCount``/``memoryMB``,
        which is what the kernel's readback is then compared against, never a
        number this script hoped it sent.
        """
        status, payload, raw = api_request(
            args.api_url,
            args.api_key,
            method="POST",
            path="/sandboxes",
            body={
                "templateID": args.template,
                "timeout": args.sandbox_timeout_s,
                "metadata": metadata,
                "cpuCount": cpu_count,
                "memoryMB": memory_mb,
            },
        )
        if status != 201 or not isinstance(payload, dict) or not payload.get("sandboxID"):
            raise Refusal(f"sized create answered {status}: {raw[:300]}")
        info_status, declared, info_raw = api_request(
            args.api_url,
            args.api_key,
            path=f"/sandboxes/{payload['sandboxID']}",
        )
        if info_status != 200 or not isinstance(declared, dict):
            raise Refusal(
                f"the record for {payload['sandboxID']} could not be read back "
                f"({info_status}): {info_raw[:200]}"
            )
        sandbox = Sandbox.connect(
            payload["sandboxID"],
            api_url=args.api_url,
            sandbox_url=args.api_url,
            api_key=args.api_key,
        )
        boxes.append((sandbox, {"declared": declared}))
        return sandbox, declared

    def declared_processes(node: str, sandbox_id: str) -> tuple[int, dict]:
        """What **this box** was declared, from the worker's own record.

        Checks 7/9 compare the kernel's ``pids.max`` with this number, so it
        has to be the value the create actually agreed to -- not the script's
        idea of it and not a deployment-wide env. The worker holds exactly that
        in ``<state base>/_runtime/<id>/sandbox.json`` (``max_processes``,
        written when the create's payload was applied, and the same file the
        worker's own runtime reads back), so the comparison runs
        request -> worker record -> kernel: the same three-way shape checks
        6/9 already use for memory via ``GET /sandboxes/{id}``.

        The lane's ``E2B_DEFAULT_MAX_PROCESSES`` (when a lane sets it at all)
        is reported beside it as a *secondary* datum and is never the source:
        a shipped manifest that does not declare it must still be measurable.
        """
        info = read_worker_record(shell, node, sandbox_id)
        record = info.get("record") or {}
        value = record.get("max_processes")
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise Refusal(
                f"the worker's record for {sandbox_id} carries no usable "
                f"max_processes (read {value!r}) in {info.get('path')}"
            )
        return value, info

    def declared_ceiling(node: str, key: str, unit: int) -> int:
        """One dimension of the node's per-sandbox ceiling, read in-container.

        Check 8's refusal text quotes the node's promise, so the expected text
        is built from the same ``E2B_MAX_SANDBOX_*`` the control plane handed
        down -- read out of the **control-plane** container (ruling R17 moved
        the three keys there; a worker no longer declares them), so a lane that
        moved a ceiling moves the expectation with it. ``node`` is kept in the
        signature because the failure names which reading is missing.
        """
        observed = (
            report["lane"]["control_plane_env"].get("observed", {})
            if isinstance(report["lane"]["control_plane_env"], dict)
            else {}
        )
        value = (observed.get(key) or {}).get("value")
        if not isinstance(value, str) or not value.strip().isdigit() or int(value) < 1:
            raise Refusal(
                f"the lane's control plane does not declare {key} (read {value!r}), "
                "so check 8 cannot know which ceiling the refusal should quote"
            )
        return int(value) // unit

    def lane_declared_processes(node: str):
        """The lane's ``E2B_DEFAULT_MAX_PROCESSES``, or ``None``.

        **Secondary only** (see ``declared_processes``): reported so a reader
        can see whether the deployment's env agrees with the worker's record,
        never used as the number the kernel's ``pids.max`` is compared against
        -- the shipped manifests do not declare it, and a check whose reading
        depends on an env the lane happens to set is the environment
        satisfying the check.
        """
        observed = report["lane"]["worker_env"].get(node, {}).get("observed", {})
        value = (observed.get("E2B_DEFAULT_MAX_PROCESSES") or {}).get("value")
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return None

    def neighbour_on(node: str, *, label: str, memory_mb: int, cpu_count: int = 1):
        """A second sandbox on ``node`` -- the neighbour half of checks 6/7.

        Placement balances by remaining capacity (``rank_candidates``), so a
        fresh box deliberately avoids the node that already carries one. The
        search therefore *fills* the other nodes instead of killing its
        mistakes: a node admits two 100%-cpu boxes, and by the pigeonhole a
        same-node one appears once the others are full. The candidates that
        landed elsewhere are real boxes with real readings and are reported;
        they are freed with the rest of this check's boxes.
        """
        for _ in range(6):
            box, _declared = create_sized(
                {"n83_acceptance": label}, cpu_count=cpu_count, memory_mb=memory_mb
            )
            box.commands.run("true")
            try:
                box_node, box_cgroup = locate(box.sandbox_id)
            except Refusal:
                box_node, box_cgroup = None, None
            if box_node == node:
                return box, box_cgroup
            note_box(box, box_node, box_cgroup)
        raise Refusal(f"no sandbox landed on {node} for the neighbour half of {label}")

    def kill_created_since(marker: int) -> None:
        """Free the boxes one check created, once its readings are taken.

        The fleet admits two 100%-cpu sandboxes per node (the node's own 200%
        ceiling), so a check that held its boxes would leave the *next* check
        unable to place a neighbour on the same node -- the opposite of what
        the neighbour readings mean. Killing here (rather than at the end of
        the run) is also what the readings want: each check measures a fresh
        box on a quiet node.
        """
        while len(boxes) > marker:
            sandbox, _info = boxes.pop()
            kill(sandbox)

    def parse_json_line(output: str, key: str):
        """The last JSON line's ``key``, or ``None`` -- a reading, not a guess."""
        for line in reversed((output or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if key in payload:
                return payload[key]
        return None

    def hold_reading(
        box,
        node: str,
        cgroup: str,
        *,
        threads: int,
        procs: int,
        window_s: float = 8.0,
    ) -> dict:
        """Run the holder with N threads / N forked children, and read the box.

        The reading is taken *while* the holder is alive (a background thread
        blocks on the command), because ``pids.current`` is a snapshot: what
        matters is what it reaches with the extra tasks present, not what it is
        after they exit.

        The unit is read as the **difference between two same-shape holders**
        (no extras / two threads / one forked process), so the command's own
        plumbing -- and anything still draining from the previous holder --
        cancels out. Do not read an absolute number instead: measured, the box
        does not return to its quiet idle count between holders (idle 6, the
        next holder's steady window 8), so an absolute reading would be a
        statement about the plumbing. The *steady* minimum of this holder's
        window is used (the first seconds still carry that plumbing's
        transient); the peak and the pre-holder reading are kept for the
        record.
        """
        holder: dict = {}

        def run_holder() -> None:
            holder["result"] = box.commands.run(
                hold_command(threads, procs),
                timeout=180,
            )

        pre_holder = read_limits(shell, node, cgroup)
        thread = threading.Thread(target=run_holder, daemon=True)
        thread.start()
        started = time.monotonic()
        peak_tasks = 0
        peak_procs = 0
        steady: list[int] = []
        while time.monotonic() - started < window_s:
            time.sleep(0.5)
            try:
                sample = read_limits(shell, node, cgroup)
            except Refusal:
                break
            tasks = int(sample["pids_current"]) if sample["pids_current"].isdigit() else 0
            if tasks > peak_tasks:
                peak_tasks = tasks
                peak_procs = int(sample["cgroup_procs"])
            # The command's plumbing is on its way out for the first couple of
            # seconds; the steady window is what the unit is read from.
            if time.monotonic() - started >= 3.0:
                steady.append(tasks)
        thread.join(timeout=60)
        return {
            "threads": threads,
            "forked_processes": procs,
            "pre_holder_pids_current": pre_holder["pids_current"],
            "pre_holder_cgroup_procs": pre_holder["cgroup_procs"],
            "steady_pids_current": min(steady) if steady else None,
            "steady_samples": steady,
            "peak_pids_current": peak_tasks,
            "cgroup_procs_at_peak": peak_procs,
            "holder_output": (getattr(holder.get("result"), "stdout", "") or "").strip()[:120],
        }

    def run_phase2_checks() -> None:
        """Checks 6-9: what the kernel does to a live sandbox (N83 phase 2).

        Run *before* checks 1-5 and each in its own ``try``: on a lane whose
        phase-1 lane is absent -- ``E2B_SANDBOX_CGROUP=off``, or the
        pre-R17 window where the worker never receives a ceiling (a control
        plane that does not hand one down) -- a refusal here is a *named failed
        reading* for that check, not a reason to stop before the phase-2 checks
        have said what they saw.
        """
        # ---- check 6: the memory ceiling kills, and only the allocator ----
        marker = len(boxes)
        try:
            hog, hog_declared = create_sized(
                {"n83_acceptance": "hog"}, cpu_count=1, memory_mb=_HOG_MEMORY_MB
            )
            hog.commands.run("true")  # the first command is what builds the slot
            hog_node, hog_cgroup = locate(hog.sandbox_id)
            if hog_cgroup is None:
                raise Refusal(
                    "no sbx_<id> cgroup under the hosting worker's own delegated "
                    "cgroup (E2B_SANDBOX_CGROUP off?), so there is no memory.max "
                    "to read and no kernel to kill the allocator"
                )
            note_box(hog, hog_node, hog_cgroup)
            declared_bytes = str(int(hog_declared["memoryMB"]) * 1024 * 1024)
            before = read_limits(shell, hog_node, hog_cgroup)
            neighbour, neighbour_cgroup = neighbour_on(
                hog_node, label="hog-neighbour", memory_mb=_HOG_MEMORY_MB
            )
            quiet_rtt = client_roundtrip_ms(neighbour)
            # Two shapes of the same reading. When the shell that ran the
            # allocator survives -- it has almost no memory of its own -- it
            # reports ``128 + SIGKILL`` in ``hog_exit``; when the command's own
            # result carries the kill instead, e2b renders it as
            # ``CommandExitException ... Killed``. Both say "the kernel killed
            # the allocator", and which one arrives is a race inside the
            # sandbox's command plumbing (measured: both, alternating), so the
            # check reads either, exactly, and never a substring.
            #
            # The box is sampled *while* the hog runs, not only after it: an
            # OOM kill can cost the route-B slot its control stream, and the
            # worker then rebuilds the instance -- which is a fresh
            # ``sbx_<id>`` directory with its counters back at zero (measured:
            # the control plane logged ``oom_kill grew from 0 to 1`` for a box
            # whose directory read ``oom_kill: 0`` a moment later). The peak
            # over the live samples is therefore the reading; the reset itself
            # is reported.
            hog_stdout = ""
            command_killed = False
            hog_result: dict = {}

            def run_hog() -> None:
                try:
                    hog_result["run"] = hog.commands.run(_HOG_COMMAND, timeout=180)
                except Exception as exc:  # noqa: BLE001 - a signalled command is a reading
                    hog_result["error"] = exc

            hog_thread = threading.Thread(target=run_hog, daemon=True)
            hog_thread.start()
            live: list[dict] = []
            while hog_thread.is_alive():
                try:
                    live.append(read_limits(shell, hog_node, hog_cgroup))
                except Refusal:
                    break
                time.sleep(0.3)
            hog_thread.join(timeout=200)
            hog_run = hog_result.get("run")
            if hog_run is not None:
                hog_stdout = hog_run.stdout or ""
            elif hog_result.get("error") is not None:
                command_killed = (
                    str(hog_result["error"]).rstrip().splitlines()[-1].strip()
                    == "Killed"
                )
            hog_exit = parse_json_line(hog_stdout, "hog_exit")
            oom_kill_peak = max(
                (entry["memory_events"].get("oom_kill", 0) for entry in live), default=0
            )
            oom_group_kill_peak = max(
                (entry["memory_events"].get("oom_group_kill", 0) for entry in live),
                default=0,
            )
            counter_reset = any(
                later["memory_events"].get("oom_kill", 0)
                < earlier["memory_events"].get("oom_kill", 0)
                for earlier, later in zip(live, live[1:])
            )
            # D3's other half, read directly: the rest of the box is still
            # there and still answering.
            try:
                box_alive = (
                    hog.commands.run("echo box-alive", timeout=60).stdout.strip()
                    == "box-alive"
                )
            except Exception:  # noqa: BLE001 - a dead box is the reading
                box_alive = False
            busy_rtt = client_roundtrip_ms(neighbour)
            after = read_limits(shell, hog_node, hog_cgroup)
            bound_ms = max(
                _NEIGHBOUR_RTT_FACTOR * quiet_rtt["min_ms"], _NEIGHBOUR_RTT_FLOOR_MS
            )
            record(
                "6_memory_ceiling_kills",
                before["memory_max"] == declared_bytes
                and before["memory_high"] == declared_bytes
                and (hog_exit == _SIGKILL_EXIT or command_killed)
                and oom_kill_peak == 1
                and oom_group_kill_peak == 0
                and box_alive
                and busy_rtt["min_ms"] <= bound_ms,
                criterion=(
                    "memory.max/memory.high == the declared memoryMB (byte for byte); "
                    "the allocator is SIGKILLed (the surviving shell reports "
                    "128+SIGKILL, or the command's own result carries the kill); the "
                    "box's own memory.events shows oom_kill=1 and oom_group_kill=0 "
                    "(D3: only the allocator dies), read live because an OOM kill "
                    "can cost the slot its control stream and the rebuilt box "
                    "starts its counters at zero; the box still answers a command; "
                    "the same-node neighbour's round trip stays within "
                    f"{_NEIGHBOUR_RTT_FACTOR:g}x its quiet min (floor "
                    f"{_NEIGHBOUR_RTT_FLOOR_MS:g} ms)"
                ),
                declared_memory_mb=hog_declared["memoryMB"],
                declared_bytes=int(declared_bytes),
                hog_exit=hog_exit,
                hog_command_killed=command_killed,
                hog_stdout=hog_stdout.strip()[:200],
                box_still_answers=box_alive,
                live_samples=len(live),
                oom_kill_peak=oom_kill_peak,
                oom_group_kill_peak=oom_group_kill_peak,
                memory_events_counter_reset=counter_reset,
                memory_peak_live_max=max(
                    (int(entry["memory_peak"]) for entry in live if entry["memory_peak"].isdigit()),
                    default=None,
                ),
                memory_max_before=before["memory_max"],
                memory_high_before=before["memory_high"],
                memory_peak_after=after["memory_peak"],
                memory_events_after=after["memory_events"],
                sandbox_cgroup=hog_cgroup,
                hog_node=hog_node,
                neighbour_node=hog_node,
                neighbour_quiet_rtt=quiet_rtt,
                neighbour_busy_rtt=busy_rtt,
                neighbour_bound_ms=round(bound_ms, 2),
            )
        except Exception as exc:  # noqa: BLE001 - a failed reading is a reading
            record("6_memory_ceiling_kills", False, reason=f"{type(exc).__name__}: {exc}")
        finally:
            kill_created_since(marker)

        # ---- check 7: the task ceiling answers EAGAIN, from the kernel -----
        marker = len(boxes)
        try:
            forker, _fork_declared = create_sized(
                {"n83_acceptance": "forkbomb"}, cpu_count=1, memory_mb=_FORK_MEMORY_MB
            )
            forker.commands.run("true")
            fork_node, fork_cgroup = locate(forker.sandbox_id)
            if fork_cgroup is None:
                raise Refusal(
                    "no sbx_<id> cgroup under the hosting worker's own delegated "
                    "cgroup (E2B_SANDBOX_CGROUP off?), so there is no pids.max, no "
                    "pids.current and no pids.events to read -- and an EAGAIN from "
                    "the mediator's own process counter would prove nothing about "
                    "the kernel"
                )
            note_box(forker, fork_node, fork_cgroup)
            declared_tasks, declared_record = declared_processes(
                fork_node, forker.sandbox_id
            )
            before = read_limits(shell, fork_node, fork_cgroup)
            neighbour, neighbour_cgroup = neighbour_on(
                fork_node, label="fork-neighbour", memory_mb=_HOG_MEMORY_MB
            )
            quiet_rtt = client_roundtrip_ms(neighbour)
            script = _FORK_SCRIPT.replace("__SECONDS__", "20").replace("__PACE__", "0.05")
            holder: dict = {}

            def run_forker() -> None:
                holder["result"] = forker.commands.run(
                    "cat > forker.py <<'PYEOF'\n" + script + "PYEOF\npython3 forker.py",
                    timeout=180,
                )

            thread = threading.Thread(target=run_forker, daemon=True)
            thread.start()
            peak_tasks = 0
            peak_procs = 0
            pids_events_max_peak = 0
            pids_events_reset = False
            previous_max = 0
            while thread.is_alive():
                time.sleep(0.4)
                try:
                    sample = read_limits(shell, fork_node, fork_cgroup)
                except Refusal:
                    break
                tasks = int(sample["pids_current"]) if sample["pids_current"].isdigit() else 0
                if tasks > peak_tasks:
                    peak_tasks = tasks
                    peak_procs = int(sample["cgroup_procs"])
                seen = sample["pids_events"].get("max", 0)
                pids_events_max_peak = max(pids_events_max_peak, seen)
                pids_events_reset = pids_events_reset or seen < previous_max
                previous_max = seen
            thread.join(timeout=120)
            busy_rtt = client_roundtrip_ms(neighbour)
            after = read_limits(shell, fork_node, fork_cgroup)
            result = holder.get("result")
            forks = parse_json_line(getattr(result, "stdout", "") or "", "forks")
            errno_read = parse_json_line(getattr(result, "stdout", "") or "", "errno")
            bound_ms = max(
                _NEIGHBOUR_RTT_FACTOR * quiet_rtt["min_ms"], _NEIGHBOUR_RTT_FLOOR_MS
            )
            record(
                "7_task_budget_eagain",
                before["pids_max"] == str(declared_tasks)
                and errno_read == _LINUX_EAGAIN
                and peak_tasks == declared_tasks
                # The kernel's wall, not the mediator's: at the moment the box
                # held the most tasks, it held *fewer processes* than the same
                # budget -- the mediator does not count threads, so a mediator
                # refusal could not have produced this reading.
                and peak_procs < declared_tasks
                and pids_events_max_peak >= 1
                and busy_rtt["min_ms"] <= bound_ms,
                criterion=(
                    "pids.max == this box's own recorded declaration (the worker's "
                    "_runtime/<id>/sandbox.json max_processes; the lane's "
                    "E2B_DEFAULT_MAX_PROCESSES is printed alongside, never the "
                    "criterion); the fork bomb's own errno is EAGAIN; pids.current "
                    "reaches pids.max exactly while cgroup.procs is still below it "
                    "(the kernel's task wall, which counts threads -- the mediator's "
                    "process counter cannot produce this); pids.events.max grows; a "
                    "same-node neighbour keeps its round trip"
                ),
                declared_processes=declared_tasks,
                declared_processes_source=declared_record.get("path"),
                worker_record=declared_record.get("record"),
                lane_env_declared_processes=lane_declared_processes(fork_node),
                pids_max_before=before["pids_max"],
                forks=forks,
                errno=errno_read,
                peak_pids_current=peak_tasks,
                cgroup_procs_at_peak=peak_procs,
                pids_events_after=after["pids_events"],
                pids_events_max_peak=pids_events_max_peak,
                pids_events_counter_reset=pids_events_reset,
                forker_stdout=(getattr(result, "stdout", "") or "").strip()[:200],
                sandbox_cgroup=fork_cgroup,
                fork_node=fork_node,
                neighbour_quiet_rtt=quiet_rtt,
                neighbour_busy_rtt=busy_rtt,
                neighbour_bound_ms=round(bound_ms, 2),
            )
        except Exception as exc:  # noqa: BLE001 - a failed reading is a reading
            record("7_task_budget_eagain", False, reason=f"{type(exc).__name__}: {exc}")
        finally:
            kill_created_since(marker)

        # ---- check 8: an over-ceiling create is a named 400 ---------------
        marker = len(boxes)
        try:
            ceiling_node = nodes[0]
            cpu_ceiling = declared_ceiling(
                ceiling_node, "E2B_MAX_SANDBOX_CPU_PERCENT", 100
            )
            memory_ceiling = declared_ceiling(
                ceiling_node, "E2B_MAX_SANDBOX_MEMORY_MB", 1
            )
            cpu_status, cpu_body, cpu_raw = api_request(
                args.api_url,
                args.api_key,
                method="POST",
                path="/sandboxes",
                body={"templateID": args.template, "cpuCount": cpu_ceiling * 4},
            )
            mem_status, mem_body, mem_raw = api_request(
                args.api_url,
                args.api_key,
                method="POST",
                path="/sandboxes",
                body={"templateID": args.template, "memoryMB": memory_ceiling * 4 + 1},
            )
            # The boundary: *at* the ceiling must not be refused (otherwise
            # "refuses everything" would pass this check).
            ok_status, ok_body, ok_raw = api_request(
                args.api_url,
                args.api_key,
                method="POST",
                path="/sandboxes",
                body={
                    "templateID": args.template,
                    "timeout": 60,
                    "cpuCount": cpu_ceiling,
                    "memoryMB": _HOG_MEMORY_MB,
                },
            )
            at_ceiling_id = (ok_body or {}).get("sandboxID")
            delete_status = None
            if ok_status == 201 and at_ceiling_id:
                delete_status = api_request(
                    args.api_url,
                    args.api_key,
                    method="DELETE",
                    path=f"/sandboxes/{at_ceiling_id}",
                )[0]
            expected_cpu = (
                f"cpuCount {cpu_ceiling * 4} exceeds this node's per-sandbox "
                f"maximum ({cpu_ceiling})"
            )
            expected_mem = (
                f"memoryMB {memory_ceiling * 4 + 1} exceeds this node's per-sandbox "
                f"maximum ({memory_ceiling})"
            )
            record(
                "8_oversize_named_400",
                cpu_status == 400
                and (cpu_body or {}).get("message") == expected_cpu
                and mem_status == 400
                and (mem_body or {}).get("message") == expected_mem
                and ok_status == 201
                # ... and the boundary box is really gone: a leak here would
                # spend a node's whole ceiling for the rest of the run.
                and delete_status == 204,
                criterion=(
                    "cpuCount/memoryMB past the node's per-sandbox ceiling answer "
                    "exactly 400 with the message that quotes that ceiling, and a "
                    "request *at* the ceiling is accepted (201); the ceiling is the "
                    "control plane's own policy, handed down to the worker (R17), so "
                    "the number quoted is the one the control-plane container "
                    "declares; and the boundary box's DELETE answers 204"
                ),
                ceiling_source=(
                    "E2B_MAX_SANDBOX_* read inside the control plane "
                    f"({args.control_plane_node}); the refusal quotes the number "
                    f"the create was checked against on {ceiling_node}"
                ),
                cpu_ceiling=cpu_ceiling,
                memory_ceiling=memory_ceiling,
                cpu_status=cpu_status,
                cpu_message=(cpu_body or {}).get("message"),
                cpu_expected=expected_cpu,
                memory_status=mem_status,
                memory_message=(mem_body or {}).get("message"),
                memory_expected=expected_mem,
                at_ceiling_status=ok_status,
                at_ceiling_delete_status=delete_status,
                at_ceiling_raw=ok_raw[:200],
            )
        except Exception as exc:  # noqa: BLE001 - a failed reading is a reading
            record("8_oversize_named_400", False, reason=f"{type(exc).__name__}: {exc}")
        finally:
            kill_created_since(marker)

        # ---- check 9: peak memory and the task unit ------------------------
        marker = len(boxes)
        try:
            box, declared = create_sized(
                {"n83_acceptance": "peak"}, cpu_count=1, memory_mb=_PEAK_MEMORY_MB
            )
            box.commands.run("true")
            node, cgroup = locate(box.sandbox_id)
            if cgroup is None:
                raise Refusal(
                    "no sbx_<id> cgroup under the hosting worker's own delegated "
                    "cgroup (E2B_SANDBOX_CGROUP off?), so there is no memory.peak "
                    "and no pids.current to read"
                )
            note_box(box, node, cgroup)
            declared_memory_bytes = int(declared["memoryMB"]) * 1024 * 1024
            declared_tasks, declared_record = declared_processes(node, box.sandbox_id)
            limits = read_limits(shell, node, cgroup)
            holder = box.commands.run(
                "cat > alloc.py <<'PYEOF'\n"
                + _ALLOC_HOLD_SCRIPT
                + "PYEOF\npython3 alloc.py %d 12" % _PEAK_ALLOC_BYTES,
                background=True,
                timeout=120,
            )
            peak = 0
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                time.sleep(0.5)
                sample = read_limits(shell, node, cgroup)
                if sample["memory_peak"].isdigit():
                    peak = max(peak, int(sample["memory_peak"]))
                if peak >= _PEAK_ALLOC_BYTES:
                    break
            holder.wait()
            baseline = hold_reading(box, node, cgroup, threads=0, procs=0)
            two_threads = hold_reading(box, node, cgroup, threads=2, procs=0)
            one_process = hold_reading(box, node, cgroup, threads=0, procs=1)
            thread_delta = (
                two_threads["steady_pids_current"] - baseline["steady_pids_current"]
            )
            process_delta = (
                one_process["steady_pids_current"] - baseline["steady_pids_current"]
            )
            record(
                "9_peak_and_task_unit",
                limits["memory_max"] == str(declared_memory_bytes)
                and limits["memory_high"] == str(declared_memory_bytes)
                and limits["pids_max"] == str(declared_tasks)
                and peak >= _PEAK_ALLOC_BYTES
                and peak <= declared_memory_bytes
                and thread_delta == 2
                and process_delta == 1,
                criterion=(
                    "memory.max/memory.high == the declared memoryMB byte for byte; "
                    "pids.max == this box's own recorded declaration (the worker's "
                    "_runtime/<id>/sandbox.json max_processes; the lane's "
                    "E2B_DEFAULT_MAX_PROCESSES is printed alongside, never the "
                    "criterion); a held 64 MiB allocation moves memory.peak into "
                    "[64 MiB, memory.max]; and pids.current counts a thread exactly "
                    "like a process (2 threads add 2, 1 forked process adds 1, both "
                    "measured against the same holder with neither -- the unit Task "
                    "5's probe read as 3 for 2 threads + 1 process)"
                ),
                declared_memory_mb=declared["memoryMB"],
                declared_memory_bytes=declared_memory_bytes,
                declared_processes=declared_tasks,
                declared_processes_source=declared_record.get("path"),
                worker_record=declared_record.get("record"),
                lane_env_declared_processes=lane_declared_processes(node),
                memory_max=limits["memory_max"],
                memory_high=limits["memory_high"],
                pids_max=limits["pids_max"],
                peak_after_alloc=peak,
                peak_alloc_bytes=_PEAK_ALLOC_BYTES,
                pids_current_idle=limits["pids_current"],
                pids_current_holder_threads0_processes0=baseline,
                pids_current_holder_threads2_processes0=two_threads,
                pids_current_holder_threads0_processes1=one_process,
                thread_delta=thread_delta,
                process_delta=process_delta,
                sandbox_cgroup=cgroup,
                node=node,
            )
        except Exception as exc:  # noqa: BLE001 - a failed reading is a reading
            record("9_peak_and_task_unit", False, reason=f"{type(exc).__name__}: {exc}")
        finally:
            kill_created_since(marker)

    try:
        # N83 phase 2 / Task 7: checks 6-9 run first, each in its own try (see
        # run_phase2_checks): they are the checks that must still report a
        # named reading on a lane where the phase-1 lane is off, or where the
        # control plane does not hand a ceiling down (the pre-R17 window) and
        # the creates below refuse.
        run_phase2_checks()

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
                # "Does not degrade": min of five round trips within 3x of the
                # quiet baseline *on the same node*. The min is the signal --
                # each batch's first sample is a cold connect and would other-
                # wise dominate both numbers (and hide a real regression).
                and busy_rtt["min_ms"]
                <= max(
                    _NEIGHBOUR_RTT_FACTOR * quiet_rtt["min_ms"], _NEIGHBOUR_RTT_FLOOR_MS
                ),
                declared_cpu_percent=declared,
                measured_cpu_percent=measured,
                cpu_max_readback=quota_readback["cpu_max"] if quota_readback else None,
                sandbox_cgroup=first_cgroup,
                spinner_node=first_node,
                first_sandbox_rtt_quiet=quiet_rtt,
                second_sandbox_rtt=busy_rtt,
                round_trip_criterion=(
                    f"min-of-5 neighbour <= {_NEIGHBOUR_RTT_FACTOR:g}x the quiet min-of-5 "
                    f"(floor {_NEIGHBOUR_RTT_FLOOR_MS:g} ms) -- detects gross starvation "
                    "(the N82 shape stalled 860 ms); a subtle slowdown is below its "
                    "resolution and is caught by check 3's cgroup accounting instead"
                ),
                round_trip_bound_ms=round(
                    max(
                        _NEIGHBOUR_RTT_FACTOR * quiet_rtt["min_ms"], _NEIGHBOUR_RTT_FLOOR_MS
                    ),
                    2,
                ),
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
            # The probe's own DONE line is the flood's reading: `ops_per_s` is
            # what the clients achieved, `stalls`/`rounds` say whether the cap
            # was in effect (see `flood_is_capped`).
            ops_per_s = stalls = rounds = None
            for line in output.splitlines():
                if not line.startswith("DONE "):
                    continue
                fields = dict(
                    part.split("=", 1) for part in line.split() if "=" in part
                )
                if "ops_per_s" in fields:
                    ops_per_s = int(fields["ops_per_s"])
                if "stalls" in fields:
                    stalls = int(fields["stalls"])
                if "rounds" in fields:
                    rounds = int(fields["rounds"])
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
                "stalls": stalls,
                "rounds": rounds,
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
            # ... and the cap really was off while it ran: a *capped* flood is
            # not "a flood inside the quota", it is the cap doing the bounding,
            # and the cgroup would look innocent either way.
            and not flood_is_capped(alone["stalls"], alone["rounds"])
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
            peers = facts["peer_containers"]
            # A peer we were *not* given: its directory is still root-owned, so
            # every write (its cpu.max, its cgroup.procs, mkdir in it) must be
            # EACCES. This is the assertion the ruling names.
            foreign_peers = [peer for peer in peers if not peer["delegated_to_our_uid"]]
            # A peer whose directory *was* delegated -- i.e. another worker on
            # a lane where every worker runs as the same host uid. Its
            # `cpu.max` is still root-owned (the delegation excludes it), but
            # `cgroup.procs`/`mkdir` are ours by uid. Reported as a reading,
            # not hidden and not asserted away: uid-based DAC cannot separate
            # two containers of the same uid (see the module docstring and
            # `docs/deploy-clusters.md` §7.48).
            delegated_peers = [peer for peer in peers if peer["delegated_to_our_uid"]]
            facts["peer_containers_count"] = len(peers)
            facts["foreign_peers"] = [peer["path"] for peer in foreign_peers]
            facts["foreign_peers_closed"] = bool(foreign_peers) and all(
                peer["cpu_max"] != "WRITABLE"
                and peer["cgroup_procs"] != "WRITABLE"
                and peer["mkdir"] != "WRITABLE"
                for peer in foreign_peers
            )
            facts["every_peer_cpu_max_closed"] = all(
                peer["cpu_max"] != "WRITABLE" for peer in peers
            )
            facts["delegated_peers"] = [
                {
                    "path": peer["path"],
                    "cgroup_procs": peer["cgroup_procs"],
                    "subtree_control": peer["subtree_control"],
                    "mkdir": peer["mkdir"],
                    "cpu_max": peer["cpu_max"],
                }
                for peer in delegated_peers
            ]
            # Which shape of evidence this lane can produce: no peer visible at
            # all because the mount is narrowed to this worker's own parent
            # (both shipped lanes now -- k8s subPathExpr, compose static
            # cgroup_parent + matching bind), or peers visible and closed, which
            # is what a *non-narrowed* mount produces (the compose lanes before
            # 2026-10-06). A whole-tree mount that shows no peer is *not*
            # evidence.
            facts["peer_visible"] = bool(peers)
            facts["mount_looks_narrowed"] = not any(
                name == "docker"
                or name.startswith("kubepods")
                or name.endswith(".slice")
                for name in facts["ls_mount"]
            )
            facts["check4_mode"] = (
                "peer-container"
                if foreign_peers
                else ("delegated-peer-only" if delegated_peers else ("narrowed-mount" if facts["mount_looks_narrowed"] else "no-evidence"))
            )
            root_probe = facts["mount_root"]
            facts["mount_root_closed"] = (
                root_probe["cpu_max"] != "WRITABLE"
                and root_probe["cgroup_procs"] != "WRITABLE"
                and root_probe["mkdir"] != "WRITABLE"
            )
            facts["evidence"] = (
                facts["foreign_peers_closed"]
                if foreign_peers
                else (
                    # Another worker's delegated cgroup is visible (same host
                    # uid): its `cpu.max` is still the invariant -- the
                    # delegation never hands it over.
                    facts["every_peer_cpu_max_closed"]
                    if delegated_peers
                    # Nothing peer-shaped is visible because the mount is this
                    # pod only: the root is then the authority, and it is not
                    # ours to write.
                    else facts["mount_looks_narrowed"] and facts["mount_root_closed"]
                )
            )
            facts["cpu_max_closed"] = facts["own"]["cpu_max"] != "WRITABLE"
            view_facts[node] = facts
        record(
            "4_narrowing_view_shape",
            bool(view_facts)
            and all(
                facts.get("own_delegated")
                and facts.get("every_peer_cpu_max_closed")
                and facts.get("evidence")
                for facts in view_facts.values()
            ),
            criterion=(
                "own delegated cgroup writable (cgroup.procs/subtree_control, NOT cpu.max); "
                "every visible peer container's cpu.max still EACCES; every non-delegated peer's "
                "cpu.max/cgroup.procs/mkdir EACCES"
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
    if len(report["checks"]) != len(_EXPECTED_CHECKS):
        report["ok"] = False
        report.setdefault("missing_checks", [])
        report["missing_checks"] = [
            name for name in _EXPECTED_CHECKS if name not in report["checks"]
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
