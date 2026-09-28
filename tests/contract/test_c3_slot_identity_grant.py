"""C3 Task 3: the identity hand-off end to end, on a real kernel (container lane).

The unit lane drives the reverse lookup against a synthetic ``/proc``. This lane
drives the *production shape* with real containers:

```
worker A (65534, no capabilities) ─┐
                                   ├─ each forks a child, each child unshares
worker B (65534, no capabilities) ─┘   its user namespace and polls setresuid
agent (--pid=host, 65534)             resolves container pid → host pid, writes
                                      the map with the file-capped as_uid
```

Both workers are started the same way, so **both children are container pid 2**
-- the ruling's own case ("two workers on one host, each with container pid 42
must not be confusable"), with the numbers coming from the kernel rather than
from the test's imagination. Each worker's identity resolves to *its own* child;
the two host pids differ; and each resolved process is provably the right
worker's (its host-side cgroup carries that worker's container id).

Then the four facts the plan's acceptance matrix asks for on this path:

① the slot process's **host** uid is X (the kernel's view, not the namespace's);
② its ``/proc/<pid>/cgroup`` is byte-for-byte the worker's;
③ the worker's ``CapEff`` is 0;
and the worker-side half of the hand-off: the child's own ``setresuid(X)``
succeeds, which is what lets it exec ``sandlock-supervise`` with no privilege of
its own.

Needs Docker, Linux and root (each ``docker run`` here is one of the two
containers the production topology has).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AGENT_IMAGE = "e2b-sandlock-agent:c3-task3-test"
WORKER_IMAGE = "python:3.14-slim"

#: The pool uid the grant hands out (E2B_UID_POOL_START/SIZE in the C side).
GRANT_UID = 10009

#: The worker's half of the hand-off, as ``W1SlotPool`` does it: fork, unshare,
#: report the pid *it* knows, then poll ``setresuid`` (the identity is written by
#: somebody else) and exec. The worker container itself holds no capability, so
#: nothing here can succeed by privilege.
WORKER_SCRIPT = '''
import os, sys, time
uid = int(sys.argv[1])
print(f"C3-WORKER pid={os.getpid()} pidns={os.readlink('/proc/self/ns/pid')}",
      flush=True)
child = os.fork()
if child == 0:
    os.unshare(0x10000000)          # CLONE_NEWUSER, unprivileged
    print(f"C3-CHILD container_pid={os.getpid()} "
          f"pidns={os.readlink('/proc/self/ns/pid')}", flush=True)
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        try:
            os.setresuid(uid, uid, uid)
        except OSError:
            time.sleep(0.1)
            continue
        print(f"C3-CHILD-SETRESUID-OK uid={os.geteuid()}", flush=True)
        time.sleep(300)
    print("C3-CHILD-TIMEOUT", flush=True)
    os._exit(1)
time.sleep(300)
'''

#: The agent's half: run the *production* lookup module inside the hostPID
#: container and print what it resolved (or refused, by name).
LOOKUP_DRIVER = '''
import sys
from deploy.c3_agent.lookup import LookupRefusal, ProcLookup, WorkerIdentity
container_pid, pid_namespace, node_id, sandbox_id = sys.argv[1:5]
try:
    host = ProcLookup().host_pid(
        int(container_pid),
        WorkerIdentity(node_id=node_id, pid_namespace=pid_namespace),
        sandbox_id=sandbox_id,
    )
except LookupRefusal as exc:
    print(f"C3-REFUSED {exc}", flush=True)
else:
    print(f"C3-RESOLVED host={host}", flush=True)
'''


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"], capture_output=True, text=True
        ).returncode
        == 0
    )


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0 or not _docker_ready(),
    reason=(
        "the identity hand-off needs a real Linux kernel (user namespaces), "
        "root to compare host-side identities, and a reachable Docker daemon "
        "for the worker/agent container pair"
    ),
)


def _run(*args: str, timeout: float | None = 600) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def agent_image(tmp_path_factory: pytest.TempPathFactory) -> str:
    """``deploy/docker/Dockerfile.agent``, built from its own minimal context."""
    context = tmp_path_factory.mktemp("c3-slot-identity-context")
    shutil.copy2(
        PROJECT_ROOT / "deploy" / "docker" / "Dockerfile.agent",
        context / "Dockerfile",
    )
    shutil.copytree(PROJECT_ROOT / "deploy" / "priv", context / "deploy" / "priv")
    shutil.copy2(
        PROJECT_ROOT / "deploy" / "__init__.py", context / "deploy" / "__init__.py"
    )
    shutil.copytree(
        PROJECT_ROOT / "deploy" / "c3_agent", context / "deploy" / "c3_agent"
    )
    shutil.copytree(PROJECT_ROOT / "gateway_common", context / "gateway_common")
    built = _run("docker", "build", "-t", AGENT_IMAGE, str(context))
    assert built.returncode == 0, f"{built.stdout}\n{built.stderr}"
    return AGENT_IMAGE


def _logs(container: str) -> str:
    logs = _run("docker", "logs", container)
    return logs.stdout + logs.stderr


def _wait_for(container: str, pattern: str, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = re.search(pattern, _logs(container), re.MULTILINE)
        if match is not None:
            return match.group(1)
        time.sleep(0.25)
    raise AssertionError(
        f"{container}: no {pattern!r} within {timeout}s:\n{_logs(container)}"
    )


class _Topology:
    """Two workers and one hostPID agent, exactly as the deployment has them."""

    def __init__(self, agent_image: str) -> None:
        self.suffix = uuid.uuid4().hex[:8]
        self.workers: dict[str, str] = {}
        self.identities: dict[str, str] = {}
        self.agent = f"c3-si-agent-{self.suffix}"
        self.agent_image = agent_image

    def start_worker(self, name: str) -> None:
        container = f"c3-si-{name}-{self.suffix}"
        started = _run(
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            # Unprivileged user namespaces are what the C3 path needs; the
            # production worker image declares the same allowance.
            "--security-opt",
            "seccomp=unconfined",
            "--entrypoint",
            "python3",
            WORKER_IMAGE,
            "-c",
            WORKER_SCRIPT,
            str(GRANT_UID),
        )
        assert started.returncode == 0, started.stderr
        self.workers[name] = container
        self.identities[name] = _wait_for(
            container, r"^C3-WORKER pid=\d+ pidns=(pid:\[\d+\])$"
        )

    def start_agent(self) -> None:
        started = _run(
            "docker",
            "run",
            "-d",
            "--name",
            self.agent,
            # The DaemonSet's own shape: face A is the only ``hostPID`` in the
            # system, and the two caps are the file-capability bounding set both
            # binaries must fit inside.
            "--pid",
            "host",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--entrypoint",
            "sleep",
            self.agent_image,
            "infinity",
        )
        assert started.returncode == 0, started.stderr

    def stop(self) -> None:
        names = [*self.workers.values(), self.agent]
        _run("docker", "rm", "-f", *names)

    def finish_child(self, name: str) -> str:
        return _wait_for(
            self.workers[name], r"^C3-CHILD container_pid=(\d+) "
        )

    def child_container_pid(self, name: str) -> int:
        """The pid the worker's own child is known by *inside* that worker.

        This is exactly what the worker reports to the control plane (ruling
        D9.1): a number that is only meaningful together with the pid namespace
        it lives in, which is why it can be the same number in two workers.
        """
        return int(self.finish_child(name))

    def exec_in_agent(self, *args: str) -> subprocess.CompletedProcess:
        return _run("docker", "exec", self.agent, *args)

    def lookup(self, container_pid: int, identity: str, name: str = "worker-1") -> str:
        """Run the production lookup inside the hostPID container."""
        result = self.exec_in_agent(
            "python3",
            "-c",
            LOOKUP_DRIVER,
            str(container_pid),
            identity,
            name,
            f"sbx_{name}",
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def grant(self, host_pid: int) -> subprocess.CompletedProcess:
        return self.exec_in_agent(
            "/var/lib/e2b-priv/as_uid",
            "--uid",
            str(GRANT_UID),
            "--pid",
            str(host_pid),
        )

    def proc_text(self, host_pid: int, field: str) -> str:
        result = self.exec_in_agent("cat", f"/proc/{host_pid}/{field}")
        assert result.returncode == 0, result.stderr
        return result.stdout


def _container_id(container: str) -> str:
    inspected = _run("docker", "inspect", "--format", "{{.Id}}", container)
    assert inspected.returncode == 0, inspected.stderr
    return inspected.stdout.strip()


@pytest.fixture()
def topology(agent_image: str) -> _Topology:
    topo = _Topology(agent_image)
    try:
        topo.start_worker("worker-1")
        topo.start_worker("worker-2")
        topo.start_agent()
        yield topo
    finally:
        topo.stop()


def test_two_workers_with_the_same_container_pid_are_not_confusable(
    topology: _Topology,
) -> None:
    """The ruling's case, with the kernel supplying the numbers.

    Both workers are started identically and each forks exactly one child, so
    both children carry the *same* container pid in their own namespaces.
    ``NSpid`` alone cannot tell them apart -- both chains end in that number --
    so the proof is the worker's pid namespace: the resolved host pids must
    differ, and each must belong to the worker that asked.
    """
    first = topology.child_container_pid("worker-1")
    second = topology.child_container_pid("worker-2")
    assert first == second, (
        "the two workers must report the same container pid for this case to "
        f"mean anything (got {first} and {second})"
    )
    assert topology.identities["worker-1"] != topology.identities["worker-2"]

    resolved_1 = topology.lookup(first, topology.identities["worker-1"], "worker-1")
    resolved_2 = topology.lookup(second, topology.identities["worker-2"], "worker-2")
    assert re.fullmatch(r"C3-RESOLVED host=\d+", resolved_1), resolved_1
    assert re.fullmatch(r"C3-RESOLVED host=\d+", resolved_2), resolved_2
    host_1 = int(resolved_1.split("=")[1])
    host_2 = int(resolved_2.split("=")[1])
    assert host_1 != host_2

    # Ownership, independently of the lookup: the host-side cgroup of each
    # resolved process carries that worker's own container id (the same shape
    # the k8s lane matches on with ``pod<uid>``, §14.2.7).
    cgroup_1 = topology.proc_text(host_1, "cgroup")
    cgroup_2 = topology.proc_text(host_2, "cgroup")
    assert _container_id(topology.workers["worker-1"]) in cgroup_1
    assert _container_id(topology.workers["worker-2"]) in cgroup_2
    assert _container_id(topology.workers["worker-2"]) not in cgroup_1
    assert _container_id(topology.workers["worker-1"]) not in cgroup_2


def test_a_worker_identity_that_matches_nobody_is_refused_by_name(
    topology: _Topology,
) -> None:
    """A lane without a usable identity cannot fall back to the pid alone."""
    container_pid = topology.child_container_pid("worker-1")
    refused = topology.lookup(container_pid, "pid:[4026532999]", "worker-9")
    assert refused == (
        f"C3-REFUSED container pid {container_pid} is not in worker worker-9's "
        "pid namespace (pid:[4026532999]): refusing"
    )


def test_a_container_pid_that_has_ended_is_named(
    topology: _Topology,
) -> None:
    """D9.5's own case, against a real ``/proc``."""
    topology.finish_child("worker-1")
    refused = topology.lookup(999999, topology.identities["worker-1"], "worker-1")
    assert refused == "C3-REFUSED 沙箱 sbx_worker-1 的槽位 pid 已不在"


def test_the_grant_lands_on_the_host_identity_and_keeps_the_workers_cgroup(
    topology: _Topology,
) -> None:
    """① ② ③, end to end: host uid, cgroup, and a worker with no capabilities.

    The decisive evidence is the *outside* view again: the kernel reports host
    uid X for the slot's process, its cgroup is byte-for-byte the worker's (so
    the process tree never left the worker -- hard rule 1), and the worker's own
    ``CapEff`` is zero while all of this happens.
    """
    container_pid = topology.child_container_pid("worker-1")
    resolved = topology.lookup(
        container_pid, topology.identities["worker-1"], "worker-1"
    )
    host_pid = int(resolved.split("=")[1])
    # The worker's *own* process, resolved the same way (it is pid 1 inside its
    # container): the comparison in ② is against the worker's cgroup, so this
    # has to be a host pid too, never the container pid.
    worker_pid = int(
        topology.lookup(1, topology.identities["worker-1"], "worker-1").split("=")[1]
    )

    granted = topology.grant(host_pid)
    assert granted.returncode == 0, granted.stderr
    assert granted.stdout == f"C3-ASUID-OK pid={host_pid} uid={GRANT_UID}\n"
    assert granted.stderr == ""

    # The child's own half: it polls setresuid and reports the identity it got.
    assert _wait_for(
        topology.workers["worker-1"], r"^(C3-CHILD-SETRESUID-OK uid=\d+)$"
    ) == f"C3-CHILD-SETRESUID-OK uid={GRANT_UID}"

    # ① the host's view of the slot's identity (not the namespace's).
    stat = topology.exec_in_agent("stat", "-c", "%u", f"/proc/{host_pid}")
    assert stat.returncode == 0, stat.stderr
    assert stat.stdout.strip() == str(GRANT_UID)

    # ② the slot stayed in the worker's cgroup, byte for byte.
    assert topology.proc_text(host_pid, "cgroup") == topology.proc_text(
        worker_pid, "cgroup"
    )

    # ③ the worker that forked it holds nothing.
    status = topology.proc_text(worker_pid, "status")
    caps = [
        line.split(":", 1)[1].strip()
        for line in status.splitlines()
        if line.startswith("CapEff:")
    ]
    assert caps == ["0000000000000000"]
    # ... and it is the same worker whose cgroup the slot is in (the identity
    # the control plane matched is the *worker's*, not "some process on the
    # host").
    assert _container_id(topology.workers["worker-1"]) in topology.proc_text(
        worker_pid, "cgroup"
    )
