"""C3 Task 4 / rulings D21 option 2 + D25: the compose lane's identity, from
the kernel -- out of files the reading face is actually allowed to read.

The k8s lane reads the worker's identity from a trusted source the worker cannot
edit (its pod's ``securityContext``). Compose has no pod, so the identity has to
come from the one thing a compose worker also cannot forge: the *kernel's* view
of the worker's own processes. Ruling **D25** fixes *which* kernel view, because
the face that asks (face B) cannot do the ptrace-guarded read:

* the anchor is the worker's **container id** -- the value its hostname carries
  and its host-side ``/proc/<pid>/cgroup`` contains;
* the candidate is the process that cgroup anchors **and which is the
  container's init** -- because a container's cgroup does *not* hold one uid on
  this lane: a sandbox's ``sandlock-supervise`` runs as the pooled uid inside
  the worker's container cgroup (measured 2026-09-29);
* the answer is the uid/gid the kernel prints in ``/proc/<pid>/status`` for that
  process -- both files are **world-readable**, so no capability is added and no
  uid is changed;
* the reader therefore does not matter here, and this lane no longer models one.

Four facts this lane pins, and each of them is the *reason* the code refuses
rather than falls back:

* **the kernel's value is what is used** -- not the claim the control plane
  forwarded (the claim is what the CP's own record holds; the kernel decides);
* **a claim the kernel does not confirm is refused by name**, and no ``chown``/
  ``rm``/``walk`` is ever exec'd (a refused identity must not read as one that
  was handed over);
* **an anchor that names nothing is refused by name** -- "never guess" is the
  same rule the slot lookup already follows;
* **candidates whose kernel identities disagree are refused by name**, while
  several candidates that *agree* are fine: uniqueness of the process set is
  not a property of the value (ruling D4's relaxation; the old "exactly one
  process" rule is what squeezed the compose lanes to one live slot per worker).

``tests/contract/test_c3_worker_kernel_identity.py`` drives the same code
against a real kernel and a real container.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from deploy.c3_agent.app import create_app as create_agent_app
from deploy.c3_agent.config import Settings as AgentSettings
from deploy.c3_agent.lookup import (
    LookupRefusal,
    ProcLookup,
    ProcWorkerIdentityResolver,
    WorkerAnchor,
)
from gateway_common.worker_identity import (
    container_cgroup_token,
    validate_container_id,
)

#: The worker's container id (the full 64 hexadecimal characters a runtime
#: uses) and the anchor it reports -- Docker's default hostname, the first 12.
CONTAINER_ID = "e4a98a0c528215e380373d982fc1fccaf49787a8f29ad785b64220ce1e16ead9"
ANCHOR = CONTAINER_ID[:12]
#: A different container on the same host: its processes must never be picked.
OTHER_ID = "e8771bd2b487e60f383538f397c39c45cf60ca8c885527117e2127b5e6623367"
WORKER = "worker-1"
SANDBOX = "sbx_kernel_identity"
TOKEN = "c3-agent-sekret"
HOST = "c3-agent"
WORKSPACE = f"/var/lib/e2b-sandboxes/workspaces/{SANDBOX}"
#: The identity the shipped compose workers run as (the worker image's ``USER``).
WORKER_UID = 65534
WORKER_GID = 65534
#: The pooled uid a sandbox would get: what a *forged* claim names.
POOL_UID = 10007


def _cgroup(container_id: str) -> str:
    """What a candidate's host-side cgroup looks like.

    Measured on the compose lanes 2026-09-29 (OrbStack):
    ``0::/../e4a98a0c528215e…`` -- the container id verbatim, which is why the
    anchor's 12-character hostname is a substring of it by construction.
    """
    return f"0::/../{container_id}"


def _proc(proc_root: Path) -> Path:
    proc_root.mkdir(parents=True, exist_ok=True)
    return proc_root


def _process(
    proc_root: Path,
    pid: int,
    *,
    container_id: str = CONTAINER_ID,
    cgroup_text: str | None = None,
    real_uid: int = WORKER_UID,
    effective_uid: int = WORKER_UID,
    real_gid: int = WORKER_GID,
    effective_gid: int = WORKER_GID,
    comm: str = "python3",
    status_text: str | None = None,
    nspid: str | None = None,
) -> Path:
    """One ``/proc/<pid>`` entry with the two files the resolver reads.

    ``Uid:``/``Gid:`` carry four numbers (real, effective, saved, fs) and the
    resolver has to name *one*: the worker reports ``os.geteuid()`` /
    ``os.getegid()``, so the effective column is the one compared.

    ``cgroup_text`` overrides the path (for anchors that must not match);
    ``status_text`` overrides the whole status file (for a kernel answer that
    cannot be read at all).
    """
    entry = _proc(proc_root) / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    body = status_text if status_text is not None else (
        f"Name:\t{comm}\n"
        f"Uid:\t{real_uid}\t{effective_uid}\t{effective_uid}\t{effective_uid}\n"
        f"Gid:\t{real_gid}\t{effective_gid}\t{effective_gid}\t{effective_gid}\n"
        f"NSpid:\t{nspid if nspid is not None else f'{pid}\t1'}\n"
    )
    (entry / "status").write_text(body, encoding="utf-8")
    (entry / "cgroup").write_text(
        cgroup_text if cgroup_text is not None else _cgroup(container_id),
        encoding="utf-8",
    )
    return entry


def _anchor(container_id: str = ANCHOR, node_id: str = WORKER) -> WorkerAnchor:
    return WorkerAnchor(node_id=node_id, container_id=container_id)


# ------------------------------------------- ① the kernel's value is used


def test_the_workers_own_uid_and_gid_come_from_the_kernel(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321)
    # A neighbour in another container, whose *different* identity must not be
    # picked: it is the reason the anchor is matched, not just any process.
    _process(root, 4322, container_id=OTHER_ID, effective_uid=11111,
             effective_gid=11111)

    assert ProcLookup(root).worker_uid_gid(_anchor()) == (WORKER_UID, WORKER_GID)


def test_the_effective_column_is_the_identity_that_is_read(tmp_path: Path) -> None:
    """The worker reports ``geteuid()``/``getegid()``, so that is the column."""
    root = _proc(tmp_path / "proc")
    _process(root, 4321, real_uid=10007, effective_uid=WORKER_UID,
             real_gid=10007, effective_gid=WORKER_GID)

    assert ProcLookup(root).worker_uid_gid(_anchor()) == (WORKER_UID, WORKER_GID)


def test_a_claim_the_kernel_does_not_confirm_is_refused_by_name(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321)

    with pytest.raises(LookupRefusal) as refused:
        ProcLookup(root).worker_uid_gid(_anchor(), claimed=(POOL_UID, POOL_UID))

    assert str(refused.value) == (
        "worker worker-1 claims uid/gid (10007, 10007), but the kernel says "
        "(65534, 65534) for e4a98a0c5282: refusing (a worker does not name the "
        "identity its privileged steps act as)"
    )


def test_a_claim_that_names_only_the_wrong_group_is_refused_by_name(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321)

    with pytest.raises(LookupRefusal) as refused:
        ProcLookup(root).worker_uid_gid(_anchor(), claimed=(WORKER_UID, POOL_UID))

    assert str(refused.value) == (
        "worker worker-1 claims uid/gid (65534, 10007), but the kernel says "
        "(65534, 65534) for e4a98a0c5282: refusing (a worker does not name the "
        "identity its privileged steps act as)"
    )


def test_a_root_worker_is_refused_by_name(tmp_path: Path) -> None:
    """uid 0 is not a worker identity (the same rule the pool gate applies)."""
    root = _proc(tmp_path / "proc")
    _process(root, 4321, real_uid=0, effective_uid=0, real_gid=0, effective_gid=0)

    with pytest.raises(LookupRefusal) as refused:
        ProcLookup(root).worker_uid_gid(_anchor())

    assert str(refused.value) == (
        "worker worker-1's processes in e4a98a0c5282 run as uid/gid (0, 0) "
        "according to the kernel: refusing (a worker may not run as root)"
    )


# ------------------------------------------------------- ② the anchor is one


def test_an_anchor_no_process_carries_is_refused_by_name(tmp_path: Path) -> None:
    """Zero candidates: the worker was recreated, or the anchor is stale."""
    root = _proc(tmp_path / "proc")
    _process(root, 4321, container_id=OTHER_ID)

    with pytest.raises(LookupRefusal) as refused:
        ProcLookup(root).worker_uid_gid(_anchor())

    assert str(refused.value) == (
        "worker worker-1's container (e4a98a0c5282) holds no process this agent "
        "can identify as the worker (the container's init): refusing to derive "
        "its own uid/gid from the kernel"
    )


def test_a_container_with_no_init_shaped_process_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """Candidates exist but none is the worker: ``pid: host`` would do this.

    A worker that shares the host pid namespace is not pid 1 in its container,
    so nothing identifies "the worker's own process" -- a named refusal, the
    same class of constraint as overriding ``hostname:``.
    """
    root = _proc(tmp_path / "proc")
    _process(root, 4321, nspid="4321")          # a single-entry chain: no container ns

    with pytest.raises(LookupRefusal) as refused:
        ProcLookup(root).worker_uid_gid(_anchor())

    assert str(refused.value) == (
        "worker worker-1's container (e4a98a0c5282) holds no process this agent "
        "can identify as the worker (the container's init): refusing to derive "
        "its own uid/gid from the kernel"
    )


@pytest.mark.parametrize(
    "reported",
    [
        "my-worker",                        # a stack that overrode ``hostname:``
        "c3-agent-proxy",                   # ... or named it after a service
        "e4a98a0c528",                      # too short to be an identity
        "E4A98A0C5282",                     # not lowercase hex
        "e4a98a0c528215e380373d982fc1fcca"
        "f49787a8f29ad785b64220ce1e16ead9e",  # longer than any container id
    ],
)
def test_an_anchor_that_is_not_a_container_id_is_refused_by_name(
    tmp_path: Path, reported: str
) -> None:
    """The shape rule is what turns ``hostname:`` into a *named* refusal.

    A loose substring match on a value a deployment can set would be a way to
    point the identity at somebody else's processes; the shape check (12..64
    lowercase hex) closes it, and the control plane applies the same rule when
    it *stores* the report.
    """
    root = _proc(tmp_path / "proc")
    _process(root, 4321)

    with pytest.raises(LookupRefusal) as refused:
        ProcLookup(root).worker_uid_gid(_anchor(container_id=reported))

    assert str(refused.value) == (
        f"worker worker-1 carries no usable container id ({reported!r}): "
        "refusing to derive its own uid/gid from the kernel"
    )


def test_a_candidate_whose_identity_cannot_be_read_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """A candidate that *is* the init but whose ``status`` cannot be parsed.

    ``_uid_gid`` answers ``None`` for both "gone" and "unreadable"; a process
    that is still there but whose kernel answer cannot be read is a refusal, not
    a skip (the branch that tells the two apart is next to it).
    """
    root = _proc(tmp_path / "proc")
    _process(root, 4321, status_text="Name:\tpython3\n")   # no Uid:/Gid: lines

    class _Unreadable(ProcLookup):
        def _is_container_init(self, pid: int) -> bool:
            return True

    with pytest.raises(LookupRefusal) as refused:
        _Unreadable(root).worker_uid_gid(_anchor())

    assert str(refused.value) == (
        "the kernel's uid/gid for one of worker worker-1's processes cannot be "
        "read: refusing"
    )


def test_a_candidate_that_vanishes_between_walk_and_read_is_skipped(
    tmp_path: Path,
) -> None:
    """A process that exits mid-walk is not an ambiguity, it is just gone.

    The windows are adjacent (the walk reads ``cgroup``, the answer reads
    ``status``), so a short-lived child of the worker can disappear in between.
    ``_uid_gid`` answers ``None`` for both "gone" and "unreadable"; the two are
    told apart by asking the tree again, and only the second is a refusal.
    """
    root = _proc(tmp_path / "proc")
    _process(root, 4321)
    _process(root, 4322)

    class _Vanishing(ProcLookup):
        def _uid_gid(self, pid: int):
            if pid == 4322:
                shutil.rmtree(self._proc_root / str(pid))
                return None
            return super()._uid_gid(pid)

    assert _Vanishing(root).worker_uid_gid(_anchor()) == (WORKER_UID, WORKER_GID)


def test_a_busy_container_still_resolves_to_the_workers_identity(
    tmp_path: Path,
) -> None:
    """**The regression guard for the old uniqueness rule** (D4, as measured).

    This is the real shape of a busy worker (measured on the compose multinode
    stack 2026-09-29), and the reason the anchor cannot be matched by cgroup
    alone:

    ```
    pid=3652371 uid= 65534 NSpid: 3652371 1     python -m envd_service      # the worker
    pid=3652762 uid= 10000 NSpid: 3652762 71    …/sandlock-supervise        # a slot
    pid=3652780 uid= 10000 NSpid: 3652780 85 1  …/sandlock-supervise        # in the sandbox's ns
    pid=3652781 uid= 10000 NSpid: 3652781 86 2  /bin/sh -c trap …           # the parking shell
    ```

    The sandbox's processes share the worker's *container cgroup* while running
    as a pooled uid, so candidates are "the container's init" — and several
    processes in the container are the normal state, not an ambiguity. The old
    "exactly one process in the namespace" predicate refused this shape, which
    squeezed the compose lanes to one live slot per worker.
    """
    root = _proc(tmp_path / "proc")
    _process(root, 4321)                                      # the worker (init)
    _process(root, 4322, effective_uid=10000, effective_gid=10000,
             nspid="4322\t71")                                # a slot, worker pid ns
    _process(root, 4323, effective_uid=10000, effective_gid=10000,
             nspid="4323\t85\t1")                             # a slot, its own pid ns
    _process(root, 4324, effective_uid=10000, effective_gid=10000,
             comm="sh", nspid="4324\t86\t2")                  # the parking shell

    assert ProcLookup(root).worker_uid_gid(_anchor()) == (WORKER_UID, WORKER_GID)


def test_several_agreeing_candidates_are_still_not_an_ambiguity(
    tmp_path: Path,
) -> None:
    """Two init-shaped candidates with the same identity ⇒ still the identity.

    This is the literal form of D4's relaxation (the ruling's own words: a
    container's processes share one uid, so several candidates are not an
    ambiguity in the *value*). It cannot happen on a normal runtime -- one
    container has one init -- but the predicate must be "they agree", not
    "there is exactly one".
    """
    root = _proc(tmp_path / "proc")
    _process(root, 4321)
    _process(root, 4322)

    assert ProcLookup(root).worker_uid_gid(_anchor()) == (WORKER_UID, WORKER_GID)


def test_candidates_that_disagree_are_refused_by_name(tmp_path: Path) -> None:
    """Two answers for one anchor ⇒ refuse: the anchor matched two identities.

    This is the rule that replaces uniqueness, and it is the *second* layer: the
    init filter already answers "which process is the worker". If two
    init-shaped candidates ever appear under one anchor (a value that shows up
    in another container's cgroup path as well, say), adopting either answer
    would be a guess about whose identity the file operations act as.
    """
    root = _proc(tmp_path / "proc")
    _process(root, 4321)
    _process(root, 4322, effective_uid=11111, effective_gid=11111)

    with pytest.raises(LookupRefusal) as refused:
        ProcLookup(root).worker_uid_gid(_anchor())

    assert str(refused.value) == (
        "worker worker-1's container (e4a98a0c5282) holds processes whose "
        "kernel identities disagree ([(11111, 11111), (65534, 65534)]): "
        "refusing to derive one identity from them"
    )


def test_the_in_process_resolver_reads_a_synthetic_proc_tree(tmp_path: Path) -> None:
    """The shipped resolver is this one: in-process, no child, no uid change."""
    root = _proc(tmp_path / "proc")
    _process(root, 4321)
    resolver = ProcWorkerIdentityResolver(ProcLookup(root))

    assert resolver.resolve(_anchor(), claimed=(WORKER_UID, WORKER_GID)) == (
        WORKER_UID,
        WORKER_GID,
    )


def test_the_container_id_shape_rule_and_its_cgroup_token() -> None:
    """``validate_container_id`` is shared by the worker, the CP and the agent."""
    assert validate_container_id("e4a98a0c5282") is True
    assert validate_container_id(CONTAINER_ID) is True
    assert validate_container_id("e4a98a0c528") is False       # 11 characters
    assert validate_container_id("E4A98A0C5282") is False       # not lowercase
    assert validate_container_id("e4a98a0c528g") is False       # not hex
    assert validate_container_id(CONTAINER_ID + "0") is False   # 65 characters
    assert validate_container_id("") is False
    assert container_cgroup_token(ANCHOR) == ANCHOR


# ------------------------------------- ③ face B: the instruction, end to end


class _StubMaintRunner:
    def __init__(self, *, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._stdout = stdout

    def run(self, argv: list[str], *, env: dict[str, str]) -> str:
        self.calls.append((list(argv), dict(env)))
        return self._stdout


def _agent(*, runner, lookup, resolver):
    return create_agent_app(
        settings=AgentSettings(token=TOKEN, node_id=HOST),
        maint_runner=runner,
        lookup=lookup,
        identity_resolver=resolver,
    )


def _shape(tmp_path: Path, **process_kwargs) -> tuple:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, **process_kwargs)
    lookup = ProcLookup(root)
    runner = _StubMaintRunner()
    resolver = ProcWorkerIdentityResolver(lookup)
    return runner, lookup, resolver


def _body(*, worker: dict | None = None) -> dict:
    return {
        "sandbox_id": SANDBOX,
        "path": WORKSPACE,
        "recursive": True,
        "worker_owned": False,
        "uid": POOL_UID,
        # The group the derivation names for ``chown-workspace`` is the worker's
        # own gid -- which is exactly what the kernel read has to confirm.
        "gid": WORKER_GID,
        "worker": (
            {
                "node_id": WORKER,
                "uid": WORKER_UID,
                "gid": WORKER_GID,
                "container_id": ANCHOR,
            }
            if worker is None
            else worker
        ),
    }


async def _post(app, body: dict, *, op: str = "chown"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://agent"
    ) as client:
        return await client.post(
            f"/internal/nodes/{HOST}/agent/{op}",
            headers={"X-Internal-Key": TOKEN},
            json=body,
        )


@pytest.mark.asyncio
async def test_a_chown_with_an_anchor_runs_maint_as_the_kernel_identity(
    tmp_path: Path,
) -> None:
    """The anchor is resolved and the *kernel's* identity reaches the child."""
    runner, lookup, resolver = _shape(tmp_path)
    app = _agent(runner=runner, lookup=lookup, resolver=resolver)

    resp = await _post(app, _body())

    assert resp.status_code == 200
    assert resp.json() == {
        "op": "chown",
        "path": WORKSPACE,
        "uid": POOL_UID,
        "gid": WORKER_GID,
        "recursive": True,
    }
    argv, env = runner.calls[0]
    assert argv == [
        "/var/lib/e2b-priv/e2b-maint",
        "chown",
        "--uid",
        str(POOL_UID),
        "--gid",
        str(WORKER_GID),
        "--recursive",
        "--path",
        WORKSPACE,
    ]
    assert env["E2B_BROKER_WORKER_UID"] == str(WORKER_UID)
    assert env["E2B_BROKER_WORKER_GID"] == str(WORKER_GID)


@pytest.mark.asyncio
async def test_a_busy_worker_still_gets_its_identity(tmp_path: Path) -> None:
    """D4's relaxation, at the agent: several worker processes ⇒ still a chown.

    A running slot, an operator's ``docker exec`` and an unreaped child all live
    in the worker's own container. Before this ruling each of them turned a
    legitimate file operation into "holds more than one process: refusing".
    """
    root = _proc(tmp_path / "proc")
    for pid in (4321, 4322, 4323):
        _process(root, pid)
    lookup = ProcLookup(root)
    runner = _StubMaintRunner()
    app = _agent(
        runner=runner,
        lookup=lookup,
        resolver=ProcWorkerIdentityResolver(lookup),
    )

    resp = await _post(app, _body())

    assert resp.status_code == 200
    _, env = runner.calls[0]
    assert env["E2B_BROKER_WORKER_UID"] == str(WORKER_UID)


@pytest.mark.asyncio
async def test_a_claim_the_kernel_does_not_confirm_execs_nothing(
    tmp_path: Path,
) -> None:
    """The verification is the point: a disagreeing claim must not chown.

    If the resolver is dropped (the sent value trusted again) this case execs
    ``e2b-maint chown --worker`` for uid 10007 -- exactly the C3 §14.3 hole this
    lane exists to close -- so the test fails on the exec, not on a detail.
    """
    runner, lookup, resolver = _shape(tmp_path)
    app = _agent(runner=runner, lookup=lookup, resolver=resolver)

    resp = await _post(
        app,
        _body(
            worker={
                "node_id": WORKER,
                "uid": POOL_UID,
                "gid": POOL_UID,
                "container_id": ANCHOR,
            }
        ),
    )

    assert resp.status_code == 502
    assert resp.json() == {
        "error": (
            "worker worker-1 claims uid/gid (10007, 10007), but the kernel "
            "says (65534, 65534) for e4a98a0c5282: refusing (a worker does not "
            "name the identity its privileged steps act as)"
        )
    }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_anchor_that_names_nothing_execs_nothing(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, container_id=OTHER_ID)
    lookup = ProcLookup(root)
    runner = _StubMaintRunner()
    app = _agent(
        runner=runner,
        lookup=lookup,
        resolver=ProcWorkerIdentityResolver(lookup),
    )

    resp = await _post(app, _body())

    assert resp.status_code == 502
    assert resp.json() == {
        "error": (
            "worker worker-1's container (e4a98a0c5282) holds no process this "
            "agent can identify as the worker (the container's init): refusing "
            "to derive its own uid/gid from the kernel"
        )
    }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_instruction_without_an_anchor_is_the_k8s_lane_unchanged(
    tmp_path: Path,
) -> None:
    """No anchor ⇒ the value the control plane sent, exactly as before.

    The k8s lane's source is the pod spec, so nothing here resolves anything and
    the resolver is never consulted -- which is what "the k8s lane stays as it
    is" means at this layer.
    """
    runner, lookup, resolver = _shape(tmp_path)
    app = _agent(runner=runner, lookup=lookup, resolver=resolver)

    resp = await _post(
        app,
        _body(worker={"node_id": WORKER, "uid": WORKER_UID, "gid": WORKER_GID}),
    )

    assert resp.status_code == 200
    _, env = runner.calls[0]
    assert env["E2B_BROKER_WORKER_UID"] == str(WORKER_UID)


@pytest.mark.asyncio
async def test_an_anchor_without_a_worker_name_is_a_shape_refusal(
    tmp_path: Path,
) -> None:
    runner, lookup, resolver = _shape(tmp_path)
    app = _agent(runner=runner, lookup=lookup, resolver=resolver)

    resp = await _post(
        app,
        _body(
            worker={
                "uid": WORKER_UID,
                "gid": WORKER_GID,
                "container_id": ANCHOR,
            }
        ),
    )

    assert resp.status_code == 400
    assert resp.json() == {
        "error": (
            "a kernel-anchored worker instruction must name the worker "
            "(worker.node_id): refusing"
        )
    }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_self_heal_removal_carries_no_identity_and_is_untouched(
    tmp_path: Path,
) -> None:
    """The sweep's ``rm`` names no worker, so nothing is resolved or refused."""
    runner, lookup, resolver = _shape(tmp_path)
    app = _agent(runner=runner, lookup=lookup, resolver=resolver)

    resp = await _post(
        app,
        {"sandbox_id": SANDBOX, "path": WORKSPACE, "worker": None},
        op="rm",
    )

    assert resp.status_code == 200
    assert resp.json() == {"op": "rm", "path": WORKSPACE}
    argv, env = runner.calls[0]
    assert argv == ["/var/lib/e2b-priv/e2b-maint", "rm", "--path", WORKSPACE]
    assert "E2B_BROKER_WORKER_UID" not in env
