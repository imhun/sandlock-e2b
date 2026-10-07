"""N83 phase 1: the per-sandbox cgroup wired into the route-B slot lifecycle.

The switch is ``E2B_SANDBOX_CGROUP`` (``off`` by default). With it off, nothing
here -- the pool's acquire path, the retire path, the worker's startup lane --
may touch a cgroup at all, which is what keeps the shipped default
byte-identical to the pre-N83 worker. With it ``required``, the rules that make
the cgroup a *quota* rather than a decoration are pinned:

* the child is placed (``attach``) **after** it is forked and **before** its
  identity is reported -- the child cannot ``exec`` until its identity lands
  (``identity_grant`` polls ``setresuid``) and ``fork`` can only happen after
  ``exec``, so everything the sandbox ever forks is inside the cgroup (the
  TOCTOU guarantee, plan section 4);
* a refusal anywhere on that path fails the create **by name** and leaves
  nothing behind -- no half slot, no reported identity, and never "run without
  a quota";
* a sandbox that would run **in-process** (route B declined: no host uid, no
  reporter, ``E2B_ROUTE_B=off``) is refused by name as well, because the
  in-process mediator has no cgroup at all (plan Review Focus 4).

The startup lane (delegation handshake, then the worker's own self-check) is
covered here too: it is retried in the background and must never crash the
worker -- a control plane that is briefly away is not a reason to crash-loop a
node -- while creates keep being refused by name until it lands.

The cgroup itself is faked throughout: Task 5's module is the real thing, and
``tests/unit/test_sandbox_cgroup.py`` is its evidence. What this file pins is
the *wiring*: who is called, in what order, with which numbers, and what
happens when the answer is no.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import envd_service.agent as node_agent
import envd_service.executors.factory as factory_mod
import envd_service.own_identity as rb
from envd_service.config import Settings
from envd_service.executors.local import LocalExecutor
from envd_service.priv_helpers import PrivHelperError
from envd_service.own_identity import OwnIdentityConfig, W1SlotPool
from envd_service.runtime.sandbox_cgroup import (
    CgroupRefusal,
    SandboxCeiling,
    SandboxCgroups,
)

SANDBOX_ID = "sbx_cgroup"
CHILD_PID = 4242
#: The declared (unclamped) share this sandbox asked for: two cores. The pool
#: must hand *this* to ``attach``, not the fork policy's ``min(100, ...)``.
DECLARED_PERCENT = 200
#: N83 phase 2 (Task 3): the other two declared sizes on the same record. They
#: are what ``attach`` writes as ``memory.high``/``memory.max`` and ``pids.max``
#: -- the record is where they come from, so the pool carries them verbatim.
DECLARED_MEMORY_MB = 2048
DECLARED_MAX_PROCESSES = 512
#: The per-sandbox ceiling the **control plane** handed down (N83 phase 2 / R17;
#: it is the control plane's own ``E2B_MAX_SANDBOX_*``): the number the module's
#: second gate compares a declared size against.
POLICY_CEILING = SandboxCeiling(cpu_percent=400, memory_mb=4096, processes=1024)
CONTROL_PLANE_URL = "http://control-plane:3000"
NODE_ID = "worker-1"


# ------------------------------------------------------------------- doubles


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


def _channel_factory(order: list):
    def _factory(handle):
        return FakeChannel(handle, order, {"stats": {"launched": True, "pid": 7}})

    return _factory


class FakeCgroups:
    """A stand-in for Task 5's ``SandboxCgroups`` that records every call.

    ``attach_error`` / ``release_error`` make one of the two refuse, which is
    how the fail-closed half of the wiring is exercised without a real
    cgroupfs.
    """

    def __init__(
        self,
        order: list | None = None,
        *,
        attach_error: BaseException | None = None,
        release_error: BaseException | None = None,
    ) -> None:
        self._order = order if order is not None else []
        self._attach_error = attach_error
        self._release_error = release_error
        self.attached: list[dict] = []
        self.released: list[str] = []

    def attach(
        self,
        *,
        sandbox_id: str,
        pid: int,
        cpu_percent: int,
        memory_mb: int | None,
        max_processes: int | None,
    ) -> str:
        self.attached.append(
            {
                "sandbox_id": sandbox_id,
                "pid": pid,
                "cpu_percent": cpu_percent,
                "memory_mb": memory_mb,
                "max_processes": max_processes,
            }
        )
        self._order.append("attach")
        if self._attach_error is not None:
            raise self._attach_error
        return f"/pod-cgroup/sbx_{sandbox_id}"

    def release(self, *, sandbox_id: str) -> bool:
        self.released.append(sandbox_id)
        if self._release_error is not None:
            raise self._release_error
        return True


def _settings(**overrides) -> SimpleNamespace:
    values = dict(
        own_identity="on",
        max_slots=0,
        uid_pool_start=20000,
        uid_pool_size=8,
        slot_tmp_root="/tmp/n83-cgroup-wiring-test",
        slot_transport="fd",
        slot_verb_timeout_s=15.0,
        identity_grant="agent-grant",
        control_plane_url=CONTROL_PLANE_URL,
        node_id=NODE_ID,
        internal_api_key="internal-key",
        identity_grant_report_timeout_s=10.0,
        sandbox_cgroup="off",
        cgroup_mount="/pod-cgroup",
        cgroup_delegate_wait_s=30.0,
        default_cpu_percent=100,
        default_memory_mb=512,
        default_max_processes=64,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _pool(
    tmp_path: Path,
    order: list,
    *,
    sandbox_cgroups=None,
    reports: list | None = None,
    **overrides,
) -> tuple[W1SlotPool, list, list]:
    """A pool whose child, channel and identity report are all fakes."""
    spawned: list[FakeProcess] = []
    reported = reports if reports is not None else []

    def _spawn(**kwargs):
        process = FakeProcess()
        spawned.append(process)
        return process

    def _reporter(sandbox_id: str, pid: int):
        order.append("report")
        reported.append((sandbox_id, pid))
        return {"status": "ok"}

    options = dict(
        uid_start=20000,
        size=2,
        tmp_root=tmp_path / "slots",
        supervise_bin=tmp_path / "sandlock-supervise",
        spawner=_spawn,
        channel_factory=_channel_factory(order),
        socket_timeout_s=2.0,
        identity_grant="agent-grant",
        identity_reporter=_reporter,
        sandbox_cgroups=sandbox_cgroups,
    )
    options.update(overrides)
    return W1SlotPool(**options), spawned, reported


@pytest.fixture(autouse=True)
def _pools_reset(monkeypatch, tmp_path):
    """Keep the process-wide fleet out of the way of the production-path cases."""
    monkeypatch.setattr(
        rb, "default_supervise_bin", lambda: tmp_path / "sandlock-supervise"
    )
    rb.reset_slot_pools()
    rb.reset_sandbox_cgroups()
    # N83 phase 2 / R17: the hand-down is process-wide too -- clear it so one
    # case's adopted ceiling cannot decide the next case's gate.
    monkeypatch.setattr(node_agent, "_HANDED_DOWN_CEILING", None)
    yield
    rb.reset_slot_pools()
    rb.reset_sandbox_cgroups()


# ------------------------------------------------------- the switch: resolve


def test_the_settings_default_to_an_off_lane(monkeypatch) -> None:
    """R-A: ``off``, ``/pod-cgroup``, 30 s -- the shipped defaults."""
    for name in (
        "E2B_SANDBOX_CGROUP",
        "E2B_CGROUP_MOUNT",
        "E2B_CGROUP_DELEGATE_WAIT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.sandbox_cgroup == "off"
    assert settings.cgroup_mount == Path("/pod-cgroup")
    assert settings.cgroup_delegate_wait_s == 30.0


def test_the_settings_read_the_documented_switch_names(monkeypatch) -> None:
    """The manifests set these three, so the spellings are pinned here."""
    monkeypatch.setenv("E2B_SANDBOX_CGROUP", "Required")
    monkeypatch.setenv("E2B_CGROUP_MOUNT", "/host-cgroup")
    monkeypatch.setenv("E2B_CGROUP_DELEGATE_WAIT_S", "5")
    settings = Settings()
    assert settings.sandbox_cgroup == "required"
    assert settings.cgroup_mount == Path("/host-cgroup")
    assert settings.cgroup_delegate_wait_s == 5.0


def test_the_switch_resolves_to_no_handle_at_all_when_off() -> None:
    """``off`` must not construct the module -- not even to ask it anything."""
    assert rb.sandbox_cgroups_for(_settings(sandbox_cgroup="off")) is None


def test_an_unknown_switch_value_is_refused_by_name() -> None:
    """A typo must not read as "off": that would be a quota-less fleet."""
    expected = (
        "E2B_SANDBOX_CGROUP must be 'off' or 'required': 'requried' would "
        "leave every sandbox running without a per-sandbox quota while "
        "looking configured -- delete the line (the default is off), or set "
        "it to 'required'"
    )
    settings = _settings(sandbox_cgroup="requried")
    with pytest.raises(ValueError) as excinfo:
        rb.sandbox_cgroups_for(settings)
    assert str(excinfo.value) == expected
    with pytest.raises(ValueError) as excinfo:
        OwnIdentityConfig.from_settings(settings)
    assert str(excinfo.value) == expected


def test_required_with_no_handle_is_refused_by_name() -> None:
    """The switch and its handle are one invariant, checked where the mode is.

    Fix round 1, review Finding 1. ``from_settings`` resolves both together, so
    the fail-open below is unreachable through the factory -- which is why the
    check belongs here: ``OwnIdentityConfig`` is built by hand in tests and by
    embedders, and a live pool with ``required`` and no handle would attach
    nothing at all (``_attach_cgroup`` returns early on ``None``), running every
    sandbox without a quota while the switch says otherwise.
    """
    with pytest.raises(ValueError) as excinfo:
        OwnIdentityConfig(sandbox_cgroup="required")
    assert str(excinfo.value) == (
        "E2B_SANDBOX_CGROUP=required needs a SandboxCgroups handle: without "
        "one the pool attaches nothing and every sandbox runs without its "
        "per-sandbox quota, which is the fail-open this switch exists to "
        "prevent -- build the handle with sandbox_cgroups_for(settings), or "
        "set E2B_SANDBOX_CGROUP=off"
    )
    # The other direction stays legal: a handle with the default ``off`` mode is
    # a caller's explicit hand-off, not the fail-open shape above.
    assert OwnIdentityConfig(sandbox_cgroups=FakeCgroups()).sandbox_cgroup == "off"
    # ...and the production shape is untouched: ``from_settings`` resolves the
    # switch and the handle together, so ``required`` still builds a fleet there.
    resolved = OwnIdentityConfig.from_settings(_settings(sandbox_cgroup="required"))
    assert resolved.sandbox_cgroup == "required"
    assert resolved.sandbox_cgroups is not None


def test_the_required_switch_builds_one_handle_for_the_whole_process(
    monkeypatch,
) -> None:
    """One process, one handle: only the object that ran ``setup`` can attach."""
    built: list[dict] = []

    class Recording:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(rb, "SandboxCgroups", Recording)
    monkeypatch.setattr(node_agent, "_HANDED_DOWN_CEILING", POLICY_CEILING)
    settings = _settings(sandbox_cgroup="required", cgroup_mount="/pod-cgroup")
    first = rb.sandbox_cgroups_for(settings)
    assert first is rb.sandbox_cgroups_for(settings)
    assert built == [
        {
            "mount": Path("/pod-cgroup"),
            "worker_uid": os.geteuid(),
            "container_token": None,
            # N83 phase 2 (R3/R17): the handle is built with the per-sandbox
            # ceiling the control plane handed down, so ``attach`` can refuse a
            # declared size above it *by name*. It is read next to the handle
            # because both are one process-wide fact.
            "policy_ceiling": POLICY_CEILING,
        }
    ]


def test_the_compose_lane_hands_the_worker_its_container_id(monkeypatch) -> None:
    """R-E: the compose mount is the whole VM tree -- narrow it by our id."""
    built: list[dict] = []

    class Recording:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(rb, "SandboxCgroups", Recording)
    monkeypatch.setattr(rb, "reported_container_id", lambda: "3f2a1b0c9d8e")
    rb.sandbox_cgroups_for(_settings(sandbox_cgroup="required"))
    assert built[0]["container_token"] == "3f2a1b0c9d8e"


def test_the_k8s_lane_hands_no_container_token(monkeypatch) -> None:
    """The k8s mount root is already this pod's cgroup: one level, no walk."""
    built: list[dict] = []

    class Recording:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(rb, "SandboxCgroups", Recording)
    monkeypatch.setattr(rb, "reported_container_id", lambda: None)
    rb.sandbox_cgroups_for(_settings(sandbox_cgroup="required"))
    assert built[0]["container_token"] is None


# ------------------------------------------------ the switch: off is a no-op


def test_with_the_switch_off_nothing_touches_a_cgroup(
    monkeypatch, tmp_path
) -> None:
    """The shipped default: a full acquire, and no cgroup object anywhere."""
    constructed: list = []

    class Recording:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    monkeypatch.setattr(rb, "SandboxCgroups", Recording)
    monkeypatch.setattr(rb, "_spawn_slot_child", lambda *a, **kw: FakeProcess())
    order: list = []
    config = OwnIdentityConfig.from_settings(_settings(sandbox_cgroup="off"))
    assert config.sandbox_cgroups is None
    config.identity_reporter = lambda sandbox_id, pid: {}
    pool = rb.slot_pool_for(config, channel_factory=_channel_factory(order))
    handle = pool.acquire_sync(
        SANDBOX_ID, {"ceiling": {}}, uid=20001, cpu_percent=DECLARED_PERCENT
    )

    assert constructed == []
    assert handle.uid == 20001
    # Byte-identical to today's lifecycle: no attach, nothing new at all.
    assert order == ["stats"]


def test_the_pool_never_releases_a_cgroup_it_was_not_given(tmp_path) -> None:
    """``release`` on the off switch is the same no-op as ``attach``."""
    order: list = []
    pool, _spawned, _reports = _pool(tmp_path, order, sandbox_cgroups=None)
    pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)
    pool.release_sync(SANDBOX_ID)
    assert order == ["report", "stats", "shutdown"]


# ------------------------------------------------------ the acquire ordering


def test_the_child_is_placed_before_its_identity_is_reported(tmp_path) -> None:
    """The TOCTOU guarantee: attach before the report, with the child's pid."""
    order: list = []
    fake = FakeCgroups(order)
    pool, spawned, reports = _pool(tmp_path, order, sandbox_cgroups=fake)
    handle = pool.acquire_sync(
        SANDBOX_ID, {"ceiling": {}}, uid=20001, cpu_percent=DECLARED_PERCENT
    )

    assert order == ["attach", "report", "stats"]
    assert fake.attached == [
        {
            "sandbox_id": SANDBOX_ID,
            "pid": CHILD_PID,
            "cpu_percent": DECLARED_PERCENT,
            "memory_mb": None,
            "max_processes": None,
        }
    ]
    assert reports == [(SANDBOX_ID, CHILD_PID)]
    assert spawned[0].killed == 0
    assert handle.uid == 20001


def test_the_declared_percent_reaches_attach_unchanged(tmp_path) -> None:
    """Not ``min(100, cpu_percent)``: the cgroup is the declared share."""
    order: list = []
    fake = FakeCgroups(order)
    pool, _spawned, _reports = _pool(tmp_path, order, sandbox_cgroups=fake)
    pool.acquire_sync(
        SANDBOX_ID,
        {"ceiling": {}},
        uid=20001,
        cpu_percent=400,
        memory_mb=2048,
        max_processes=256,
    )
    pool.acquire_sync(
        "sbx_two", {"ceiling": {}}, uid=20000, cpu_percent=DECLARED_PERCENT
    )

    assert [call["cpu_percent"] for call in fake.attached] == [400, DECLARED_PERCENT]


def test_the_declared_memory_and_pids_reach_attach_unchanged(tmp_path) -> None:
    """N83 phase 2 (Task 3): the record's sizes are what the box is capped at.

    ``memory.high``/``memory.max`` get the declared MiB and ``pids.max`` the
    declared task budget, verbatim -- no clamping here, and no default invented
    by the pool.
    """
    order: list = []
    fake = FakeCgroups(order)
    pool, _spawned, _reports = _pool(tmp_path, order, sandbox_cgroups=fake)
    pool.acquire_sync(
        SANDBOX_ID,
        {"ceiling": {}},
        uid=20001,
        cpu_percent=DECLARED_PERCENT,
        memory_mb=DECLARED_MEMORY_MB,
        max_processes=DECLARED_MAX_PROCESSES,
    )

    assert fake.attached == [
        {
            "sandbox_id": SANDBOX_ID,
            "pid": CHILD_PID,
            "cpu_percent": DECLARED_PERCENT,
            "memory_mb": DECLARED_MEMORY_MB,
            "max_processes": DECLARED_MAX_PROCESSES,
        }
    ]


def test_a_caller_that_declares_nothing_hands_attach_a_none(tmp_path) -> None:
    """``None`` is "the caller does not know", and the pool invents nothing.

    ``cpu_percent`` has a documented floor (100 %, pinned since phase 1); the
    two phase-2 dimensions have none, so the pool passes the honest ``None``
    through and the module resolves it against the worker's ceiling -- never
    against a number this layer made up.
    """
    order: list = []
    fake = FakeCgroups(order)
    pool, _spawned, _reports = _pool(tmp_path, order, sandbox_cgroups=fake)
    pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)

    assert fake.attached[0]["cpu_percent"] == 100
    assert fake.attached[0]["memory_mb"] is None
    assert fake.attached[0]["max_processes"] is None


def test_the_handle_is_built_with_the_handed_down_policy_ceiling(
    monkeypatch, tmp_path
) -> None:
    """R3/R17: the second gate compares against the control plane's hand-down.

    Not the kernel read (``kernel_ceiling`` is only the adoption cross-check):
    the policy is the *control plane's* deployment decision, adopted from every
    register/heartbeat answer (``envd_service.agent.adopted_sandbox_ceiling``),
    and the process-wide handle is built with it, so every ``attach`` through
    that handle sees the same number.
    """
    built: list[dict] = []

    class Recording:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(rb, "SandboxCgroups", Recording)
    monkeypatch.setattr(node_agent, "_HANDED_DOWN_CEILING", POLICY_CEILING)
    rb.sandbox_cgroups_for(_settings(sandbox_cgroup="required"))

    assert built[0]["policy_ceiling"] == POLICY_CEILING


def test_a_late_hand_down_updates_the_live_handle_in_place(
    monkeypatch, tmp_path
) -> None:
    """R17: the value can change under a live process, and the handle follows.

    The handle is process-wide and cached -- and it is the object the lane's
    ``setup`` established the delegated parent on -- so a new ceiling is applied
    *to* it. Rebuilding instead would abandon the parent every live
    ``sbx_<id>`` hangs under, which is exactly what the brief's ruling forbids.
    """
    ceiling = SandboxCeiling(cpu_percent=200, memory_mb=2048, processes=256)
    settings = _settings(sandbox_cgroup="required")
    monkeypatch.setattr(node_agent, "_HANDED_DOWN_CEILING", ceiling)
    handle = rb.sandbox_cgroups_for(settings)
    assert handle.policy_ceiling == ceiling

    changed = SandboxCeiling(cpu_percent=100, memory_mb=1024, processes=128)
    assert rb.update_sandbox_cgroup_ceiling(settings, changed) is True

    assert rb.sandbox_cgroups_for(settings) is handle
    assert handle.policy_ceiling == changed


def test_an_off_lane_never_builds_or_updates_a_handle(monkeypatch, tmp_path) -> None:
    """The ``off`` lane stays byte-for-byte today's: no handle, no update."""
    built: list[dict] = []

    class Recording:
        def __init__(self, **kwargs):  # pragma: no cover - must not run
            built.append(kwargs)

    monkeypatch.setattr(rb, "SandboxCgroups", Recording)
    settings = _settings(sandbox_cgroup="off")
    assert rb.sandbox_cgroups_for(settings) is None
    assert rb.update_sandbox_cgroup_ceiling(
        settings, SandboxCeiling(cpu_percent=100, memory_mb=1024, processes=128)
    ) is False
    assert built == []


# ------------------------------------------------------- the fail-closed half


def test_required_and_not_ready_refuses_the_create_by_name(tmp_path) -> None:
    """A lane whose ``setup`` never ran has no parent: refuse, do not place.

    The refusal is Task 5's own ``setup-not-run``: the pool holds the handle,
    the handle holds no delegated parent, and the sentence names exactly that.
    """
    order: list = []
    handle = SandboxCgroups(mount=tmp_path / "pod-cgroup", worker_uid=os.geteuid())
    pool, spawned, reports = _pool(tmp_path, order, sandbox_cgroups=handle)

    with pytest.raises(CgroupRefusal) as excinfo:
        pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001, cpu_percent=200)
    assert str(excinfo.value) == (
        "cgroup-refusal setup-not-run: call setup() before attach() or release()"
    )
    # Nothing half-built: the child is dead, no identity was reported, and the
    # uid went back to the ledger with it.
    assert spawned[0].killed == 1
    assert reports == []
    assert pool.acquired_uid(SANDBOX_ID) is None
    # The uid came back with the refusal (W1 recycles it, never leaks it): the
    # next create reaches the same named refusal rather than "the slot segment
    # is exhausted" or "that uid is outside the segment".
    with pytest.raises(CgroupRefusal) as again:
        pool.acquire_sync("sbx_after", {"ceiling": {}}, uid=20001)
    assert str(again.value) == (
        "cgroup-refusal setup-not-run: call setup() before attach() or release()"
    )
    assert spawned[1].killed == 1


def test_a_refused_attach_kills_the_child_and_reports_nothing(tmp_path) -> None:
    """An ungrantable child must not be left behind polling for an identity."""
    order: list = []
    fake = FakeCgroups(
        order, attach_error=CgroupRefusal("cgroup-refusal cpu-max: nope")
    )
    pool, spawned, reports = _pool(tmp_path, order, sandbox_cgroups=fake)

    with pytest.raises(CgroupRefusal) as excinfo:
        pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)
    assert str(excinfo.value) == "cgroup-refusal cpu-max: nope"
    assert reports == []
    assert order == ["attach"]
    assert spawned[0].killed == 1
    assert pool.acquired_uid(SANDBOX_ID) is None


# ------------------------------------------------------------ the retire path


def test_retire_releases_the_sandbox_cgroup_exactly_once(tmp_path) -> None:
    """W1 recycle ends the generation *and* its cgroup, once per generation."""
    order: list = []
    fake = FakeCgroups(order)
    pool, _spawned, _reports = _pool(tmp_path, order, sandbox_cgroups=fake)
    handle = pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)

    pool.release_sync(SANDBOX_ID)
    assert fake.released == [SANDBOX_ID]
    # A second release is a no-op: the ledger has nothing left to retire.
    pool.release_sync(SANDBOX_ID)
    assert fake.released == [SANDBOX_ID]
    assert pool.slot(SANDBOX_ID) is None
    assert handle.process.returncode == 0


def test_a_refused_release_is_a_named_warning_and_teardown_still_ends(
    tmp_path, caplog
) -> None:
    """Teardown never fails because the cgroup is already gone (or wedged).

    ... and the warning says what actually happens to the leftover (Task 5
    review, fix 3): nothing in this worker sweeps ``sbx_*`` directories, so the
    old "the node's GC to reclaim" was a promise with no implementation behind
    it.
    """
    order: list = []
    fake = FakeCgroups(
        order, release_error=CgroupRefusal("cgroup-refusal release-rmdir: busy")
    )
    pool, _spawned, _reports = _pool(tmp_path, order, sandbox_cgroups=fake)
    pool.acquire_sync(SANDBOX_ID, {"ceiling": {}}, uid=20001)

    # The acquire above warns about the slot documents' mode when the test runs
    # unprivileged (an unrelated, pre-existing warning). Narrow the capture to
    # the retire under test, then demand it in full.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=rb.__name__):
        pool.release_sync(SANDBOX_ID)

    assert fake.released == [SANDBOX_ID]
    assert pool.acquired_uid(SANDBOX_ID) is None
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == rb.__name__
    ] == [
        "route-B slot rb-sbx_cgroup: the cgroup for sandbox sbx_cgroup was "
        "not released (cgroup-refusal release-rmdir: busy); the subtree is left "
        "behind -- nothing in this worker reclaims a sbx_* directory, so a "
        "later create under the same id is the only thing that would touch it"
    ]
    # ...and the uid is immediately leasable again: teardown completed.
    assert pool.acquire_sync("sbx_next", {"ceiling": {}}, uid=20001).uid == 20001


# ------------------------------------------------- the worker's startup lane


def test_the_startup_lane_is_not_started_when_the_switch_is_off(
    monkeypatch,
) -> None:
    """``off`` reaches no further than the resolver: no task, no handshake."""
    calls: list = []
    monkeypatch.setattr(
        node_agent, "request_cgroup_delegate", lambda **kw: calls.append(kw)
    )
    assert node_agent.start_cgroup_lane(_settings(sandbox_cgroup="off")) is None
    assert calls == []


async def test_the_startup_lane_delegates_first_then_self_checks(
    monkeypatch, caplog
) -> None:
    """R-B's order: one delegation request, then ``SandboxCgroups.setup``.

    The answer's ``containerCgroup``/``delegated`` are logged at info level --
    they are the only place an operator can see what the agent handed over.
    """
    order: list = []

    def _delegate(**kwargs):
        order.append("delegate")
        return {
            "op": "delegate-cgroup",
            "containerCgroup": "/kubepods/burstable/podabc/3f2a1b0c9d8e",
            "delegated": [".", "cgroup.procs", "cgroup.subtree_control"],
            "nodeID": NODE_ID,
            "workerAnchor": None,
        }

    class ReadyCgroups:
        def setup(self, *, wait_s: float) -> str:
            order.append("setup")
            return (
                "cgroup ready parent=/pod-cgroup/3f2a1b0c9d8e drained=1 "
                "subtree_control=cpu"
            )

    monkeypatch.setattr(node_agent, "request_cgroup_delegate", _delegate)
    with caplog.at_level(logging.INFO, logger=node_agent.__name__):
        task = node_agent.start_cgroup_lane(
            _settings(sandbox_cgroup="required"),
            sandbox_cgroups=ReadyCgroups(),
        )
        await asyncio.wait_for(task, timeout=5)

    assert order == ["delegate", "setup"]
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == node_agent.__name__
    ] == [
        "cgroup delegation answer (attempt 1): nodeID=worker-1 "
        "containerCgroup=/kubepods/burstable/podabc/3f2a1b0c9d8e "
        "delegated=['.', 'cgroup.procs', 'cgroup.subtree_control'] "
        "workerAnchor=None",
        "cgroup lane ready (attempt 1): cgroup ready "
        "parent=/pod-cgroup/3f2a1b0c9d8e drained=1 subtree_control=cpu",
    ]


async def test_a_failed_attempt_is_retried_and_does_not_crash_the_worker(
    monkeypatch, caplog
) -> None:
    """Neither hop may crash the process: the lane retries, creates keep failing.

    Both halves fail here -- the delegation is away once, the self-check twice --
    and the lane keeps re-running *both* in order until one attempt gets through.
    """
    attempts: list = []
    delegations = {"n": 0}

    def _delegate(**kwargs):
        delegations["n"] += 1
        attempts.append(f"delegate-{delegations['n']}")
        if delegations["n"] == 1:
            raise PrivHelperError(
                "the control plane is unreachable for the cgroup delegation: "
                "connection refused"
            )
        return {"op": "delegate-cgroup", "containerCgroup": "/pod-cgroup/x"}

    class FlakyCgroups:
        def __init__(self) -> None:
            self.setups = 0

        def setup(self, *, wait_s: float) -> str:
            self.setups += 1
            attempts.append(f"setup-{self.setups}")
            if self.setups < 3:
                raise CgroupRefusal("cgroup-refusal delegation-timeout: not yet")
            return "cgroup ready parent=/pod-cgroup/3f2a1b0c9d8e"

    monkeypatch.setattr(node_agent, "request_cgroup_delegate", _delegate)
    monkeypatch.setattr(node_agent, "CGROUP_RETRY_INTERVAL_S", 0.01)
    with caplog.at_level(logging.WARNING, logger=node_agent.__name__):
        task = node_agent.start_cgroup_lane(
            _settings(sandbox_cgroup="required"), sandbox_cgroups=FlakyCgroups()
        )
        await asyncio.wait_for(task, timeout=5)

    assert attempts == [
        "delegate-1",
        "delegate-2",
        "setup-1",
        "delegate-3",
        "setup-2",
        "delegate-4",
        "setup-3",
    ]
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == node_agent.__name__
    ] == [
        "cgroup lane not ready (attempt 1): the control plane is unreachable "
        "for the cgroup delegation: connection refused -- retrying in 0.0s; "
        "sandbox creates are refused by name until it lands "
        "(E2B_SANDBOX_CGROUP=required, N83 phase 1)",
        "cgroup lane not ready (attempt 2): cgroup-refusal "
        "delegation-timeout: not yet -- retrying in 0.0s; sandbox creates are "
        "refused by name until it lands (E2B_SANDBOX_CGROUP=required, "
        "N83 phase 1)",
        "cgroup lane not ready (attempt 3): cgroup-refusal "
        "delegation-timeout: not yet -- retrying in 0.0s; sandbox creates are "
        "refused by name until it lands (E2B_SANDBOX_CGROUP=required, "
        "N83 phase 1)",
    ]


async def test_a_hop_that_never_lands_keeps_retrying_without_raising(
    monkeypatch,
) -> None:
    """The lane is a hard dependency, not a startup gate: it simply keeps going."""
    attempts: list = []

    def _delegate(**kwargs):
        attempts.append(len(attempts) + 1)
        raise PrivHelperError("the control plane is unreachable")

    class NeverReady:
        def setup(self, *, wait_s: float) -> str:
            raise AssertionError("setup must not run while the delegation fails")

    monkeypatch.setattr(node_agent, "request_cgroup_delegate", _delegate)
    monkeypatch.setattr(node_agent, "CGROUP_RETRY_INTERVAL_S", 0.01)
    task = node_agent.start_cgroup_lane(
        _settings(sandbox_cgroup="required"), sandbox_cgroups=NeverReady()
    )
    while len(attempts) < 3:
        await asyncio.sleep(0.01)
        assert not task.done(), "a failing lane must retry instead of finishing"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == [1, 2, 3]


def test_an_unknown_switch_value_is_refused_at_startup(monkeypatch) -> None:
    """A typo is a configuration refusal, not a silent "off"."""
    with pytest.raises(ValueError) as excinfo:
        node_agent.start_cgroup_lane(_settings(sandbox_cgroup="on"))
    assert str(excinfo.value) == (
        "E2B_SANDBOX_CGROUP must be 'off' or 'required': 'on' would leave "
        "every sandbox running without a per-sandbox quota while looking "
        "configured -- delete the line (the default is off), or set it to "
        "'required'"
    )


async def test_the_node_agent_owns_the_startup_lane(monkeypatch) -> None:
    """The lifespan's start/stop pair takes the lane with it."""

    def _unreachable(**kwargs):
        raise PrivHelperError("the control plane is unreachable")

    monkeypatch.setattr(node_agent.NodeAgent, "_loop", _never_ending)
    monkeypatch.setattr(node_agent, "request_cgroup_delegate", _unreachable)
    monkeypatch.setattr(node_agent, "CGROUP_RETRY_INTERVAL_S", 0.01)
    agent = node_agent.NodeAgent(
        settings=_settings(sandbox_cgroup="required"),
        runtime_registry=SimpleNamespace(),
        control_plane_url=CONTROL_PLANE_URL,
        node_address="10.0.0.5",
        node_id=NODE_ID,
    )

    agent.start()
    lane = agent._cgroup_task
    assert lane is not None
    await asyncio.sleep(0.05)
    assert not lane.done(), "a failing lane must retry instead of finishing"
    await agent.stop()
    assert lane.cancelled()
    assert agent._cgroup_task is None


async def _never_ending(*_args, **_kwargs) -> None:
    await asyncio.sleep(3600)


def _delegate_is_unreachable(**_kwargs) -> dict:
    """The startup lane's retry path: the control plane is away for now."""
    raise PrivHelperError("the control plane is unreachable")


async def test_the_off_lane_starts_no_event_sweeper(monkeypatch) -> None:
    """``off`` is byte-identical here too: no lane, so nothing samples.

    N83 phase 2 (Task 5) puts the kernel's per-sandbox event counters on the
    heartbeat. They live inside ``sbx_<id>``, which only exists on a lane that
    built one -- so a worker with the switch off must not even start a task for
    them, exactly like the startup lane above.
    """
    monkeypatch.setattr(node_agent.NodeAgent, "_loop", _never_ending)
    agent = node_agent.NodeAgent(
        settings=_settings(sandbox_cgroup="off"),
        runtime_registry=SimpleNamespace(),
        control_plane_url=CONTROL_PLANE_URL,
        node_address="10.0.0.5",
        node_id=NODE_ID,
    )

    agent.start()

    assert agent._cgroup_task is None
    assert agent._cgroup_events_task is None
    await agent.stop()


async def test_the_required_lane_sweeps_the_event_counters(monkeypatch) -> None:
    """With the lane on, the counters are sampled on their own cadence and kept
    for the heartbeat."""
    monkeypatch.setattr(node_agent.NodeAgent, "_loop", _never_ending)
    monkeypatch.setattr(
        node_agent, "request_cgroup_delegate", _delegate_is_unreachable
    )
    monkeypatch.setattr(node_agent, "CGROUP_RETRY_INTERVAL_S", 0.01)
    events = {"sbx_alpha": {"oom_kill": 1, "oom_group_kill": 0, "pids_max": 0}}
    monkeypatch.setattr(
        node_agent, "sample_sandbox_events", lambda _settings: dict(events)
    )
    agent = node_agent.NodeAgent(
        settings=_settings(sandbox_cgroup="required"),
        runtime_registry=SimpleNamespace(),
        control_plane_url=CONTROL_PLANE_URL,
        node_address="10.0.0.5",
        node_id=NODE_ID,
    )
    agent._cgroup_events_interval_s = 0.01

    agent.start()
    sweeper = agent._cgroup_events_task
    assert sweeper is not None
    await asyncio.sleep(0.05)
    assert agent._cgroup_events == events
    await agent.stop()
    assert sweeper.cancelled()
    assert agent._cgroup_events_task is None


# ------------------------------------------------- the factory's two local paths


def _create_executor(settings):
    """``create_executor`` with the argument set the health tests use."""
    return factory_mod.create_executor(
        settings,
        workspace_dir="/tmp/ws",
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        network=None,
    )


def _expected_local_refusal(mode: str) -> str:
    """The exact fail-closed text, written out rather than imported."""
    return (
        "E2B_SANDBOX_CGROUP=required refuses the LOCAL executor "
        f"(E2B_EXECUTOR={mode}): it applies no sandbox confinement and no "
        "per-sandbox cgroup, so this sandbox would run with no quota. Install "
        "the matching sandlock wheel (E2B_EXECUTOR=auto or sandlock), or set "
        "E2B_SANDBOX_CGROUP=off to accept uncapped sandboxes."
    )


def test_required_refuses_the_explicit_local_executor_by_name() -> None:
    """Final review Important 2, route 1: ``E2B_EXECUTOR=local`` + required.

    ``LocalExecutor`` reads no ``sandbox_cgroup`` at all, so before this guard
    the operator's two switches silently cancelled each other and the sandbox
    ran uncapped. It must refuse **by name** instead.
    """
    with pytest.raises(RuntimeError) as excinfo:
        _create_executor(_settings(executor="local", sandbox_cgroup="required"))

    assert type(excinfo.value) is RuntimeError
    assert str(excinfo.value) == _expected_local_refusal("local")


def test_required_refuses_autos_missing_sandlock_fallback_by_name(monkeypatch) -> None:
    """Final review Important 2, route 2: ``auto`` + absent sandlock + required.

    The documented fallback for a genuinely missing package is
    ``LocalExecutor`` -- fine under the default ``off``, and exactly the
    "mixed-version, no quota, no confinement" shape the switch forbids under
    ``required``. Only a ``ModuleNotFoundError`` naming the top-level package
    reaches this path (a broken install has already refused above).
    """
    missing = ModuleNotFoundError("No module named 'sandlock'", name="sandlock")
    # The dev host is macOS; the probe is Linux-only, so pin it here.
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: missing)

    with pytest.raises(RuntimeError) as excinfo:
        _create_executor(_settings(executor="auto", sandbox_cgroup="required"))

    assert type(excinfo.value) is RuntimeError
    assert str(excinfo.value) == _expected_local_refusal("auto")


def test_off_keeps_both_local_paths_verbatim(monkeypatch) -> None:
    """The default lane is byte-for-byte unchanged by the two guards above."""
    # Route 1: an explicit local executor never even probes sandlock.
    assert isinstance(
        _create_executor(_settings(executor="local", sandbox_cgroup="off")),
        LocalExecutor,
    )
    # Route 2: auto + absent still falls back to local, as documented.
    missing = ModuleNotFoundError("No module named 'sandlock'", name="sandlock")
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: missing)
    assert isinstance(
        _create_executor(_settings(executor="auto", sandbox_cgroup="off")),
        LocalExecutor,
    )


def test_required_does_not_shadow_the_installed_but_broken_refusal(monkeypatch) -> None:
    """The new guard closes the two local paths only, not the sandlock ones.

    An installed-but-unusable package still takes the B1 ``unusable`` refusal
    (never the uncapped fallback, and not the new local message). Pinned here
    because this file's command is the one that runs the N83 wiring.
    """
    broken = ImportError("libsandlock_ffi.so: cannot open shared object file")
    monkeypatch.setattr(factory_mod.sys, "platform", "linux")
    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: broken)

    with pytest.raises(RuntimeError) as excinfo:
        _create_executor(_settings(executor="auto", sandbox_cgroup="required"))

    assert str(excinfo.value) == (
        "E2B_EXECUTOR=auto cannot run: the sandlock package is installed but "
        "unusable (ImportError: libsandlock_ffi.so: cannot open shared object "
        "file); refusing to fall back to the LOCAL executor, which applies no "
        "sandbox confinement. Reinstall the matching sandlock wheel (or "
        "rebuild libsandlock_ffi.so) and restage the worker image."
    )
