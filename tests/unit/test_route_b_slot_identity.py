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
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import envd_service.route_b as rb
import envd_service.slot_identity as si
import envd_service.worker_identity as wi
from envd_service.config import Settings
from envd_service.priv_helpers import PrivHelperError, request_identity
from envd_service.route_b import RouteBConfig, W1SlotPool

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_URL = "http://control-plane:3000"
NODE_ID = "worker-1"
SANDBOX_ID = "sbx_slot_identity"
CHILD_PID = 4242
PID_NAMESPACE = "pid:[4026532458]"


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
        route_b="on",
        route_b_slots=0,
        uid_pool_start=20000,
        uid_pool_size=8,
        route_b_tmp_root="/tmp/c3-slot-identity-test",
        route_b_transport="fd",
        route_b_verb_timeout_s=15.0,
        slot_identity="agent-grant",
        control_plane_url=CONTROL_PLANE_URL,
        node_id=NODE_ID,
        internal_api_key="internal-key",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# ----------------------------------------------------------- the worker's mode


def test_the_slot_identity_mode_defaults_to_the_spawn_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker path stays the default until Task 4/7 retire it."""
    monkeypatch.delenv("E2B_SLOT_IDENTITY", raising=False)
    assert Settings().slot_identity == "spawn"
    bare = _settings()
    del bare.slot_identity  # an embedder's settings object, not the worker's
    assert RouteBConfig.from_settings(bare).slot_identity == "spawn"
    monkeypatch.setenv("E2B_SLOT_IDENTITY", "agent-grant")
    assert RouteBConfig.from_settings(bare).slot_identity == "agent-grant"
    # The worker's own resolved setting wins over the environment.
    assert RouteBConfig.from_settings(_settings(slot_identity="spawn")).slot_identity == "spawn"
    monkeypatch.setenv("E2B_SLOT_IDENTITY", "something-else")
    with pytest.raises(PrivHelperError) as excinfo:
        RouteBConfig.from_settings(bare)
    assert str(excinfo.value) == (
        "E2B_SLOT_IDENTITY must be 'spawn' or 'agent-grant' (got "
        "'something-else')"
    )


def test_agent_grant_needs_a_reporter_rather_than_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the mode: no root, no broker -- only the CP."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    assert RouteBConfig(slot_identity="spawn").privileged_starter is False
    assert (
        RouteBConfig(slot_identity="agent-grant", identity_reporter=lambda *a: {})
        .privileged_starter
        is True
    )
    assert (
        RouteBConfig(slot_identity="agent-grant").privileged_starter is False
    ), "without a reporter the child could never be granted an identity"


def test_agent_grant_never_hands_the_pool_the_broker_spawner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker is the *old* starter; on this path it must not be used.

    ``RouteBConfig.from_settings`` wires ``helpers.slot_spawner`` when the worker
    has the file-capability brokers. That spawner performs the setuid itself, so
    on ``agent-grant`` it would put the privileged step back in front of the
    path C3 is replacing -- and the child it starts never polls for a grant.
    """
    from envd_service import priv_helpers

    monkeypatch.setattr(
        priv_helpers, "active_helpers", lambda: SimpleNamespace(
            slot_spawner=lambda **kw: FakeProcess()
        )
    )
    assert RouteBConfig.from_settings(_settings(slot_identity="spawn")).spawner is not None
    agent_grant = RouteBConfig.from_settings(
        _settings(slot_identity="agent-grant", control_plane_url=CONTROL_PLANE_URL)
    )
    assert agent_grant.spawner is None
    assert agent_grant.identity_reporter is not None


def test_the_child_is_started_without_any_privileged_helper() -> None:
    """The child's argv is our own unshare-and-poll helper, nothing else.

    ``e2b-slot-spawn`` (the broker) and ``setpriv`` are what the *old* path
    needed; this one must not reach for either -- the worker is unprivileged and
    the identity comes from the agent.
    """
    argv = si.child_argv(
        uid=20001,
        supervise_argv=[
            "/wheels/sandlock/bin/sandlock-supervise",
            "--policy",
            "/tmp/policy.json",
            "--uid",
            "20001",
            "--control-fd",
            "7",
        ],
    )
    assert argv[:3] == [sys.executable, "-m", "envd_service.slot_identity"]
    assert argv[3:6] == ["--uid", "20001", "--"]
    assert argv[6] == "/wheels/sandlock/bin/sandlock-supervise"
    assert "setpriv" not in argv
    assert not any("e2b-slot-spawn" in arg for arg in argv)


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


def test_the_reporter_is_only_built_when_the_worker_knows_where_the_cp_is() -> None:
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
    reporter = wi.build_identity_reporter(
        _settings(),
        control_plane_url=CONTROL_PLANE_URL,
        node_id=NODE_ID,
    )
    assert callable(reporter)


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
    under ``envd_service/`` -- the package the worker image ships -- mentions
    the agent's URL or token variable. A worker that cannot name them cannot
    dial them, which is what makes the connection-layer refusal in slice B a
    *second* line rather than the only one.
    """
    assert not [
        name for name in vars(Settings()) if "c3_agent" in name.lower()
    ]
    offender = []
    for path in (REPO_ROOT / "envd_service").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "E2B_C3_AGENT_URL" in text or "E2B_C3_AGENT_TOKEN" in text:
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


def test_the_spawn_fallback_reports_nothing(tmp_path: Path) -> None:
    """The default mode is untouched: no reporter, no CP round trip."""
    pool, _spawned, order, reports = _pool(
        tmp_path, slot_identity="spawn", identity_reporter=None
    )
    pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)
    assert reports == []
    assert order == ["stats"]


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
