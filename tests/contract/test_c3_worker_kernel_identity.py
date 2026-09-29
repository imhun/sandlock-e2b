"""C3 Task 4 / D21 option 2 against a **real kernel** (container lane).

The unit lane drives the resolver against a synthetic ``/proc``. This lane
drives the shipped shape with real containers, because the property under test
is the *kernel's*: a process may only read another process's
``/proc/<pid>/ns/pid`` when the two identities match (``ptrace_may_access``), so
"face B reads the worker's uid/gid out of the kernel" is only true if the reader
runs as the workers' identity -- and only a real kernel can say so.

```
worker container (uid 65534, its own pid namespace)  ─┐
                                                      ├─ the resolver child runs
reader container (root, --pid=host, docker's default  ┘   as 65534, reads /proc
                 capabilities: **no** CAP_SYS_PTRACE)
```

What it covers: the production :class:`SubprocessWorkerIdentityResolver` -- the
child's ``user=``/``group=``, the argv, the one-line JSON protocol -- resolving a
real worker's anchor on a real host ``/proc``, and each named refusal the brief
asks for (a claim the kernel does not confirm, an anchor that names nothing, an
anchor that names more than one process).

What it does **not** cover: the control plane's hop (no HTTP here), the compose
stack files themselves, and the ``pid: host`` of face B (the reader's ``--pid
=host`` stands in for it). ``tests/unit/test_c3_worker_kernel_identity.py``
pins the instruction-level behaviour and
``tests/unit/test_c3_fileops_forwarding.py`` the control-plane half.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "python:3.14-slim"
WORKER = "e2b-c3-kernel-identity-worker"
#: The identity the shipped worker image runs as (``USER 65534:65534``), and the
#: identity the resolver child is told to run as (``E2B_C3_AGENT_RESOLVER_UID``'s
#: default).
WORKER_UID = 65534
WORKER_GID = 65534
#: The pooled uid a sandbox would get: what a forged claim names.
POOL_UID = 10007
#: A shape-valid pid namespace no process can be in.
NOBODY_NAMESPACE = "pid:[999999999]"

#: The reader's program: the *production* resolver for one named case, printed
#: as JSON so the host lane asserts exact values. ``case`` is the case name and
#: ``anchor`` is the worker's real pid namespace.
READER_SCRIPT = r'''
import json, sys

from deploy.c3_agent.lookup import (
    LookupRefusal,
    SubprocessWorkerIdentityResolver,
    WorkerIdentity,
)

case, anchor = sys.argv[1], sys.argv[2]
# The shipped configuration: the child runs as the workers' own identity, which
# is what makes the kernel's ptrace rule let it read the anchor's process.
resolver = SubprocessWorkerIdentityResolver(uid=65534, gid=65534, timeout_s=30.0)
cases = {
    "kernel": (anchor, (65534, 65534)),
    "disagreeing_uid": (anchor, (10007, 10007)),
    "disagreeing_group": (anchor, (65534, 10007)),
    "no_process": ("pid:[999999999]", (65534, 65534)),
    "ambiguous": (anchor, (65534, 65534)),
}
namespace, claimed = cases[case]
identity = WorkerIdentity(node_id="worker-1", pid_namespace=namespace)
try:
    print(json.dumps({"identity": list(resolver.resolve(identity, claimed=claimed))}))
except LookupRefusal as exc:
    print(json.dumps({"refused": str(exc)}))
'''


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), capture_output=True, text=True, check=False, timeout=300
    )


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = _run("docker", "info")
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ready(),
    reason="needs a Docker daemon (this lane starts a worker container and a "
    "host-pid reader)",
)


def _host_repo_path() -> str:
    """Where this repository lives *on the Docker host* (see the Task 3 lane)."""
    inspected = _run(
        "docker",
        "inspect",
        "--format",
        "{{range .Mounts}}{{.Source}}->{{.Destination}}\n{{end}}",
        os.uname().nodename,
    )
    if inspected.returncode == 0:
        for line in inspected.stdout.splitlines():
            source, _, destination = line.partition("->")
            if destination.strip() == str(PROJECT_ROOT):
                return source.strip()
    return str(PROJECT_ROOT)


@pytest.fixture()
def worker():
    """One worker container: a single process (``sleep``) as uid 65534."""
    started = _run(
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        WORKER,
        "--user",
        f"{WORKER_UID}:{WORKER_GID}",
        IMAGE,
        "sleep",
        "600",
    )
    if started.returncode != 0:  # pragma: no cover - a broken daemon
        pytest.fail(f"could not start the worker container: {started.stderr}")
    try:
        anchor = _run("docker", "exec", WORKER, "readlink", "/proc/1/ns/pid")
        if anchor.returncode != 0:  # pragma: no cover
            pytest.fail(f"could not read the worker's namespace: {anchor.stderr}")
        yield anchor.stdout.strip()
    finally:
        _run("docker", "rm", "-f", WORKER)


def _reader(*, anchor: str, case: str, second_process: bool = False) -> dict:
    """Run the production resolver inside a host-pid reader container."""
    if second_process:
        added = _run("docker", "exec", "-d", WORKER, "sleep", "300")
        if added.returncode != 0:  # pragma: no cover
            pytest.fail(f"could not add a second worker process: {added.stderr}")
    result = _run(
        "docker",
        "run",
        "--rm",
        "--pid=host",
        "-v",
        f"{_host_repo_path()}:/w",
        "-w",
        "/w",
        "-e",
        "PYTHONPATH=/w",
        IMAGE,
        "python3",
        "-c",
        READER_SCRIPT,
        case,
        anchor,
    )
    if result.returncode != 0:  # pragma: no cover - a broken image/mount
        pytest.fail(f"the reader container failed: {result.stderr}")
    return json.loads(result.stdout)


def test_the_readers_own_capabilities_cannot_read_the_worker(worker: str) -> None:
    """The premise: docker's default set has no ``CAP_SYS_PTRACE``.

    If this ever stops being true the lane below would pass for the wrong
    reason, so it is asserted rather than assumed.
    """
    caps = _run(
        "docker",
        "run",
        "--rm",
        "--pid=host",
        IMAGE,
        "sh",
        "-c",
        "grep CapEff /proc/self/status",
    )
    assert caps.returncode == 0
    value = int(caps.stdout.split()[1], 16)
    assert value & (1 << 19) == 0  # CAP_SYS_PTRACE


def test_the_readers_own_process_cannot_see_the_anchor(worker: str) -> None:
    """And the concrete consequence: root without the capability reads nothing.

    Only the child that drops to the workers' identity can, which is what makes
    ``E2B_C3_AGENT_RESOLVER_UID`` load-bearing rather than decorative.
    """
    out = _run(
        "docker",
        "run",
        "--rm",
        "--pid=host",
        IMAGE,
        "python3",
        "-c",
        (
            "import os, sys\n"
            "anchor = sys.argv[1]\n"
            "found = []\n"
            "for name in os.listdir('/proc'):\n"
            "    if not name.isdigit():\n"
            "        continue\n"
            "    try:\n"
            "        link = os.readlink(f'/proc/{name}/ns/pid')\n"
            "    except OSError:\n"
            "        continue\n"
            "    if link == anchor:\n"
            "        found.append(int(name))\n"
            "print(found)\n"
        ),
        worker,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "[]"


def test_the_kernel_answer_is_the_workers_identity(worker: str) -> None:
    assert _reader(anchor=worker, case="kernel") == {
        "identity": [WORKER_UID, WORKER_GID]
    }


def test_a_claim_of_another_tenants_uid_is_refused_by_name(worker: str) -> None:
    """The §14.3 hole itself: a worker naming a *pool* uid.

    The resolver child runs as the workers' identity, so it can see the anchor's
    process and the kernel's answer is read for real -- which is what makes this
    a *disagreement* and not merely "nothing found".
    """
    assert _reader(anchor=worker, case="disagreeing_uid") == {
        "refused": (
            "worker worker-1 claims uid/gid (10007, 10007), but the kernel says "
            f"(65534, 65534) for {worker}: refusing (a worker does not name the "
            "identity its privileged steps act as)"
        )
    }


def test_a_claim_of_the_wrong_group_is_refused_by_name(worker: str) -> None:
    """The group is half the identity: the group a sandbox tree is handed to."""
    assert _reader(anchor=worker, case="disagreeing_group") == {
        "refused": (
            "worker worker-1 claims uid/gid (65534, 10007), but the kernel says "
            f"(65534, 65534) for {worker}: refusing (a worker does not name the "
            "identity its privileged steps act as)"
        )
    }


def test_an_anchor_that_names_nothing_is_refused_by_name(worker: str) -> None:
    assert _reader(anchor=worker, case="no_process") == {
        "refused": (
            "worker worker-1's pid namespace (pid:[999999999]) holds no process "
            "this identity resolver can see (it runs as 65534:65534): refusing "
            "to derive its own uid/gid"
        )
    }


def test_an_anchor_that_names_two_processes_is_refused_by_name(worker: str) -> None:
    assert _reader(anchor=worker, case="ambiguous", second_process=True) == {
        "refused": (
            f"worker worker-1's pid namespace ({worker}) holds more than one "
            "process: refusing (ambiguous)"
        )
    }
