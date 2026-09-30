"""C3 Task 4 / rulings D21 option 2 + D25 against a **real kernel** (container lane).

The unit lane drives the resolver against a synthetic ``/proc``. This lane
drives the shipped shape with real containers, because the property under test
is the *kernel's*: which of a worker's files the asking face may read.

```
worker container (uid 65534, hostname = its container id)  ─┐
                                                           ├─ face B, exactly as
reader container (root, --pid=host, CapEff == 0xb:          ┘  the manifest runs it
                 CHOWN + DAC_OVERRIDE + FOWNER,
                 **no** CAP_SYS_PTRACE, no SETUID)
```

What it covers, in the order the ruling argues it:

* **the premise** -- face B's capability set cannot ``readlink /proc/<pid>/ns/pid``
  of a worker's process (``ptrace_may_access``), which is why the old anchor had
  to be read by a child running as the workers' uid;
* **the same face reads the cgroup and the status file fine** -- they are
  world-readable, so the *container-id* anchor needs no capability, no uid
  change, and (D2's resolution) no ``SETUID``/``SETGID`` in face B at all;
* the production :class:`c3_agent.lookup.ProcWorkerIdentityResolver`
  resolving a real worker's anchor on a real host ``/proc``, plus each named
  refusal the brief asks for (a claim the kernel does not confirm, an anchor no
  process carries, candidates whose identities disagree), and the D4 relaxation
  (several processes in one container -- a running slot, a ``docker exec``, a
  zombie -- are not an ambiguity).

What it does **not** cover: the control plane's hop (no HTTP here) and the
compose stack files themselves; ``tests/unit/test_c3_worker_kernel_identity.py``
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
#: Face B's shipped capability set (judgment 4): CHOWN | DAC_OVERRIDE | FOWNER.
FACE_B_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER")
#: The identity the shipped worker image runs as (``USER 65534:65534``).
WORKER_UID = 65534
WORKER_GID = 65534
#: The pooled uid a sandbox would get: what a forged claim names.
POOL_UID = 10007
#: A shape-valid container id no container on this host carries.
NOBODY_ID = "0123456789ab"

#: The reader's program: the *production* resolver for one named case, printed
#: as JSON so the host lane asserts exact values. It runs as face B is shipped --
#: root, three capabilities, no ``SETUID``/``SETGID``, no ``CAP_SYS_PTRACE`` --
#: which is the whole point of D25.
READER_SCRIPT = r'''
import json, os, sys

from c3_agent.lookup import (
    LookupRefusal,
    ProcLookup,
    ProcWorkerIdentityResolver,
    WorkerAnchor,
)

case, anchor = sys.argv[1], sys.argv[2]
cases = {
    "kernel": (anchor, (65534, 65534)),
    "disagreeing_uid": (anchor, (10007, 10007)),
    "disagreeing_group": (anchor, (65534, 10007)),
    "no_process": ("0123456789ab", (65534, 65534)),
    "busy": (anchor, (65534, 65534)),
}
namespace, claimed = cases[case]
identity = WorkerAnchor(node_id="worker-1", container_id=namespace)
resolver = ProcWorkerIdentityResolver(ProcLookup())
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


def _face_b_flags(*extra: str) -> list[str]:
    """The flags that make ``docker run`` a face B: root, three capabilities."""
    flags = ["--pid=host", "--user", "0:0", "--cap-drop", "ALL"]
    for capability in FACE_B_CAPS:
        flags += ["--cap-add", capability]
    return flags + list(extra)


@pytest.fixture()
def worker():
    """One worker container, yielding **the anchor it would report** (D25).

    That is the value a real worker reads out of the kernel and reports: the
    container's hostname, which the runtime sets to the first 12 characters of
    the container id. The test does not slice an id itself -- it asks the
    container, exactly as ``envd_service.worker_identity.worker_container_id``
    does.
    """
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
        hostname = _run("docker", "exec", WORKER, "hostname")
        if hostname.returncode != 0:  # pragma: no cover
            pytest.fail(f"could not read the worker's hostname: {hostname.stderr}")
        assert hostname.stdout.strip(), "a container without a hostname"
        yield hostname.stdout.strip()
    finally:
        _run("docker", "rm", "-f", WORKER)


def _reader(
    *, anchor: str, case: str, extra_process: str | None = None
) -> dict:
    """Run the production resolver inside a container shaped exactly like face B."""
    if extra_process is not None:
        added = _run(
            "docker", "exec", "-d", "-u", extra_process, WORKER, "sleep", "300"
        )
        if added.returncode != 0:  # pragma: no cover
            pytest.fail(f"could not add a second worker process: {added.stderr}")
    result = _run(
        "docker",
        "run",
        "--rm",
        *_face_b_flags(
            "-v",
            f"{_host_repo_path()}:/w",
            "-w",
            "/w",
            "-e",
            "PYTHONPATH=/w",
        ),
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


def test_face_bs_capability_set_cannot_read_the_workers_namespaces(
    worker: str,
) -> None:
    """The premise of D25: the *old* anchor's file is out of reach for face B.

    ``readlink /proc/<pid>/ns/pid`` goes through ``ptrace_may_access``, which
    allows it only for a process of the same uid or with ``CAP_SYS_PTRACE``.
    Face B is root without that capability (it shares ``pid: host`` with the
    control plane), so the anchor it *can* use has to be a world-readable file.
    """
    out = _run(
        "docker",
        "run",
        "--rm",
        *_face_b_flags(),
        IMAGE,
        "python3",
        "-c",
        (
            "import errno, os, sys\n"
            "pid = sys.argv[1]\n"
            "print(open('/proc/self/status').read().split('CapEff:')[1].split()[0])\n"
            "try:\n"
            "    print('readlink', os.readlink(f'/proc/{pid}/ns/pid'))\n"
            "except OSError as exc:\n"
            "    print('readlink refused', errno.errorcode.get(exc.errno, exc.errno))\n"
        ),
        worker[0],
    )
    assert out.returncode == 0
    caps, readlink = out.stdout.splitlines()
    # Exactly judgment 4's set: CHOWN | DAC_OVERRIDE | FOWNER.
    assert int(caps, 16) == 0x0B
    assert readlink.startswith("readlink refused")


def test_face_b_reads_the_cgroup_and_the_status_file_anyway(worker: str) -> None:
    """And the files the D25 anchor uses are open to that same process."""
    out = _run(
        "docker",
        "run",
        "--rm",
        *_face_b_flags(),
        IMAGE,
        "python3",
        "-c",
        (
            "import sys\n"
            "anchor = sys.argv[1]\n"
            "for name in sorted((n for n in __import__('os').listdir('/proc')\n"
            "                    if n.isdigit()), key=int):\n"
            "    try:\n"
            "        cgroup = open(f'/proc/{name}/cgroup').read().strip()\n"
            "    except OSError:\n"
            "        continue\n"
            "    if anchor not in cgroup:\n"
            "        continue\n"
            "    status = open(f'/proc/{name}/status').read()\n"
            "    uid = [l for l in status.splitlines() if l.startswith('Uid:')][0]\n"
            "    print(cgroup)\n"
            "    print(uid)\n"
            "    break\n"
            "else:\n"
            "    print('NO CANDIDATE'); print('')\n"
        ),
        worker,
    )
    assert out.returncode == 0
    cgroup, uid = out.stdout.splitlines()
    assert worker in cgroup              # the reported anchor, verbatim
    assert uid.split("\t")[1] == str(WORKER_UID)


def test_the_kernel_answer_is_the_workers_identity(worker: str) -> None:
    assert _reader(anchor=worker, case="kernel") == {
        "identity": [WORKER_UID, WORKER_GID]
    }


def test_a_claim_of_another_tenants_uid_is_refused_by_name(worker: str) -> None:
    """The §14.3 hole itself: a worker naming a *pool* uid."""
    assert _reader(anchor=worker, case="disagreeing_uid") == {
        "refused": (
            "worker worker-1 claims uid/gid (10007, 10007), but the kernel says "
            f"(65534, 65534) for {worker}: refusing (a worker does not name "
            "the identity its privileged steps act as)"
        )
    }


def test_a_claim_of_the_wrong_group_is_refused_by_name(worker: str) -> None:
    """The group is half the identity: the group a sandbox tree is handed to."""
    assert _reader(anchor=worker, case="disagreeing_group") == {
        "refused": (
            "worker worker-1 claims uid/gid (65534, 10007), but the kernel says "
            f"(65534, 65534) for {worker}: refusing (a worker does not name "
            "the identity its privileged steps act as)"
        )
    }


def test_an_anchor_that_names_nothing_is_refused_by_name(worker: str) -> None:
    assert _reader(anchor=worker, case="no_process") == {
        "refused": (
            f"worker worker-1's container ({NOBODY_ID}) holds no process this "
            "agent can identify as the worker (the container's init): refusing "
            "to derive its own uid/gid from the kernel"
        )
    }


def test_a_second_process_in_the_container_is_not_an_ambiguity(worker: str) -> None:
    """D4's relaxation on a real kernel: a second process is normal, not a tie.

    This is the case the old "exactly one process in the namespace" rule
    refused, and it is the state every worker is in as soon as one slot is
    running (or an operator runs one ``docker exec``).
    """
    assert _reader(
        anchor=worker, case="busy", extra_process=f"{WORKER_UID}:{WORKER_GID}"
    ) == {"identity": [WORKER_UID, WORKER_GID]}


def test_a_sandbox_shaped_process_does_not_hijack_the_identity(worker: str) -> None:
    """The measured shape that makes the *init* filter necessary.

    A sandbox's ``sandlock-supervise`` runs as a **pooled uid** inside the
    worker's own container cgroup (measured on the compose multinode stack
    2026-09-29: ``uid=10000 NSpid=[host, 71]`` beside the worker's
    ``uid=65534 NSpid=[host, 1]``). So "processes whose cgroup carries the
    anchor" is a set with two identities in it, and the answer has to come from
    the container's init -- never from the majority, and never from a value the
    kernel did not report for the worker's own process.
    """
    assert _reader(
        anchor=worker, case="busy", extra_process=f"{POOL_UID}:{POOL_UID}"
    ) == {"identity": [WORKER_UID, WORKER_GID]}
