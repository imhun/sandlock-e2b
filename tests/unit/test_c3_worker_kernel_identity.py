"""C3 Task 4 / ruling D21 option 2: the compose lane's identity, from the kernel.

The k8s lane reads the worker's identity from a trusted source the worker cannot
edit (its pod's ``securityContext``). Compose has no pod, so the identity has to
come from the one thing a compose worker also cannot forge: the *kernel's* view
of the worker's own process. The anchor is the value the control plane already
records and already carries for the slot hand-off -- the worker's pid namespace
(``readlink /proc/<pid>/ns/pid``, ruling D9.3) -- and the answer is the uid/gid
the kernel prints for the process(es) in it.

Three facts this lane pins, and each of them is the *reason* the code refuses
rather than falls back:

* **the kernel's value is what is used** -- not the claim the control plane
  forwarded (the claim is what the CP's own record holds; the kernel decides);
* **a claim the kernel does not confirm is refused by name**, and no ``chown``/
  ``rm``/``walk`` is ever exec'd (a refused identity must not read as one that
  was handed over);
* **an anchor that names nothing, or more than one process, is refused by
  name** -- "never guess" is the same rule the slot lookup already follows.

⚠ The reader matters, and it is not decoration. The kernel only lets a process
read *another* process's ``/proc/<pid>/ns/pid`` when their identities match
(``ptrace_may_access``: the same-uid shortcut, or ``CAP_SYS_PTRACE``). The
shipped face B is root with ``CHOWN``/``DAC_OVERRIDE``/``FOWNER`` and no
``CAP_SYS_PTRACE`` -- deliberately, because it runs with ``pid: host`` next to
the control plane -- so the read happens in a child that runs as the *workers'*
own identity. That is what ``E2B_C3_AGENT_RESOLVER_UID``
(:class:`deploy.c3_agent.config.Settings`) names, and it is why this lane models
the reader explicitly instead of pretending the reading process is root.

``tests/contract/test_c3_worker_kernel_identity.py`` drives the same code
against a real kernel and a real container.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from deploy.c3_agent.app import create_app as create_agent_app
from deploy.c3_agent.config import Settings as AgentSettings
from deploy.c3_agent.lookup import (
    LookupRefusal,
    ProcLookup,
    ProcWorkerIdentityResolver,
    SubprocessWorkerIdentityResolver,
    WorkerIdentity,
    worker_identity_refusal_message,
)

#: The worker's pid namespace, as ``readlink`` spells it (the value a real
#: container on this machine answered while the Task 3 lane was written).
NAMESPACE = "pid:[4026532458]"
OTHER_NAMESPACE = "pid:[4026532709]"
WORKER = "worker-1"
SANDBOX = "sbx_kernel_identity"
TOKEN = "c3-agent-sekret"
HOST = "c3-agent"
WORKSPACE = f"/var/lib/e2b-sandboxes/workspaces/{SANDBOX}"
#: The identity the shipped compose workers run as (the worker image's
#: ``USER``) -- and therefore the identity the resolver child runs as.
WORKER_UID = 65534
WORKER_GID = 65534
#: The pooled uid a sandbox would get: what a *forged* claim names.
POOL_UID = 10007


def _proc(proc_root: Path) -> Path:
    proc_root.mkdir(parents=True, exist_ok=True)
    return proc_root


def _process(
    proc_root: Path,
    pid: int,
    *,
    pid_namespace: str,
    real_uid: int = WORKER_UID,
    effective_uid: int = WORKER_UID,
    real_gid: int = WORKER_GID,
    effective_gid: int = WORKER_GID,
    comm: str = "python3",
) -> Path:
    """One ``/proc/<pid>`` entry with the two files the resolver reads.

    ``Uid:``/``Gid:`` carry four numbers (real, effective, saved, fs) and the
    resolver has to name *one*: the worker reports ``os.geteuid()`` /
    ``os.getegid()``, so the effective column is the one compared.
    """
    entry = _proc(proc_root) / str(pid)
    (entry / "ns").mkdir(parents=True, exist_ok=True)
    (entry / "status").write_text(
        f"Name:\t{comm}\n"
        f"Uid:\t{real_uid}\t{effective_uid}\t{effective_uid}\t{effective_uid}\n"
        f"Gid:\t{real_gid}\t{effective_gid}\t{effective_gid}\t{effective_gid}\n"
        f"NSpid:\t{pid}\t1\n",
        encoding="utf-8",
    )
    link = entry / "ns" / "pid"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(pid_namespace, link)
    return entry


def _identity(pid_namespace: str = NAMESPACE, node_id: str = WORKER) -> WorkerIdentity:
    return WorkerIdentity(node_id=node_id, pid_namespace=pid_namespace)


# ------------------------------------------- ① the kernel's value is used


def test_the_workers_own_uid_and_gid_come_from_the_kernel(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=NAMESPACE)
    # A neighbour in another namespace, whose number must not be picked.
    _process(root, 4322, pid_namespace=OTHER_NAMESPACE)

    assert ProcLookup(root).worker_uid_gid(
        _identity(),
        claimed=(WORKER_UID, WORKER_GID),
        reader=(WORKER_UID, WORKER_GID),
    ) == (WORKER_UID, WORKER_GID)


def test_the_effective_column_is_the_identity_that_is_read(tmp_path: Path) -> None:
    """``Uid:``/``Gid:`` carry four numbers; the effective one is the worker's.

    The worker reports ``os.geteuid()``/``os.getegid()``, and it is the
    effective identity a privileged step acts as -- so the resolver reads
    column 2, not column 1 (which a setuid shape could leave at the image's
    own uid).
    """
    root = _proc(tmp_path / "proc")
    _process(
        root,
        500,
        pid_namespace=NAMESPACE,
        real_uid=65533,
        effective_uid=WORKER_UID,
        real_gid=65533,
        effective_gid=WORKER_GID,
    )

    assert ProcLookup(root).worker_uid_gid(
        _identity(),
        claimed=(WORKER_UID, WORKER_GID),
        reader=(WORKER_UID, WORKER_GID),
    ) == (WORKER_UID, WORKER_GID)


# ------------------------------- ② a claim the kernel does not confirm


def test_a_claim_the_kernel_does_not_confirm_is_refused_by_name(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=NAMESPACE)

    with pytest.raises(LookupRefusal) as refusal:
        ProcLookup(root).worker_uid_gid(
            _identity(),
            claimed=(POOL_UID, POOL_UID),
            reader=(WORKER_UID, WORKER_GID),
        )

    assert str(refusal.value) == (
        "worker worker-1 claims uid/gid (10007, 10007), but the kernel says "
        "(65534, 65534) for pid:[4026532458]: refusing (a worker does not name "
        "the identity its privileged steps act as)"
    )


def test_a_claim_that_names_only_the_wrong_group_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """The group is half the identity: the group a sandbox tree is handed to."""
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=NAMESPACE)

    with pytest.raises(LookupRefusal) as refusal:
        ProcLookup(root).worker_uid_gid(
            _identity(),
            claimed=(WORKER_UID, POOL_UID),
            reader=(WORKER_UID, WORKER_GID),
        )

    assert str(refusal.value) == (
        "worker worker-1 claims uid/gid (65534, 10007), but the kernel says "
        "(65534, 65534) for pid:[4026532458]: refusing (a worker does not name "
        "the identity its privileged steps act as)"
    )


def test_a_root_worker_is_refused_by_name(tmp_path: Path) -> None:
    """uid 0 is not a worker identity (it is what ``--worker`` would hand to)."""
    root = _proc(tmp_path / "proc")
    _process(
        root,
        4321,
        pid_namespace=NAMESPACE,
        real_uid=0,
        effective_uid=0,
        real_gid=0,
        effective_gid=0,
    )

    with pytest.raises(LookupRefusal) as refusal:
        ProcLookup(root).worker_uid_gid(_identity(), claimed=(1, 1), reader=(0, 0))

    assert str(refusal.value) == (
        "worker worker-1's process in pid:[4026532458] runs as uid/gid (0, 0) "
        "according to the kernel: refusing (a worker may not run as root)"
    )


# ------------------------- ③ nothing, more than one, and the anchor itself


def test_an_anchor_that_names_no_process_is_refused_by_name(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=OTHER_NAMESPACE)

    with pytest.raises(LookupRefusal) as refusal:
        ProcLookup(root).worker_uid_gid(
            _identity(),
            claimed=(WORKER_UID, WORKER_GID),
            reader=(WORKER_UID, WORKER_GID),
        )

    assert str(refusal.value) == (
        "worker worker-1's pid namespace (pid:[4026532458]) holds no process "
        "this identity resolver can see (it runs as 65534:65534): refusing to "
        "derive its own uid/gid"
    )


def test_an_anchor_that_names_two_processes_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """Two processes in one namespace: never pick one of them."""
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=NAMESPACE)
    _process(root, 4322, pid_namespace=NAMESPACE)

    with pytest.raises(LookupRefusal) as refusal:
        ProcLookup(root).worker_uid_gid(
            _identity(),
            claimed=(WORKER_UID, WORKER_GID),
            reader=(WORKER_UID, WORKER_GID),
        )

    assert str(refusal.value) == (
        "worker worker-1's pid namespace (pid:[4026532458]) holds more than "
        "one process: refusing (ambiguous)"
    )


def test_an_unusable_anchor_is_refused_before_any_proc_walk(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=NAMESPACE)

    with pytest.raises(LookupRefusal) as refusal:
        ProcLookup(root).worker_uid_gid(
            _identity(pid_namespace="not-a-namespace"),
            claimed=(WORKER_UID, WORKER_GID),
            reader=(WORKER_UID, WORKER_GID),
        )

    assert str(refusal.value) == (
        "worker worker-1 carries no usable pid namespace identity "
        "('not-a-namespace'): refusing to derive its own uid/gid from the kernel"
    )


def test_an_unreadable_status_file_is_refused_by_name(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    entry = _process(root, 4321, pid_namespace=NAMESPACE)
    (entry / "status").write_text("Name:\tpython3\n", encoding="utf-8")

    with pytest.raises(LookupRefusal) as refusal:
        ProcLookup(root).worker_uid_gid(
            _identity(),
            claimed=(WORKER_UID, WORKER_GID),
            reader=(WORKER_UID, WORKER_GID),
        )

    assert str(refusal.value) == (
        "the kernel's uid/gid for worker worker-1 (pid namespace "
        "pid:[4026532458]) cannot be read: refusing"
    )


def test_the_shared_refusal_helper_names_the_resolver_identity() -> None:
    """One message builder, so the CLI and the library cannot drift apart."""
    assert worker_identity_refusal_message(
        WORKER, NAMESPACE, (WORKER_UID, WORKER_GID)
    ) == (
        "worker worker-1's pid namespace (pid:[4026532458]) holds no process "
        "this identity resolver can see (it runs as 65534:65534): refusing to "
        "derive its own uid/gid"
    )


# ------------------------------------------------ the resolvers, end to end


def test_the_in_process_resolver_reads_a_synthetic_proc_tree(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=NAMESPACE)
    resolver = ProcWorkerIdentityResolver(
        ProcLookup(root), reader=(WORKER_UID, WORKER_GID)
    )

    assert resolver.resolve(_identity(), claimed=(WORKER_UID, WORKER_GID)) == (
        WORKER_UID,
        WORKER_GID,
    )


class _Completed:
    def __init__(self, *, stdout: str, returncode: int, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _RecordingRunner:
    """The ``subprocess.run`` seam of the production resolver."""

    def __init__(self, *, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self._stdout = stdout
        self._returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return _Completed(stdout=self._stdout, returncode=self._returncode)


def _subprocess_resolver(runner) -> SubprocessWorkerIdentityResolver:
    return SubprocessWorkerIdentityResolver(
        uid=WORKER_UID,
        gid=WORKER_GID,
        timeout_s=5.0,
        process_runner=runner,
        python="/usr/local/bin/python3",
    )


def test_the_subprocess_resolver_asks_the_kernel_and_judges_the_answer() -> None:
    runner = _RecordingRunner(stdout='{"uid": 65534, "gid": 65534}\n')

    assert _subprocess_resolver(runner).resolve(
        _identity(), claimed=(WORKER_UID, WORKER_GID)
    ) == (WORKER_UID, WORKER_GID)

    argv, kwargs = runner.calls[0]
    assert argv == [
        "/usr/local/bin/python3",
        "-m",
        "deploy.c3_agent.lookup",
        "resolve-worker",
        "--node-id",
        WORKER,
        "--pid-namespace",
        NAMESPACE,
        "--claimed-uid",
        str(WORKER_UID),
        "--claimed-gid",
        str(WORKER_GID),
    ]
    # The child runs as the workers' own identity -- the kernel's rule for
    # reading another process's namespace -- and carries no token.
    assert kwargs["user"] == WORKER_UID
    assert kwargs["group"] == WORKER_GID
    assert "E2B_C3_AGENT_TOKEN" not in kwargs["env"]


def test_the_subprocess_resolver_forwards_the_named_refusal() -> None:
    runner = _RecordingRunner(
        stdout=(
            '{"error": "worker worker-1 has no process in its namespace: '
            'refusing"}\n'
        ),
        returncode=3,
    )

    with pytest.raises(LookupRefusal) as refusal:
        _subprocess_resolver(runner).resolve(
            _identity(), claimed=(WORKER_UID, WORKER_GID)
        )

    assert str(refusal.value) == (
        "worker worker-1 has no process in its namespace: refusing"
    )


def test_the_subprocess_resolver_refuses_an_answer_that_is_not_one_identity() -> None:
    runner = _RecordingRunner(stdout="not json\n")

    with pytest.raises(LookupRefusal) as refusal:
        _subprocess_resolver(runner).resolve(
            _identity(), claimed=(WORKER_UID, WORKER_GID)
        )

    assert str(refusal.value) == (
        "the identity resolver answered something that is not one uid/gid "
        "('not json\\n'): refusing"
    )


# ------------------------------------- face B: the instruction, end to end


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
    _process(root, 4321, pid_namespace=NAMESPACE, **process_kwargs)
    lookup = ProcLookup(root)
    runner = _StubMaintRunner()
    resolver = ProcWorkerIdentityResolver(lookup, reader=(WORKER_UID, WORKER_GID))
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
                "pid_namespace": NAMESPACE,
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
                "pid_namespace": NAMESPACE,
            }
        ),
    )

    assert resp.status_code == 502
    assert resp.json() == {
        "error": (
            "worker worker-1 claims uid/gid (10007, 10007), but the kernel "
            "says (65534, 65534) for pid:[4026532458]: refusing (a worker does "
            "not name the identity its privileged steps act as)"
        )
    }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_anchor_that_names_nothing_execs_nothing(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=OTHER_NAMESPACE)
    lookup = ProcLookup(root)
    runner = _StubMaintRunner()
    app = _agent(
        runner=runner,
        lookup=lookup,
        resolver=ProcWorkerIdentityResolver(lookup, reader=(WORKER_UID, WORKER_GID)),
    )

    resp = await _post(app, _body())

    assert resp.status_code == 502
    assert resp.json() == {
        "error": (
            "worker worker-1's pid namespace (pid:[4026532458]) holds no "
            "process this identity resolver can see (it runs as 65534:65534): "
            "refusing to derive its own uid/gid"
        )
    }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_anchor_that_names_two_processes_execs_nothing(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, 4321, pid_namespace=NAMESPACE)
    _process(root, 4322, pid_namespace=NAMESPACE)
    lookup = ProcLookup(root)
    runner = _StubMaintRunner()
    app = _agent(
        runner=runner,
        lookup=lookup,
        resolver=ProcWorkerIdentityResolver(lookup, reader=(WORKER_UID, WORKER_GID)),
    )

    resp = await _post(app, _body())

    assert resp.status_code == 502
    assert resp.json() == {
        "error": (
            "worker worker-1's pid namespace (pid:[4026532458]) holds more "
            "than one process: refusing (ambiguous)"
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
                "pid_namespace": NAMESPACE,
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
