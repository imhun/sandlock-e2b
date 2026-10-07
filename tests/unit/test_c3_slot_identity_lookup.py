"""C3 Task 3 (controller ruling D9.3): container pid → host pid, unambiguously.

The agent is the only component that can see both sides: the worker reports the
pid **it** knows (``os.getpid()`` inside its own pid namespace) and the agent,
running ``hostPID``, has to turn it into the host pid it will hand to
``as_uid``. ``NSpid`` is the rendezvous, but on its own it is **ambiguous** --
``deploy/compose/docker-compose.multinode.yml`` runs three workers on one host,
so "the process whose ``NSpid`` chain ends in 42" has three answers.

The disambiguator is the worker's **pid namespace identity**, reported by the
worker itself and carried to the agent by the control plane: the candidate's
``/proc/<pid>/ns/pid`` must read exactly the recorded value. Two workers in two
pid namespaces can never be confused, because the value is an exact match on
the namespace the candidate actually lives in, not on a number the kernel
happens to spell the same way twice.

Why a self-reported value is sound here (and not merely convenient): a worker
inside its own pid namespace **cannot enumerate host pids** -- its ``/proc`` is
its own -- so it cannot observe, let alone substitute, a peer container's
namespace inode. And the claim "I am node N" is already anchored by the C3
identity layer's source-IP second factor before this lookup runs
(``control_plane/api/internal.py``), so a worker can only ever speak for the
node whose network position it holds. What a *wrong* value can do is break its
own grants: every refusal below is fail-closed and named.

This lane drives the lookup against a synthetic ``/proc`` (the shape a real
Linux kernel produces: tab-separated ``NSpid`` chains, ``ns/pid`` symlinks and
per-process ``cgroup`` paths). ``tests/contract/test_c3_identity_grant_grant.py``
drives the same code against a real kernel with two worker containers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from c3_agent.lookup import (
    LookupRefusal,
    ProcLookup,
    SlotProcess,
    WorkerIdentity,
)

#: Two containers' pid namespaces, as ``readlink`` spells them.
NAMESPACE_A = "pid:[4026532458]"
NAMESPACE_B = "pid:[4026532709]"

#: The worker's own cgroup path, as the hostPID agent sees it: the pod UID is
#: part of it (measured in ``docs/c3-privilege-relocation.md`` §14.2.7).
POD_UID = "6d3cdd7b-3a5e-4a1f-9a6b-0c1d2e3f4a5b"
CGROUP_A = f"0::/../kubepods.slice/burstable/pod{POD_UID}/cfee67b0a1d2"


def _proc(proc_root: Path) -> Path:
    proc_root.mkdir(parents=True, exist_ok=True)
    return proc_root


def _candidate(
    proc_root: Path,
    pid: int,
    *,
    nspid: list[int],
    pid_namespace: str,
    cgroup: str = "0::/\n",
    comm: str = "python3",
    start_time: int = 4242,
) -> Path:
    """One ``/proc/<pid>`` entry with the files the lookup reads."""
    entry = _proc(proc_root) / str(pid)
    (entry / "ns").mkdir(parents=True, exist_ok=True)
    (entry / "status").write_text(
        f"Name:\t{comm}\nNSpid:\t" + "\t".join(str(p) for p in nspid) + "\n",
        encoding="utf-8",
    )
    (entry / "cgroup").write_text(cgroup, encoding="utf-8")
    # ``/proc/<pid>/stat``: after ``comm`` the fields start at the state, so the
    # 22nd field overall is index 19 of everything following the last ')'.
    (entry / "stat").write_text(
        f"{pid} ({comm}) S " + " ".join(["0"] * 18 + [str(start_time)]) + "\n",
        encoding="utf-8",
    )
    link = entry / "ns" / "pid"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(pid_namespace, link)
    return entry


def _identity_a(**overrides) -> WorkerIdentity:
    values = dict(node_id="worker-1", pid_namespace=NAMESPACE_A)
    values.update(overrides)
    return WorkerIdentity(**values)


def _lookup(proc_root: Path) -> ProcLookup:
    return ProcLookup(proc_root=proc_root)


# --------------------------------------------- the rendezvous, and its ambiguity


def test_the_host_pid_is_the_candidate_whose_nspid_chain_ends_in_the_reported_pid(
    tmp_path: Path,
) -> None:
    """The plain case: 2147848 is the host pid of container pid 425."""
    root = _proc(tmp_path / "proc")
    _candidate(root, 2147848, nspid=[2147848, 425], pid_namespace=NAMESPACE_A)
    # A neighbour that shares the host pid's number space but is not a match.
    _candidate(root, 2147849, nspid=[2147849, 426], pid_namespace=NAMESPACE_A)

    assert _lookup(root).host_pid(
        425, _identity_a(), sandbox_id="sbx_lookup"
    ) == SlotProcess(
        host_pid=2147848, start_time="4242", pid_namespace=NAMESPACE_A
    )


def test_two_workers_with_the_same_container_pid_are_not_confusable(
    tmp_path: Path,
) -> None:
    """The ruling's own case: worker-1's 42 and worker-2's 42 on one host.

    Both candidates carry the *same* ``NSpid`` tail, which is exactly why
    ``NSpid`` alone is not the proof. Each worker's own identity selects its own
    candidate, and the other worker's process is refused by name -- never
    silently written.
    """
    root = _proc(tmp_path / "proc")
    _candidate(root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A)
    _candidate(root, 3000099, nspid=[3000099, 42], pid_namespace=NAMESPACE_B)
    lookup = _lookup(root)
    identity_a = WorkerIdentity(node_id="worker-1", pid_namespace=NAMESPACE_A)
    identity_b = WorkerIdentity(node_id="worker-2", pid_namespace=NAMESPACE_B)

    assert lookup.host_pid(
        42, identity_a, sandbox_id="sbx_a"
    ) == SlotProcess(3000042, "4242", NAMESPACE_A)
    assert lookup.host_pid(
        42, identity_b, sandbox_id="sbx_b"
    ) == SlotProcess(3000099, "4242", NAMESPACE_B)
    with pytest.raises(LookupRefusal) as excinfo:
        lookup.host_pid(
            42,
            WorkerIdentity(
                node_id="worker-3", pid_namespace="pid:[4026532999]"
            ),
            sandbox_id="sbx_c",
        )
    assert str(excinfo.value) == (
        "container pid 42 is not in worker worker-3's pid namespace "
        "(pid:[4026532999]): refusing"
    )


def test_a_candidate_in_another_namespace_is_never_selected_by_nspid_alone(
    tmp_path: Path,
) -> None:
    """Only the target worker's own process may be resolved.

    This is the disambiguation in isolation: one candidate, matching the
    reported container pid, in a *different* pid namespace. Matching on the
    chain alone would hand the agent a process that belongs to someone else.
    """
    root = _proc(tmp_path / "proc")
    _candidate(root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_B)

    with pytest.raises(LookupRefusal) as excinfo:
        _lookup(root).host_pid(42, _identity_a(), sandbox_id="sbx_other")
    assert str(excinfo.value) == (
        "container pid 42 is not in worker worker-1's pid namespace "
        f"({NAMESPACE_A}): refusing"
    )


def test_two_candidates_in_the_target_namespace_are_refused_as_ambiguous(
    tmp_path: Path,
) -> None:
    """A second match is a refusal, not a coin flip."""
    root = _proc(tmp_path / "proc")
    _candidate(root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A)
    _candidate(root, 3000043, nspid=[3000043, 42], pid_namespace=NAMESPACE_A)

    with pytest.raises(LookupRefusal) as excinfo:
        _lookup(root).host_pid(42, _identity_a(), sandbox_id="sbx_twice")
    assert str(excinfo.value) == (
        "container pid 42 matches more than one process of worker worker-1: "
        "refusing (ambiguous)"
    )


def test_a_host_process_that_never_entered_the_workers_namespace_is_not_a_candidate(
    tmp_path: Path,
) -> None:
    """An unshared pid namespace is what makes the chain longer than one entry.

    The agent's own ``/proc`` is full of single-entry ``NSpid`` lines (every
    process in its own namespace); treating one of those as the worker's child
    would be the "pid namespace is not really part of the proof" bug.
    """
    root = _proc(tmp_path / "proc")
    _candidate(root, 42, nspid=[42], pid_namespace=NAMESPACE_A)

    with pytest.raises(LookupRefusal) as excinfo:
        _lookup(root).host_pid(42, _identity_a(), sandbox_id="sbx_host_pid")
    assert str(excinfo.value) == "沙箱 sbx_host_pid 的槽位 pid 已不在"


# ------------------------------------------------------- the k8s second proof


def test_the_k8s_lane_also_requires_the_pod_in_the_candidates_cgroup(
    tmp_path: Path,
) -> None:
    """pod UID + ``NSpid`` + pid namespace: all three, when the lane has them."""
    root = _proc(tmp_path / "proc")
    _candidate(
        root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A,
        cgroup=CGROUP_A,
    )
    identity = _identity_a(pod_uid=POD_UID)

    assert _lookup(root).host_pid(
        42, identity, sandbox_id="sbx_k8s"
    ) == SlotProcess(3000042, "4242", NAMESPACE_A)


def test_a_candidate_outside_the_target_pod_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """The same pid namespace, but another pod's cgroup: refuse, and name it.

    In the k8s lane the pod UID comes from the API (``spec.nodeName`` is read
    from the worker pod, then the agent pod on that node) rather than from the
    worker, so a candidate that fails it is a candidate the control plane never
    asked about.
    """
    root = _proc(tmp_path / "proc")
    _candidate(
        root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A,
        cgroup="0::/../kubepods.slice/burstable/pod00000000-1111-2222-3333-"
        "444444444444/abc123",
    )

    with pytest.raises(LookupRefusal) as excinfo:
        _lookup(root).host_pid(
            42, _identity_a(pod_uid=POD_UID), sandbox_id="sbx_wrong_pod"
        )
    assert str(excinfo.value) == (
        f"container pid 42 is in a process of worker worker-1 but not in pod "
        f"{POD_UID}'s cgroup: refusing"
    )


def test_a_worker_identity_without_a_usable_pid_namespace_is_refused(
    tmp_path: Path,
) -> None:
    """A lane that cannot supply the identity must fail closed, never match on
    the pid alone."""
    root = _proc(tmp_path / "proc")
    _candidate(root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A)

    with pytest.raises(LookupRefusal) as excinfo:
        _lookup(root).host_pid(
            42,
            WorkerIdentity(node_id="worker-1", pid_namespace=""),
            sandbox_id="sbx_no_identity",
        )
    assert str(excinfo.value) == (
        "worker worker-1 carries no usable pid namespace identity (''): "
        "refusing to resolve a container pid without one"
    )


# ------------------------------------------------------------- the two consumers


def test_the_reported_container_pid_that_is_gone_is_named(tmp_path: Path) -> None:
    """⑤'s other half: the child crashed between the report and the write.

    The lookup is where "there is no such process" is first visible, and the
    name the operator greps for is the one the task brief fixes.
    """
    root = _proc(tmp_path / "proc")
    _candidate(root, 3000042, nspid=[3000042, 41], pid_namespace=NAMESPACE_A)
    with pytest.raises(LookupRefusal) as excinfo:
        ProcLookup(proc_root=root).host_pid(
            424242, _identity_a(), sandbox_id="sbx_gone"
        )
    assert str(excinfo.value) == "沙箱 sbx_gone 的槽位 pid 已不在"


def test_the_resolved_process_is_the_one_that_is_still_there(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _candidate(root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A)
    lookup = _lookup(root)
    slot = lookup.host_pid(42, _identity_a(), sandbox_id="sbx_alive")

    assert lookup.still_alive(slot) is True
    assert lookup.still_alive(
        SlotProcess(3000099, "4242", NAMESPACE_A)
    ) is False


def test_a_recycled_pid_is_not_the_slot_process(tmp_path: Path) -> None:
    """A pid that came back is a *different* process, and it says "gone".

    The kernel reuses pids, so "the write failed and pid 3000042 is still in the
    table" is not the same fact as "the slot's child is still there". The
    resolution records the kernel's own discriminator (``stat`` field 22), and a
    later check that finds a different one must report the child as gone rather
    than forward the primitive's reading of whatever now holds that number.
    """
    root = _proc(tmp_path / "proc")
    _candidate(
        root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A,
        start_time=111,
    )
    lookup = _lookup(root)
    slot = lookup.host_pid(42, _identity_a(), sandbox_id="sbx_recycled")
    assert lookup.still_alive(slot) is True

    # Same pid, same namespace, same container pid -- a different process.
    _candidate(
        root, 3000042, nspid=[3000042, 42], pid_namespace=NAMESPACE_A,
        start_time=999,
    )
    assert lookup.still_alive(slot) is False
