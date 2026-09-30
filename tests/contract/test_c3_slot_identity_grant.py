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
#: The lane's own scratch, inside the repository's ``tmp/`` (the rule for every
#: lane here). The driver container writes the program the child execs into it.
LANE_TMP = PROJECT_ROOT / "tmp" / "c3-slot-child"
#: The worker-side driver: the *production* child path, run where the worker
#: runs (65534, no capabilities, repository mounted).
CHILD_DRIVER = "/w/tests/contract/c3_slot_child_driver.py"
CHILD_DRIVER_MOUNT = "/w"


def _host_repo_path() -> str:
    """Where this repository lives *on the Docker host*.

    The worker containers are started by the daemon, so a bind mount has to name
    a path the **host** can resolve -- while this test may itself be running
    inside a container (the lane's normal place), where the repository is mounted
    somewhere else. The mount table of the container we are in is the honest
    source, and a run directly on the host falls back to our own path.
    """
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
from c3_agent.lookup import LookupRefusal, ProcLookup, WorkerIdentity
container_pid, pid_namespace, node_id, sandbox_id = sys.argv[1:5]
try:
    slot = ProcLookup().host_pid(
        int(container_pid),
        WorkerIdentity(node_id=node_id, pid_namespace=pid_namespace),
        sandbox_id=sandbox_id,
    )
except LookupRefusal as exc:
    print(f"C3-REFUSED {exc}", flush=True)
else:
    print(
        f"C3-RESOLVED host={slot.host_pid} start={slot.start_time}",
        flush=True,
    )
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
    # The 2026-09-30 move: the package is top-level `c3_agent/` and its C source
    # is `c3_agent/priv/`, so one copytree carries both -- exactly the two
    # things the Dockerfile COPYs.
    shutil.copytree(PROJECT_ROOT / "c3_agent", context / "c3_agent")
    shutil.copytree(PROJECT_ROOT / "gateway_common", context / "gateway_common")
    built = _run("docker", "build", "-t", AGENT_IMAGE, str(context))
    assert built.returncode == 0, f"{built.stdout}\n{built.stderr}"
    return AGENT_IMAGE


def _logs(container: str) -> str:
    logs = _run("docker", "logs", container)
    # ``docker logs`` prefixes each stream's chunks with a framing byte; strip
    # the control range so line-anchored patterns see the real lines.
    return re.sub(r"[\x00-\x08\x0e-\x1f]", "", logs.stdout + logs.stderr)


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

    def start_child_driver(
        self, name: str, *, mode: str, delay: float = 0.0
    ) -> tuple[str, float]:
        """Start a worker container running the production child path.

        Returns the container name and the moment it was started, so a lane can
        bound *when* the worker reported (``--mode delayed`` is only reported
        once the child's handshake byte has arrived).
        """
        LANE_TMP.mkdir(parents=True, exist_ok=True)
        LANE_TMP.chmod(0o777)
        container = f"c3-si-driver-{name}-{self.suffix}"
        started_at = time.monotonic()
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
            "--security-opt",
            "seccomp=unconfined",
            "-v",
            f"{_host_repo_path()}:{CHILD_DRIVER_MOUNT}",
            "-w",
            CHILD_DRIVER_MOUNT,
            "-e",
            "PYTHONPATH=/w",
            "--entrypoint",
            "python3",
            WORKER_IMAGE,
            CHILD_DRIVER,
            "--uid",
            str(GRANT_UID),
            "--mode",
            mode,
            "--delay",
            str(delay),
            "--tmp-dir",
            "/w/tmp/c3-slot-child",
        )
        assert started.returncode == 0, started.stderr
        self.drivers = getattr(self, "drivers", [])
        self.drivers.append(container)
        return container, started_at

    def stop(self) -> None:
        names = [*self.workers.values(), self.agent, *getattr(self, "drivers", [])]
        _run("docker", "rm", "-f", *names)

    def pid_namespace_of(self, container: str) -> str:
        """The container's pid namespace, as any process inside it reads it."""
        result = _run("docker", "exec", container, "readlink", "/proc/self/ns/pid")
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

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


def _cgroup_owner(cgroup_text: str) -> str:
    """The container id a host-side cgroup path ends in.

    Docker's cgroup drivers spell the leaf three ways -- ``docker-<id>.scope``
    (systemd), ``<id>`` (cgroupfs) and the same under a runtime prefix -- so the
    wrapper is stripped before the comparison, never the id itself.
    """
    leaf = cgroup_text.strip().rsplit("/", 1)[-1]
    for prefix in ("docker-", "crio-", "cri-containerd-"):
        if leaf.startswith(prefix):
            leaf = leaf[len(prefix) :]
            break
    if leaf.endswith(".scope"):
        leaf = leaf[: -len(".scope")]
    return leaf


def _resolved_pid(resolved: str) -> int:
    """The host pid out of the driver's answer, shape checked exactly.

    The start time rides along because the lookup is required to record it (a
    pid alone cannot be re-found after the fact); asserting the shape here is
    what keeps the real ``/proc/<pid>/stat`` parse honest on a real kernel.
    """
    # ``start`` is the kernel's birth tick: a parse that silently read the wrong
    # field would give 0, not a positive number.
    match = re.fullmatch(r"C3-RESOLVED host=(\d+) start=([1-9]\d*)", resolved)
    assert match is not None, resolved
    return int(match.group(1))


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


@pytest.fixture()
def agent_only(agent_image: str) -> _Topology:
    """Just the agent (hostPID), for the lanes that bring their own worker."""
    topo = _Topology(agent_image)
    try:
        topo.start_agent()
        yield topo
    finally:
        topo.stop()


def _wait_for_ready(container: str, timeout: float = 60.0) -> tuple[int, str, float]:
    """``(container_pid, mode, seconds from the call until the report)``.

    The elapsed time is the observable the ordering lane needs: the driver
    prints this line *after* its handshake, so it cannot precede the child's
    ``unshare`` -- but a worker that reported on the spawn alone would print it
    immediately.
    """
    started = time.monotonic()
    pattern = r"^C3-DRIVER-READY container_pid=(\d+) mode=([\w-]+)$"
    deadline = started + timeout
    while time.monotonic() < deadline:
        match = re.search(pattern, _logs(container), re.MULTILINE)
        if match is not None:
            return int(match.group(1)), match.group(2), time.monotonic() - started
        time.sleep(0.05)
    raise AssertionError(
        f"{container}: no {pattern!r} within {timeout}s:\n{_logs(container)}"
    )


@pytest.mark.parametrize("iteration", [1, 2, 3, 4, 5])
def test_the_production_child_path_grants_then_execs(
    agent_only: _Topology, iteration: int
) -> None:
    """D11.2: the *worker's* child module, not a stand-in, end to end.

    ``_spawn_slot_identity`` builds ``child_argv`` for
    ``python -m envd_service.slot_identity``, hands it the handshake descriptor
    and returns only after the child's ``unshare``. From there the real grant
    runs (face A's ``as_uid`` against the real ``/proc``) and the child -- which
    has been polling ``setresuid`` -- execs the program it was told to run. That
    covers argv parsing, the handshake, the poll loop and the descriptor
    surviving the interpreter start, and it is repeated because a race is not
    disproved by one pass.
    """
    name = f"production-{iteration}"
    container, _started = agent_only.start_child_driver(name, mode="production")
    container_pid, mode, _elapsed = _wait_for_ready(container)
    assert mode == "production"
    identity = agent_only.pid_namespace_of(container)

    resolved = agent_only.lookup(container_pid, identity, name)
    host_pid = _resolved_pid(resolved)

    granted = agent_only.grant(host_pid)
    assert granted.returncode == 0, granted.stderr
    assert granted.stdout == f"C3-ASUID-OK pid={host_pid} uid={GRANT_UID}\n"
    assert granted.stderr == ""

    # The child got past setresuid and exec'd -- with the identity the agent
    # wrote, and still inside the worker's own pid namespace.
    assert _wait_for(
        container, r"^(C3-SLOT-EXEC-OK uid=\d+ pidns=pid:\[\d+\])$"
    ) == f"C3-SLOT-EXEC-OK uid={GRANT_UID} pidns={identity}"
    stat = agent_only.exec_in_agent("stat", "-c", "%u", f"/proc/{host_pid}")
    assert stat.stdout.strip() == str(GRANT_UID)


def test_the_report_waits_for_a_slow_childs_unshare(agent_only: _Topology) -> None:
    """D11, in the lane: the worker is *late* by exactly the child's delay.

    The child here is the production module behind a wrapper that sleeps one
    second before exec'ing it, and the descriptor rides through both execs. The
    worker's report is printed only after the handshake byte, so it cannot appear
    before the child has unshared -- while a worker that reported on the spawn
    alone would print immediately (and then grant a namespace that does not
    exist yet).
    """
    delay = 1.0
    container, _started = agent_only.start_child_driver(
        "slow", mode="delayed", delay=delay
    )
    container_pid, mode, elapsed = _wait_for_ready(container)
    assert mode == "delayed"
    assert elapsed >= delay

    identity = agent_only.pid_namespace_of(container)
    resolved = agent_only.lookup(container_pid, identity, "slow")
    host_pid = _resolved_pid(resolved)
    granted = agent_only.grant(host_pid)
    assert granted.returncode == 0, granted.stderr
    assert _wait_for(
        container, r"^(C3-SLOT-EXEC-OK uid=\d+ pidns=pid:\[\d+\])$"
    ) == f"C3-SLOT-EXEC-OK uid={GRANT_UID} pidns={identity}"


def test_a_grant_that_precedes_the_unshare_is_refused_by_name(
    agent_only: _Topology,
) -> None:
    """The counter-arm: the old ordering is a *refusal*, not a slow success.

    ``--mode no-wait`` is the pre-D11 worker: it reports the pid straight off
    ``Popen``, with the child still a second away from its ``unshare``. Face A
    then reads the initial namespace's full-range map and refuses by name --
    which is exactly the intermittent create failure the handshake removes, and
    the reason the ordering is a contract and not a tuning detail.
    """
    container, _started = agent_only.start_child_driver(
        "nowait", mode="no-wait", delay=1.0
    )
    container_pid, mode, _elapsed = _wait_for_ready(container)
    assert mode == "no-wait"
    identity = agent_only.pid_namespace_of(container)
    resolved = agent_only.lookup(container_pid, identity, "nowait")
    host_pid = _resolved_pid(resolved)

    granted = agent_only.grant(host_pid)
    assert granted.returncode == 77
    assert granted.stdout == ""
    assert granted.stderr == (
        f"as_uid: refused: uid_map for pid {host_pid} is the initial namespace's "
        "full range: this pid has not unshared a user namespace, so there is "
        "no new identity to grant\n"
    )


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
    host_1 = _resolved_pid(resolved_1)
    host_2 = _resolved_pid(resolved_2)
    assert host_1 != host_2

    # Ownership, independently of the lookup: the host-side cgroup of each
    # resolved process *is* its own worker's cgroup, byte for byte -- and the two
    # workers' cgroups are different strings, because they are different
    # containers. That is the same shape the k8s lane matches on with
    # ``pod<uid>`` (§14.2.7), asserted exactly rather than "contains".
    cgroup_1 = topology.proc_text(host_1, "cgroup")
    cgroup_2 = topology.proc_text(host_2, "cgroup")
    worker_1_pid = _resolved_pid(
        topology.lookup(1, topology.identities["worker-1"], "worker-1")
    )
    worker_2_pid = _resolved_pid(
        topology.lookup(1, topology.identities["worker-2"], "worker-2")
    )
    assert cgroup_1 == topology.proc_text(worker_1_pid, "cgroup")
    assert cgroup_2 == topology.proc_text(worker_2_pid, "cgroup")
    assert cgroup_1 != cgroup_2
    # ... and the cgroup's own last path element is that worker's container id
    # (``docker-<id>.scope`` on a systemd host, the bare id on a cgroupfs one).
    assert _cgroup_owner(cgroup_1) == _container_id(topology.workers["worker-1"])
    assert _cgroup_owner(cgroup_2) == _container_id(topology.workers["worker-2"])


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
    host_pid = _resolved_pid(resolved)
    # The worker's *own* process, resolved the same way (it is pid 1 inside its
    # container): the comparison in ② is against the worker's cgroup, so this
    # has to be a host pid too, never the container pid.
    worker_pid = _resolved_pid(
        topology.lookup(1, topology.identities["worker-1"], "worker-1")
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
    # ... and it is the same worker whose cgroup the slot is in (the identity the
    # control plane matched is the *worker's*, not "some process on the host").
    assert _cgroup_owner(topology.proc_text(worker_pid, "cgroup")) == _container_id(
        topology.workers["worker-1"]
    )
