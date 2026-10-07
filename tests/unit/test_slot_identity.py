"""C3 Task 3 slice A (ruling D9.1): the worker's identity-grant startup path.

On this path the worker holds **no privilege at all**: it forks the slot's
child, the child unshares its user namespace, the worker reports
``{sandbox_id, pid}`` to the control plane (fire-and-forget: the child polls
``setresuid(X)`` itself and execs ``sandlock-supervise``; nothing "releases"
it), and the identity is written by the agent. ``E2B_SLOT_IDENTITY=agent-grant``
selects it; the default stays ``spawn`` so the broker path is still there until
Task 4/7 retire it.

Two rules from the plan are pinned here at the config and session level, because
no runtime assertion can see them:

* the report carries **no uid** (a fake control plane that rejects an extra
  ``uid`` field is the test);
* the worker has neither the agent's address nor its token -- there is no
  ``worker ↔ agent`` channel to speak of, so the only host its identity client
  ever dials is the control plane's.
"""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import envd_service.own_identity as rb
import envd_service.slot_identity as si
import envd_service.worker_identity as wi
from envd_service.config import Settings
from envd_service.priv_helpers import PrivHelperError, request_identity
from envd_service.own_identity import OwnIdentityConfig, W1SlotPool

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_URL = "http://control-plane:3000"
NODE_ID = "worker-1"
SANDBOX_ID = "sbx_slot_identity"
CHILD_PID = 4242
PID_NAMESPACE = "pid:[4026532458]"
#: The real ``Popen``, kept before any test patches the module attribute.
_REAL_POPEN = subprocess.Popen


class FakeProcess:
    """The spawned child, as the pool sees it."""

    def __init__(self, pid: int = CHILD_PID) -> None:
        self.pid = pid
        self.stderr = None
        self.returncode: int | None = None
        self.killed = 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return 0


class FakeChannel:
    """Answers the readiness probe and records the order of every verb."""

    def __init__(self, handle, order: list, replies: dict) -> None:
        self._order = order
        self._replies = replies

    def request(self, verb, args=None, fds=()):
        self._order.append(verb)
        return self._replies.get(verb, {})

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _settings(**overrides) -> SimpleNamespace:
    values = dict(
        own_identity="on",
        max_slots=0,
        uid_pool_start=20000,
        uid_pool_size=8,
        slot_tmp_root="/tmp/c3-slot-identity-test",
        slot_transport="fd",
        slot_verb_timeout_s=15.0,
        slot_identity="agent-grant",
        control_plane_url=CONTROL_PLANE_URL,
        node_id=NODE_ID,
        internal_api_key="internal-key",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# ----------------------------------------------------------- the worker's mode


def test_the_only_slot_identity_mode_left_is_agent_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3 is the shape; the pre-C3 starter is a named refusal (N52)."""
    monkeypatch.delenv("E2B_SLOT_IDENTITY", raising=False)
    assert Settings().slot_identity == "agent-grant"
    bare = _settings()
    del bare.slot_identity  # an embedder's settings object, not the worker's
    monkeypatch.setenv("E2B_SLOT_IDENTITY", "agent-grant")
    assert OwnIdentityConfig.from_settings(bare).slot_identity == "agent-grant"
    # The worker's own resolved setting wins over the environment.
    assert (
        OwnIdentityConfig.from_settings(_settings(slot_identity="agent-grant")).slot_identity
        == "agent-grant"
    )
    # Anything else -- including the retired `spawn` -- is named, not guessed.
    for retired in ("spawn", "something-else"):
        monkeypatch.setenv("E2B_SLOT_IDENTITY", retired)
        with pytest.raises(PrivHelperError) as excinfo:
            OwnIdentityConfig.from_settings(bare)
        assert "must be 'agent-grant'" in str(excinfo.value)
        assert "retired" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        OwnIdentityConfig(slot_identity="spawn")
    assert "retired" in str(excinfo.value)


def test_agent_grant_needs_a_reporter_rather_than_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the mode: no root, no broker -- only the CP."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    assert (
        OwnIdentityConfig(slot_identity="agent-grant", identity_reporter=lambda *a: {})
        .privileged_starter
        is True
    )
    assert (
        OwnIdentityConfig(slot_identity="agent-grant").privileged_starter is False
    ), "without a reporter the child could never be granted an identity"


def test_the_pool_never_gets_a_broker_spawner(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no privileged starter left to wire (C3 / N52).

    The file-capability spawner was the *old* starter: it performed the setuid
    itself, which is exactly the privileged step C3 replaced. It is gone from
    the worker (open-issues N52), so ``from_settings`` hands the pool no
    spawner at all and the child it starts is the unprivileged one that polls
    for its grant.
    """
    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", CONTROL_PLANE_URL)
    monkeypatch.setenv("E2B_NODE_ID", NODE_ID)
    agent_grant = OwnIdentityConfig.from_settings(
        _settings(slot_identity="agent-grant")
    )
    assert agent_grant.spawner is None
    assert agent_grant.identity_reporter is not None


def test_the_child_comes_from_clone3_and_the_parent_half_never_execs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No privileged helper, and no ``python -m`` hop either.

    ``e2b-slot-spawn`` (the broker) and ``setpriv`` are what the *old* path
    needed; this one must not reach for either -- the worker is unprivileged and
    the identity comes from the agent. N80 (2026-10-06) also dropped the
    ``python -m envd_service.slot_identity`` hop: ``clone3`` creates the child
    inside the namespace, so the only thing that ever execs is
    ``sandlock-supervise``, and it does so in the child half.
    """
    import envd_service.slot_identity as si

    cloned: list[str] = []

    def _fake_clone3() -> int:
        cloned.append("clone3")
        # The parent half: a pid that is not our child, so poll() reports it
        # gone instead of blocking the test.
        return 999_999

    executed: list[tuple] = []
    monkeypatch.setattr(si, "_clone3_new_user_namespace", _fake_clone3)
    monkeypatch.setattr(si.os, "execvpe", lambda *a, **k: executed.append(a))

    process = si.spawn_child(
        uid=20001,
        supervise_argv=[
            "/wheels/sandlock/bin/sandlock-supervise",
            "--policy",
            "/tmp/policy.json",
            "--uid",
            "20001",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert cloned == ["clone3"]
        assert process.pid == 999_999
        # The parent half hands the child its stdio and returns; it never execs.
        assert executed == []
        assert process.stderr is not None
    finally:
        process.kill()
        process.stderr.close()


def _drive_the_child_half(monkeypatch: pytest.MonkeyPatch, *, pass_fds=()):
    """Run ``spawn_child`` as the clone3 child and report what it reached.

    The child half only runs when ``_clone3_new_user_namespace`` returns 0, and
    it ends in ``os._exit``/``execvpe``. Both are replaced here so the test can
    see the order instead of dying with the child.
    """
    import envd_service.slot_identity as si

    class _ChildFinished(Exception):
        """Unwinds the child half at its own ``os._exit``."""

    monkeypatch.setattr(si, "_clone3_new_user_namespace", lambda: 0)
    monkeypatch.setattr(si, "_close_fds_except", lambda keep: None)
    monkeypatch.setattr(si.os, "dup2", lambda fd, target: None)

    polls: list[float] = []
    executed: list[tuple] = []
    exits: list[int] = []

    def _fake_await(uid: int, *, deadline_s: float, **kwargs) -> bool:
        polls.append(deadline_s)
        return True

    def _fake_exit(code: int) -> None:
        exits.append(code)
        raise _ChildFinished(code)

    monkeypatch.setattr(si, "_await_identity", _fake_await)
    monkeypatch.setattr(
        si.os,
        "execvpe",
        lambda file, *a, **k: executed.append(
            (file, a[0], {fd: os.get_inheritable(fd) for fd in pass_fds})
        ),
    )
    monkeypatch.setattr(si.os, "_exit", _fake_exit)

    with pytest.raises(_ChildFinished):
        si.spawn_child(
            uid=20001,
            supervise_argv=["/wheels/sandlock/bin/sandlock-supervise"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
        )
    return polls, executed, exits


def test_the_child_half_reaches_the_poll_with_the_modules_own_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child must not call its own ``timeout_s`` parameter as a helper.

    ``spawn_child``'s ``timeout_s`` argument shadows the module function of the
    same name; the default path called the *argument* -- ``None`` -- as if it
    were the helper, so every real child (route B never passes the argument)
    died with ``os._exit(4)`` before its first ``setresuid``. The worker still
    reported the pid, the agent then aimed ``as_uid`` at a dead process, and the
    create failed with "cannot write uid_map for pid N: Permission denied": a
    reaped task's id-map files are root-owned, so a 65534 grantor cannot even
    open them.
    """
    monkeypatch.setenv("E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S", "7.5")

    polls, executed, exits = _drive_the_child_half(monkeypatch)

    assert exits == [9], "the child must reach exec, not die on the way there"
    assert polls == [7.5], "the env knob must be read through the helper"
    assert executed == [
        (
            "/wheels/sandlock/bin/sandlock-supervise",
            ["/wheels/sandlock/bin/sandlock-supervise"],
            {},
        )
    ]


def test_the_child_half_defaults_to_the_module_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped path: route B passes no ``timeout_s`` at all."""
    monkeypatch.delenv("E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S", raising=False)

    polls, _executed, exits = _drive_the_child_half(monkeypatch)

    assert exits == [9]
    assert polls == [si.DEFAULT_TIMEOUT_S]


def test_the_child_half_clears_close_on_exec_for_the_slots_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pass_fds`` must survive the child's ``execve``.

    ``subprocess.Popen(pass_fds=...)`` cleared close-on-exec on those
    descriptors; the clone3 starter replaced ``Popen`` but not that step.
    Python builds the slot's control and events channels with
    ``socket.socketpair()``, which is ``O_CLOEXEC``, so the descriptors
    ``_close_fds_except`` deliberately kept were still closed by ``execvpe`` --
    ``sandlock-supervise`` then died with "control fd 21 is not open: Bad file
    descriptor", and the create failed with "route-B slot ... exited before
    answering on control fd".
    """
    reader, writer = os.pipe()
    try:
        assert os.get_inheritable(writer) is False

        _polls, executed, exits = _drive_the_child_half(
            monkeypatch, pass_fds=(writer,)
        )

        assert exits == [9]
        assert executed == [
            (
                "/wheels/sandlock/bin/sandlock-supervise",
                ["/wheels/sandlock/bin/sandlock-supervise"],
                {writer: True},
            )
        ]
    finally:
        os.close(reader)
        os.close(writer)


# ------------------------------------------------------------- the report itself


def _report(handler, **overrides):
    options = dict(
        control_plane_url=CONTROL_PLANE_URL,
        node_id=NODE_ID,
        internal_key="internal-key",
        timeout_s=5.0,
        transport=httpx.MockTransport(handler),
    )
    options.update(overrides)
    return request_identity(CHILD_PID, SANDBOX_ID, **options)


def test_the_report_carries_no_uid_and_goes_to_the_control_plane() -> None:
    """⑤ The message is ``{sandbox_id, pid}``: the worker names no identity."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"nodeID": NODE_ID, "uid": 20007})

    answer = _report(handler)
    assert answer == {"nodeID": NODE_ID, "uid": 20007}
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == (
        f"{CONTROL_PLANE_URL}/internal/nodes/{NODE_ID}/slot-identity"
    )
    assert request.headers["X-Internal-Key"] == "internal-key"
    assert json.loads(request.content) == {
        "sandbox_id": SANDBOX_ID,
        "pid": CHILD_PID,
    }


def test_request_identity_has_no_uid_in_its_signature() -> None:
    """The brief's shape rule, pinned structurally as well as on the wire."""
    signature = inspect.signature(request_identity)
    assert list(signature.parameters)[:2] == ["pid", "sandbox_id"]
    assert not any(
        "uid" in name.lower() for name in signature.parameters
    ), list(signature.parameters)


def test_a_refused_report_is_fail_closed_and_named() -> None:
    """The control plane's own refusal reaches the caller verbatim."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "code": 503,
                "message": (
                    f"node {NODE_ID} has reported no pid namespace identity: "
                    "refusing to instruct the agent without it"
                ),
            },
        )

    with pytest.raises(PrivHelperError) as excinfo:
        _report(handler)
    assert str(excinfo.value) == (
        "the control plane refused the slot-identity report for sandbox "
        f"{SANDBOX_ID} (HTTP 503): node {NODE_ID} has reported no pid namespace "
        "identity: refusing to instruct the agent without it"
    )


def test_an_unreachable_control_plane_is_a_named_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(PrivHelperError) as excinfo:
        _report(handler)
    assert str(excinfo.value) == (
        f"the control plane is unreachable for the slot-identity report of "
        f"sandbox {SANDBOX_ID}: connection refused"
    )


def test_the_reporter_is_built_from_the_workers_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real source of "where is my control plane, who am I".

    ``envd_service.config.Settings`` has no field for either, so the environment
    (``E2B_CONTROL_PLANE_URL`` / ``E2B_NODE_ID`` -- the same two the node agent
    registers with) is what a worker actually reads. An embedder may override
    both, and an explicit ``""`` means "not wired" rather than "fall back".
    """
    monkeypatch.delenv("E2B_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("E2B_NODE_ID", raising=False)
    assert wi.build_identity_reporter(_settings()) is None
    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", CONTROL_PLANE_URL)
    assert wi.build_identity_reporter(_settings()) is None
    monkeypatch.setenv("E2B_NODE_ID", NODE_ID)
    assert callable(wi.build_identity_reporter(_settings()))

    assert (
        wi.build_identity_reporter(
            _settings(), control_plane_url="", node_id=NODE_ID
        )
        is None
    )
    assert (
        wi.build_identity_reporter(
            _settings(), control_plane_url=CONTROL_PLANE_URL, node_id=""
        )
        is None
    )


def test_the_workers_pid_namespace_is_read_from_proc(tmp_path: Path) -> None:
    """The value the CP stores and the agent matches on: ``pid:[N]`` exactly."""
    ns_dir = tmp_path / "self" / "ns"
    ns_dir.mkdir(parents=True)
    os.symlink(PID_NAMESPACE, ns_dir / "pid")
    assert wi.worker_pid_namespace(proc_root=tmp_path) == PID_NAMESPACE
    assert wi.worker_pid_namespace(proc_root=tmp_path / "nope") is None


# ------------------------------------------- the two channels, at the source level


def test_the_worker_has_neither_the_agents_address_nor_its_token() -> None:
    """⑥/⑦ The second and third checkpoints of "only two channels exist".

    The worker's own settings surface has no agent knobs at all, and nothing
    under ``envd_service/`` -- the package the worker image ships -- mentions the
    agent's URL, its token variable, or even the port it listens on. A worker
    that cannot *name* the agent cannot dial it, which is what makes slice B's
    connection-layer refusal (the NetworkPolicy) a second line rather than the
    only one.

    What this does **not** cover, and slice B owns: the real connection refusal
    between the two containers on a live node, and the same check against the
    worker *manifests* (``tests/unit/test_c3_internal_api_shape.py`` pins the
    token half of that today).
    """
    assert not [
        name for name in vars(Settings()) if "c3_agent" in name.lower()
    ]
    offender = []
    for path in (REPO_ROOT / "envd_service").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if (
            "E2B_C3_AGENT_URL" in text
            or "E2B_C3_AGENT_TOKEN" in text
            or "49985" in text
        ):
            offender.append(str(path.relative_to(REPO_ROOT)))
    assert offender == []


# ------------------------------------------------------------ the pool's wiring


def test_the_register_and_heartbeat_payloads_carry_the_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register *and* heartbeat: the identity is re-sent, never pinned once."""
    from envd_service import agent as node_agent

    monkeypatch.setattr(
        node_agent, "worker_pid_namespace", lambda *a, **k: PID_NAMESPACE
    )
    registered = node_agent._register_payload(Settings(), "worker-1")
    assert registered["pidNamespace"] == PID_NAMESPACE
    heartbeat = node_agent._heartbeat_usage_payload(
        Settings(), pid_namespace=PID_NAMESPACE
    )
    assert heartbeat["pidNamespace"] == PID_NAMESPACE


def test_a_host_with_no_proc_ns_pid_reports_no_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent is allowed here and refused downstream -- never silently matched."""
    from envd_service import agent as node_agent

    monkeypatch.setattr(node_agent, "worker_pid_namespace", lambda *a, **k: None)
    assert "pidNamespace" not in node_agent._register_payload(Settings(), "w")
    assert "pidNamespace" not in node_agent._heartbeat_usage_payload(Settings())


def _pool(tmp_path: Path, **overrides) -> tuple[W1SlotPool, list, list, list]:
    order: list = []
    spawned: list[FakeProcess] = []
    reports: list = []

    def _spawn(**kwargs):
        process = FakeProcess()
        spawned.append(process)
        return process

    def _factory(handle):
        return FakeChannel(handle, order, {"stats": {"launched": True, "pid": 7}})

    def _reporter(sandbox_id: str, pid: int):
        order.append("report")
        reports.append((sandbox_id, pid))
        return {"status": "ok"}

    options = dict(
        uid_start=20000,
        size=2,
        tmp_root=tmp_path / "slots",
        supervise_bin=tmp_path / "sandlock-supervise",
        spawner=_spawn,
        channel_factory=_factory,
        socket_timeout_s=2.0,
        slot_identity="agent-grant",
        identity_reporter=_reporter,
    )
    options.update(overrides)
    return W1SlotPool(**options), spawned, order, reports


def test_the_childs_container_pid_is_reported_before_the_readiness_probe(
    tmp_path: Path,
) -> None:
    """The report is fire-and-forget, but it must not arrive after the wait.

    The child is polling ``setresuid`` while the worker waits for the slot's
    channel: if the report had not been sent by then, the wait would be waiting
    for an identity that nobody has granted yet.
    """
    pool, spawned, order, reports = _pool(tmp_path)
    handle = pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)

    assert reports == [(SANDBOX_ID, CHILD_PID)]
    assert order == ["report", "stats"]
    assert handle.uid == 20001
    assert spawned[0].killed == 0


def test_a_report_the_control_plane_refuses_kills_the_child_and_refuses(
    tmp_path: Path,
) -> None:
    """Fail closed: an ungrantable child must not be left behind to poll."""

    def _reporter(sandbox_id: str, pid: int):
        if sandbox_id == SANDBOX_ID:
            raise PrivHelperError(
                "the control plane refused the slot-identity report for "
                f"sandbox {sandbox_id} (HTTP 503): nope"
            )
        return {"status": "ok"}

    pool, spawned, _order, _reports = _pool(tmp_path, identity_reporter=_reporter)
    with pytest.raises(PrivHelperError) as excinfo:
        pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)
    assert str(excinfo.value) == (
        f"the control plane refused the slot-identity report for sandbox "
        f"{SANDBOX_ID} (HTTP 503): nope"
    )
    assert spawned[0].killed == 1
    # The uid came back with the refusal: W1 recycles it, never leaks it.
    assert pool.acquired_uid(SANDBOX_ID) is None
    assert pool.acquire_sync("sbx_after", {"ceiling": {}}, uid=20001).uid == 20001


def test_the_agent_grant_pool_starts_the_child_with_the_unshare_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode picks the starter: no broker, no ``setpriv``, no root.

    ``spawner`` is left unset here on purpose -- that is the production shape
    for a worker with no privileged broker, and what it must reach for is the
    unprivileged helper, with the same slot arguments the privileged form
    passes.
    """
    seen: dict = {}

    def _fake_starter(supervise_bin, **kwargs):
        seen.update(kwargs, supervise_bin=supervise_bin)
        return FakeProcess()

    monkeypatch.setattr(rb, "_spawn_slot_identity", _fake_starter)
    pool, _spawned, order, reports = _pool(tmp_path, spawner=None)
    handle = pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)

    assert seen["uid"] == 20001
    assert seen["supervise_bin"] == tmp_path / "sandlock-supervise"
    assert seen["name"] == f"rb-{SANDBOX_ID}"
    assert str(seen["policy_path"]).endswith("policy.json")
    assert str(seen["program_path"]).endswith("program.json")
    assert seen["worker_uid"] == os.geteuid()
    assert seen["control_fd"] is not None
    assert reports == [(SANDBOX_ID, CHILD_PID)]
    assert order == ["report", "stats"]
    assert handle.process.pid == CHILD_PID


def test_the_child_takes_both_halves_of_its_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setresgid(X)`` then ``setresuid(X)`` -- and never ``setgroups``.

    The gid half is load-bearing on this path and has no witness anywhere else:
    a slot's documents are ``owner=<worker>, group=X, mode 0440``
    (``maint.c``'s ``--worker`` form) and ``sandlock-supervise`` opens
    ``policy.json`` before it does anything else, so a child that took only the
    uid half starts, cannot read its policy, and kills the create with "policy
    read failed: Permission denied". Measured on the compose multinode stack
    2026-09-29 through a real grant: ``Uid=10001 Gid=65534`` and not one of the
    ``policy-<uid>.json`` probes readable; with the gid set, exactly the one
    whose group is the slot's uid opens.

    ``setgroups`` is pinned **absent** for the same reason it is absent in the
    code: ``as_uid`` writes ``deny`` into ``/proc/<pid>/setgroups`` (it must, to
    write the gid map unprivileged), and the kernel refuses ``setgroups`` for
    the life of that namespace -- so a version that called it would hang on
    EPERM and never reach the exec.

    ``setresuid`` failing on the first attempt covers the other half of the
    shape: ``as_uid`` writes ``uid_map`` before ``gid_map``, so a poll that sees
    only the first must retry the pair rather than stop half-done.
    """
    calls: list[tuple[str, tuple[int, int, int]]] = []
    attempts = {"uid": 0}

    def _record(name: str):
        def _call(real, want, extra) -> None:
            calls.append((name, (real, want, extra)))

        return _call

    # ``raising=False``: ``setresgid``/``setresuid`` are Linux-only and this
    # lane also runs on the macOS host -- what is under test is the *pair and
    # the order*, not this platform's ``os``.
    monkeypatch.setattr(
        si.os, "setresgid", _record("setresgid"), raising=False
    )

    def _setresuid(real: int, want: int, extra: int) -> None:
        calls.append(("setresuid", (real, want, extra)))
        attempts["uid"] += 1
        if attempts["uid"] == 1:
            raise OSError(1, "not granted yet")

    monkeypatch.setattr(si.os, "setresuid", _setresuid, raising=False)
    monkeypatch.setattr(
        si.os,
        "setgroups",
        lambda *_args: pytest.fail("the child must not call setgroups (deny)"),
        raising=False,
    )

    assert si._await_identity(20001, deadline_s=1.0, interval_s=0.0) is True
    assert calls == [
        ("setresgid", (20001, 20001, 20001)),
        ("setresuid", (20001, 20001, 20001)),
        ("setresgid", (20001, 20001, 20001)),
        ("setresuid", (20001, 20001, 20001)),
    ]
