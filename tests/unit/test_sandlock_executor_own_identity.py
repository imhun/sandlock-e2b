"""SandlockExecutor on route B: the supervise slot as the exec instance.

The native ``sandlock-supervise`` binary is Linux-only and needs privilege to
start at another uid, so the *fleet* is faked here and the assertions are
about what the executor puts on the wire: which uid is leased, which policy
document the generation gets, how each ``start``/``update_network``/``close``
maps to a verb, and that closed/dead recovery restarts the slot instead of
silently falling back to the in-process mediator. The real two-uid evidence is
``tests/contract/test_own_identity_slot_pool.py``; the executor running end-to-end
against a real slot is ``tests/contract/test_own_identity_executor.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import envd_service.executors.sandlock as sl
import envd_service.own_identity as rb
from envd_service.executors.base import ExecConfig
from envd_service.own_identity import OwnIdentityConfig, SlotDeadError, SlotHandle
from gateway_common.network import NetworkUpdateConflictError

WORKSPACE = "/var/lib/e2b-sandboxes/sbx_route_b/workspace"
ROOTFS = Path("tmp/unit-route-b-rootfs")
HOST_UID = 20007


class FakeSlotProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.stderr = None
        self.killed = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return 0


class FakeChannel:
    """Answers the verbs the executor uses and records every request."""

    def __init__(self, path, token, log, replies, child_output=b""):
        self.path = path
        self.token = token
        self._log = log
        self._replies = replies
        self._child_output = child_output

    def request(self, verb, args=None, fds=()):
        fds = tuple(fds)
        self._log.append((verb, args, fds))
        if verb == "exec" and self._child_output:
            # The child's stdout end is the second fd; write through it the way
            # a running command would, then let the shim's close finish the
            # stream.
            os.write(fds[1], self._child_output)
        reply = self._replies.get(verb, {})
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class FakePool:
    """A W1 fleet that hands out in-memory slots."""

    def __init__(self, child_output=b""):
        self.replies = {
            "exec": {"child_id": 11, "pid": 5150},
            "wait_child": {
                "code": 0,
                "signal": None,
                "killed": False,
                "timed_out": False,
            },
            "update_network": {"stale_child_ids": []},
            "stats": {"launched": True, "pid": 5100},
        }
        self.child_output = child_output
        self.log: list[tuple] = []
        self.acquire_calls: list[dict] = []
        self.retired: list[str] = []
        self.tmp = Path("tmp/unit-route-b-slots")
        self.tmp.mkdir(parents=True, exist_ok=True)

    @property
    def channel_factory(self):
        return self._factory

    def _factory(self, handle):
        assert handle.sock_path is None, "the executor leases fd-handoff slots"
        return FakeChannel(
            f"fd:{id(handle.control_socket)}",
            handle.token,
            self.log,
            self.replies,
            self.child_output,
        )

    def acquire_sync(
        self,
        sandbox_id,
        policy_json,
        program_json=None,
        *,
        uid=None,
        name=None,
        cpu_percent=None,
        memory_mb=None,
        max_processes=None,
    ):
        self.acquire_calls.append(
            {
                "sandbox_id": sandbox_id,
                "policy": policy_json,
                "program": program_json,
                "uid": uid,
                "name": name,
                # N83 phase 1: the sandbox's declared share, unclamped.
                "cpu_percent": cpu_percent,
                # N83 phase 2 (Task 3): the declared memory/task budget, which
                # the pool writes as ``memory.high``/``memory.max``/``pids.max``.
                "memory_mb": memory_mb,
                "max_processes": max_processes,
            }
        )
        handle = SlotHandle(
            sandbox_id=sandbox_id,
            uid=uid,
            name=name or f"rb-{sandbox_id}",
            # transport 1: no registered path, no token in the spawn argv.
            token=None,
            sock_path=None,
            control_socket=object(),
            policy_path=self.tmp / "policy.json",
            program_path=self.tmp / "program.json",
            process=FakeSlotProcess(),
            instance_pid=5100,
        )
        return handle

    def retire(self, handle):
        self.retired.append(handle.sandbox_id)
        handle.process.returncode = 0


class FakeSandlock:
    """The slice of the native module the executor touches off-Linux."""

    @staticmethod
    def minimal_dev():
        return {
            "/dev/ptmx": "/dev/ptmx",
            "/dev/pts": "/dev/pts",
            "/dev/null": "/dev/null",
            "/dev/urandom": "/dev/urandom",
            "/dev/zero": "/dev/zero",
            "/dev/tty": "/dev/tty",
        }


class FakeExecStdio:
    INHERIT = 0
    PIPED = 1
    NULL = 2
    PTY = 3


@pytest.fixture(autouse=True)
def _route_b_capable(monkeypatch, tmp_path):
    """Pretend the wheel ships supervise and the worker is root.

    ``sandlock`` itself is Linux-only; route B needs it for the ctypes channel
    client, so the tests stand in for the parts the executor uses.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    binary = tmp_path / "sandlock-supervise"
    binary.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(rb, "default_supervise_bin", lambda: binary)
    # an F17-or-newer wheel: the fd-handoff client exists (off-Linux the real
    # import fails, which would read as an old wheel)
    monkeypatch.setattr(rb, "fd_client_available", lambda: True)
    monkeypatch.setattr(sl, "sandlock", FakeSandlock())
    monkeypatch.setattr(sl, "ExecStdio", FakeExecStdio)
    monkeypatch.setattr(sl, "SandboxInstance", type("FakeInstance", (), {}))
    # The devpts pre-check is about the *host* having /dev/pts to bind; a
    # macOS runner does not, so the mount set is supplied directly (identical
    # to the off-Linux mirror in the module).
    monkeypatch.setattr(sl, "_minimal_dev_mounts", FakeSandlock.minimal_dev)
    rb.reset_slot_pools()
    yield
    rb.reset_slot_pools()


def _config(**over) -> OwnIdentityConfig:
    cfg = {
        "mode": "auto",
        "slots": 0,
        "uid_start": 20000,
        "uid_size": 16,
        "tmp_root": Path("tmp/unit-route-b-registry"),
        # C3 is the only slot-identity shape left (N52): the child unshares and
        # a reporter writes its identity. These cases are about the slot
        # machinery, so the reporter is a no-op -- the child's own polling is
        # not what they exercise.
        "slot_identity": "agent-grant",
        "identity_reporter": lambda *args: {},
    }
    cfg.update(over)
    return OwnIdentityConfig(**cfg)


def _executor(monkeypatch, *, own_identity, base_image="python:3.11-slim",
              image_rootfs=ROOTFS, host_uid=HOST_UID, per_sandbox_uid=True, **over):
    """A chroot-shape executor (the mediation shape) unless overridden."""
    kwargs = {
        "workspace_dir": WORKSPACE,
        "base_image": base_image,
        "image_rootfs": image_rootfs,
        "host_uid": host_uid,
        "per_sandbox_uid": per_sandbox_uid,
        "memory_mb": 1024,
        "cpu_percent": 100,
        "disk_mb": 2048,
        "max_processes": 128,
        "max_open_files": 1024,
        "allow_internet_access": False,
        "enable_network": False,
        "sandbox_id": "sbx_route_b",
        "own_identity": own_identity,
    }
    kwargs.update(over)
    return sl.SandlockExecutor(**kwargs)


def _exec_cmd(cmd=None, **over) -> ExecConfig:
    cfg = {
        "cmd": cmd or ["/bin/true"],
        "env": {},
        "cwd": WORKSPACE,
        "stdin_enabled": False,
    }
    cfg.update(over)
    return ExecConfig(**cfg)


# ------------------------------------------------------------------ selection


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"own_identity": None, "want": False}, id="unconfigured"),
        pytest.param({"own_identity": _config(mode="off"), "want": False}, id="mode-off"),
        pytest.param({"own_identity": _config(mode="auto"), "want": True}, id="auto-chroot"),
        # N15: the pure shape is mediated too -- host root, identity
        # translation -- so `auto` leases a slot for it exactly as it does for
        # the image shape. Keeping it in-process would run the mediation as the
        # mediator's uid, which is the T5 attribution the fork refuses.
        pytest.param(
            {
                "own_identity": _config(mode="auto"),
                "base_image": None,
                "image_rootfs": None,
                "want": True,
            },
            id="auto-pure",
        ),
        pytest.param(
            {
                "own_identity": _config(mode="auto", slots=4),
                "base_image": None,
                "image_rootfs": None,
                "want": True,
            },
            id="slots-opt-in-pure",
        ),
        pytest.param(
            {
                "own_identity": _config(mode="on"),
                "base_image": None,
                "image_rootfs": None,
                "want": True,
            },
            id="mode-on-pure",
        ),
        pytest.param(
            {
                "own_identity": _config(mode="auto"),
                "per_sandbox_uid": False,
                "host_uid": None,
                "want": False,
            },
            id="shared-uid",
        ),
        pytest.param(
            {"own_identity": _config(mode="auto"), "host_uid": None, "want": False},
            id="no-host-uid",
        ),
    ],
)
def test_route_b_selection_matrix(monkeypatch, case) -> None:
    shape = {k: v for k, v in case.items() if k != "want"}
    ex = _executor(monkeypatch, **shape)
    assert ex._own_identity_active is case["want"]


def test_a_worker_without_a_reporter_stays_in_process_and_says_why(monkeypatch) -> None:
    """No reporter, no slot -- and the reason is on record, not silent.

    This replaced "a non-root worker without a privileged starter stays
    in-process": with ``spawn`` retired (N52) there is no starter to lack --
    every slot on every shape is granted by the agent, so the thing a worker
    can lack is the *reporter*. It cannot opt into a mediation tier either:
    the fork deleted that field (B3, 2026-09-11).
    """
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    sl.SandlockExecutor._mediation_shape_disclosed = False
    ex = _executor(monkeypatch, own_identity=_config(mode="auto", identity_reporter=None))
    assert ex._own_identity_active is False
    assert "needs the control-plane reporter" in ex._own_identity_decline
    assert "mediation_run_as" not in ex._policy_ceiling()
    # ...and the shape that *has* one is the production one: an unprivileged
    # worker with no broker leases slots.
    engaged = _executor(monkeypatch, own_identity=_config(mode="auto"))
    assert engaged._own_identity_active is True


def test_forced_route_b_without_a_reporter_fails_loudly(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    with pytest.raises(
        RuntimeError,
        match=r"^route B was requested but E2B_SLOT_IDENTITY=agent-grant needs "
        r"the control-plane reporter",
    ):
        _executor(
            monkeypatch, own_identity=_config(mode="on", identity_reporter=None)
        )


def test_agent_grant_engages_route_b_without_root_or_a_broker(monkeypatch) -> None:
    """C3 Task 3: the unprivileged worker runs slots through the agent.

    Nothing in this process changes an identity any more, so root is not the
    gate -- the *reporter* is. With one, a 65534 worker with no broker spawner
    leases slots; without one there is no pool at all (the sibling test above).
    """
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    engaged = _executor(
        monkeypatch,
        own_identity=_config(mode="auto", identity_reporter=lambda *a: {}),
    )
    assert engaged._own_identity_active is True


def test_forced_route_b_without_a_host_uid_fails_loudly(monkeypatch) -> None:
    with pytest.raises(
        RuntimeError,
        match=r"^route B was requested \(E2B_ROUTE_B=on / E2B_ROUTE_B_SLOTS>0\) "
        r"but no per-sandbox host uid",
    ):
        _executor(monkeypatch, own_identity=_config(mode="on"), host_uid=None)


def test_an_old_wheel_without_the_fd_client_falls_back(monkeypatch) -> None:
    """No `sandlock_supervise_connect_fd` in the installed FFI means no
    transport-1 client. `auto` keeps the in-process backend rather than
    silently downgrading to a token-in-argv registered lease; a forced request
    says what to rebuild."""
    import envd_service.own_identity as route_b_mod

    monkeypatch.setattr(route_b_mod, "fd_client_available", lambda: False)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    assert ex._own_identity_active is False
    with pytest.raises(
        RuntimeError,
        match=(
            r"^route B was requested with transport=fd, but the installed "
            r"sandlock wheel has no sandlock_supervise_connect_fd"
        ),
    ):
        _executor(monkeypatch, own_identity=_config(mode="on"))


def test_missing_supervise_binary_keeps_the_in_process_backend(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        rb, "default_supervise_bin", lambda: tmp_path / "not-installed"
    )
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    assert ex._own_identity_active is False


def test_the_ceiling_carries_no_mediation_tier_for_the_slot(monkeypatch) -> None:
    """Two layers of the same guarantee, both against the fork's current wire:
    the ceiling never sets the tier, and the wire no longer knows the field at
    all (fork B3 deleted it), so a ceiling that carried it is refused by name
    rather than silently dropped -- a slot must never be told to mediate as
    anyone but itself, since its mediator already *is* the sandbox uid."""
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    ceiling = ex._policy_ceiling()
    assert "mediation_run_as" not in ceiling
    assert ceiling["uid"] == HOST_UID == ceiling["gid"]
    with pytest.raises(ValueError, match="mediation_run_as"):
        rb.supervise_policy_document(dict(ceiling, mediation_run_as="supervisor"))


def test_the_cgroup_lane_reaches_the_fork_policy(monkeypatch) -> None:
    """N83 phase 2 (Task 4, D7): the lane switch rides the policy document.

    ``E2B_SANDBOX_CGROUP=required`` is the one deployment in which the kernel
    is the enforcer of a sandbox's memory and task budgets, so it is also the
    only lane allowed to tell the fork to retire the mediator's own
    address-space accounting and to drop the notification rate cap (N82: the
    cap stands in for the supervisor's accounting, and behind a cgroup the
    flooder pays that accounting itself). ``off`` must put *nothing* new on the
    wire -- not ``false``, but no key at all, because that lane's document has
    to stay byte-for-byte the one it was before either field existed.
    """
    off = _executor(monkeypatch, own_identity=_config(mode="auto"), notify_rate_limit=5000)
    assert "kernel_enforced_limits" not in off._policy_ceiling()
    off_doc = rb.supervise_policy_document(off._policy_ceiling())
    assert "kernel_enforced_limits" not in off_doc
    assert off_doc["notify_rate_limit"] == 5000

    on = _executor(
        monkeypatch,
        own_identity=_config(
            mode="auto", sandbox_cgroup="required", sandbox_cgroups=_HandleStub()
        ),
        notify_rate_limit=5000,
    )
    on_doc = rb.supervise_policy_document(on._policy_ceiling())
    assert on_doc["kernel_enforced_limits"] is True
    assert "notify_rate_limit" not in on_doc

    # The two lanes differ by exactly that pair -- both of them about
    # notification accounting -- and by nothing else about the sandbox.
    assert set(on_doc) - set(off_doc) == {"kernel_enforced_limits"}
    assert set(off_doc) - set(on_doc) == {"notify_rate_limit"}
    assert {
        key: value for key, value in on_doc.items() if key != "kernel_enforced_limits"
    } == {
        key: value for key, value in off_doc.items() if key != "notify_rate_limit"
    }


def test_an_unset_notify_cap_stays_unset_on_both_lanes(monkeypatch) -> None:
    """``0`` is the fork's "no cap" on every lane: the key is simply absent.

    The rule above only decides whether a *configured* cap travels. A
    deployment that never set one must keep producing the document it always
    did, whichever lane it is on -- ``E2B_SANDBOX_NOTIFY_RATE_LIMIT`` is not in
    either shipped manifest.
    """
    for own_identity in (
        _config(mode="auto"),
        _config(mode="auto", sandbox_cgroup="required", sandbox_cgroups=_HandleStub()),
    ):
        ex = _executor(monkeypatch, own_identity=own_identity, notify_rate_limit=0)
        assert "notify_rate_limit" not in rb.supervise_policy_document(
            ex._policy_ceiling()
        )


# ------------------------------------------------------------------ lease


async def test_first_exec_leases_this_sandbox_uid_with_the_full_ceiling(
    monkeypatch,
) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))

    running = await ex.start(
        _exec_cmd(["/bin/sh", "-c", "echo hi"], env={"A": "1"})
    )
    assert pool.acquire_calls == [
        {
            "sandbox_id": "sbx_route_b",
            "policy": rb.supervise_policy_document(ex._policy_ceiling()),
            "program": None,
            "uid": HOST_UID,
            "name": "sbx_route_b",
            # N83 phase 1: the sandbox's declared share rides the lease, so the
            # pool can write it into ``sbx_<id>``'s ``cpu.max``.
            "cpu_percent": 100,
            # N83 phase 2 (Task 3): the other two declared sizes ride the same
            # lease, so the box's ``memory.high``/``memory.max``/``pids.max``
            # are the record's numbers and nothing else.
            "memory_mb": 1024,
            "max_processes": 128,
        }
    ]
    policy = pool.acquire_calls[0]["policy"]
    assert policy["chroot"] == str(ROOTFS)
    # Two spellings, two consumers: the host path is what the mediator's
    # on-behalf gate compares real paths against, and the mount points are what
    # the fork's Landlock rules are written in (it grants a mount's *source* the
    # rights its mount point declares -- see docs/chroot-workspace-exec.md §7).
    assert policy["fs_writable"] == [WORKSPACE, "/workspace", "/home/user"]
    assert f"/workspace:{WORKSPACE}" in policy["fs_mount"]
    assert policy["max_memory"] == "1024M"
    assert running.pid == 5150
    assert pool.log[0][0] == "exec"
    # A second command reuses the same slot: one generation per sandbox.
    await ex.start(_exec_cmd())
    assert len(pool.acquire_calls) == 1
    assert [verb for verb, _, _ in pool.log] == ["exec", "exec"]


async def test_shape_knobs_reach_the_slot_document(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(
        monkeypatch,
        own_identity=_config(mode="auto"),
        enable_net_isolation=True,
        fd_inject_connect=True,
        pid_ns=False,
        bind_inject=True,
        port_mappings={50006: 8080},
        extra_fs_writable=["/var/lib/e2b-volumes/vol_1"],
    )
    ex.set_mcp_bind_port(50006)
    await ex.start(_exec_cmd())
    policy = pool.acquire_calls[0]["policy"]
    assert policy["net_allow_bind"] == [50006]
    assert policy["net_isolation"] is True
    assert policy["fd_inject_connect"] is True
    # The wire field the supervisor's policy parser expects (its manifest keeps
    # the name; see crates/sandlock-supervise/src/policy.rs).
    assert policy["net_bind_inject"] is True
    assert policy["port_mappings"] == {50006: 8080}
    assert policy["fs_writable"] == [
        WORKSPACE,
        "/var/lib/e2b-volumes/vol_1",
        "/workspace",
        "/home/user",
    ]


async def test_mcp_gateway_exec_is_the_only_one_allowed_to_bind(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    ex.set_mcp_bind_port(50006)
    await ex.start(_exec_cmd(["python", "-m", "mcp-gateway"]))
    await ex.start(_exec_cmd())
    assert pool.log[0][1]["bind_ports"] == [50006]
    assert "bind_ports" not in pool.log[1][1]


async def test_exec_params_travel_as_verb_args(monkeypatch) -> None:
    """Per-exec cwd/env/clean_env keep their in-process meaning: cwd maps into
    the chroot view (``/home/user``, the canonical alias) and bash falls back
    to sh."""
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    await ex.start(
        _exec_cmd(["/bin/bash", "-lc", "pwd"], env={"FOO": "bar"}, cwd="")
    )
    assert pool.log[0][1] == {
        "argv": ["/bin/sh", "-lc", "pwd"],
        "cwd": "/home/user",
        "env": {"FOO": "bar"},
        "clean_env": True,
    }


async def test_output_streams_and_exit_code_comes_from_wait_child(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool(child_output=b"hello\n")
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    running = await ex.start(_exec_cmd(["/bin/echo"]))
    chunks = [
        (kind, data)
        async for kind, data in running.output()
        if kind != "__eof__"
    ]
    # Only non-empty chunks reach a consumer: the stderr pipe hit EOF empty.
    assert chunks == [("stdout", b"hello\n")]
    assert await running.exit_code() == 0
    assert pool.log[-1] == ("wait_child", {"child_id": 11}, ())
    assert ex._child_registry == {}


async def test_pty_command_gets_a_worker_side_master(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    running = await ex.start(
        _exec_cmd(["/bin/sh"], pty=True, rows=40, cols=120)
    )
    stdin_fd, stdout_fd, stderr_fd = pool.log[0][2]
    assert stdin_fd == stdout_fd == stderr_fd  # one slave, three ends
    assert running._pty_mode is True
    # A slot child is signalled by number, so pause/resume may use it (FUP #8).
    assert running.supports_signal_pause is True
    running.kill(19)
    assert pool.log[-1] == ("kill_child", {"child_id": 11, "signum": 19}, ())


async def test_a_dead_slot_is_leased_again_before_the_exec_retries(
    monkeypatch,
) -> None:
    """``SlotDeadError`` surfaces at exec time; the recovery is a slot restart
    at the same uid (W1), never a fall back to the in-process mediator."""
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    pool.replies["exec"] = SlotDeadError("route-B instance sbx_route_b is dead")
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    with pytest.raises(SlotDeadError, match="is dead"):
        await ex.start(_exec_cmd())
    assert len(pool.acquire_calls) == 2
    assert pool.retired == ["sbx_route_b"]


async def test_slot_that_never_starts_is_restarted_once_then_reported(
    monkeypatch,
) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    attempts = []

    record = pool.acquire_sync

    def _dead_on_first(
        sandbox_id,
        policy_json,
        program_json=None,
        *,
        uid=None,
        name=None,
        cpu_percent=None,
        memory_mb=None,
        max_processes=None,
    ):
        attempts.append(uid)
        if len(attempts) == 1:
            raise SlotDeadError(
                f"route-B slot {name} (uid {uid}) exited before binding: boom"
            )
        return record(
            sandbox_id,
            policy_json,
            program_json,
            uid=uid,
            name=name,
            cpu_percent=cpu_percent,
            memory_mb=memory_mb,
            max_processes=max_processes,
        )

    pool.acquire_sync = _dead_on_first
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    running = await ex.start(_exec_cmd())
    assert attempts == [HOST_UID, HOST_UID]
    assert running.pid == 5150


async def test_close_ends_the_generation_and_frees_the_uid(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    inst = ex._ensure_instance()
    assert ex.instance_handle is inst
    ex.close()
    ex.close()
    assert pool.retired == ["sbx_route_b"]
    assert ex.instance_handle is None
    with pytest.raises(RuntimeError, match="shut down"):
        await ex.start(_exec_cmd())
    assert len(pool.acquire_calls) == 1


async def test_network_update_goes_to_the_slot_and_logs_staleness(
    monkeypatch, caplog
) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    pool.replies["update_network"] = {"stale_child_ids": [11]}
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(
        monkeypatch,
        own_identity=_config(mode="auto"),
        enable_network=True,
        network={"allowOut": ["198.18.0.99"]},
        allow_internet_access=True,
    )
    await ex.start(_exec_cmd())
    with caplog.at_level("INFO", logger="envd_service.executors.sandlock"):
        ex.update_network({"allowOut": []})
    assert pool.log[-1] == ("update_network", {"ips": []}, ())
    assert "stale_child_count=1" in caplog.text


# ------------------------------------------------- checkpoint / restore verbs


async def test_a_checkpoint_goes_to_the_slot_with_the_workers_own_path(
    monkeypatch,
) -> None:
    """S1a/S2: the capture happens in the slot, the *path* is the deployment's.

    The image is the sandbox's whole process, so the write has to happen where
    the process tree lives (the slot); where those bytes land -- and whose
    account they are billed to -- is the worker's business, so it is handed the
    answer rather than the policy.
    """
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    image = "/var/lib/e2b-sandboxes/_runtime/sbx_route_b/checkpoint/latest"
    pool.replies["checkpoint"] = {
        "dir": image,
        "name": "latest",
        "pid": 5100,
        "fds": 4,
    }
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    await ex.start(_exec_cmd())

    reply = ex.capture_checkpoint(image, "latest")
    # `exclude_main` rides every capture: this deployment's sessions run a park
    # as their main child (see `OwnIdentityInstance.capture_checkpoint`), so the
    # workload is the child beside it. Without the flag the engine refuses a
    # sandbox with anything running in it.
    assert pool.log[-1] == (
        "checkpoint",
        {"dir": image, "name": "latest", "exclude_main": True},
        (),
    )
    assert reply == {
        "captured": True,
        "reason": "",
        "dir": image,
        "name": "latest",
        "pid": 5100,
        "fds": 4,
    }

    # No name means no `name` field at all: the slot's own default is the
    # image's business, not something this side spells out.
    ex.capture_checkpoint("/var/lib/e2b-sandboxes/_runtime/sbx_route_b/checkpoint/other")
    assert pool.log[-1] == (
        "checkpoint",
        {
            "dir": "/var/lib/e2b-sandboxes/_runtime/sbx_route_b/checkpoint/other",
            "exclude_main": True,
        },
        (),
    )


def test_a_capture_without_a_live_session_leases_nothing_to_find_out(
    monkeypatch,
) -> None:
    """A capture is not a reason to start a session -- there is nothing in it."""
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))

    reply = ex.capture_checkpoint("/var/lib/e2b-sandboxes/_runtime/x/checkpoint/latest")

    assert reply == {
        "captured": False,
        "reason": "no live session on this worker to capture",
    }
    assert pool.acquire_calls == []
    assert pool.log == []


async def test_a_refused_capture_carries_the_slots_own_words(monkeypatch) -> None:
    """A session with several live children refuses, and the reason survives.

    The refusal must not read as a dead slot (the executor rebuilds those), and
    it must not be swallowed either: the caller keeps pausing the sandbox in
    place, and the reason is what says why there is no image.
    """
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    pool.replies["checkpoint"] = rb.SandboxError(
        "checkpoint requires exactly one live child, found 2"
    )
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))
    await ex.start(_exec_cmd())

    reply = ex.capture_checkpoint("/var/lib/e2b-sandboxes/_runtime/x/checkpoint/latest")

    assert reply == {
        "captured": False,
        "reason": "checkpoint requires exactly one live child, found 2",
    }


def test_the_in_process_mediator_cannot_capture_and_says_which_shape_it_is(
    monkeypatch,
) -> None:
    """No slot owns the process tree, so no verb can reach it (T5's shape)."""
    ROOTFS.mkdir(parents=True, exist_ok=True)
    ex = _executor(monkeypatch, own_identity=None)

    reply = ex.capture_checkpoint("/var/lib/e2b-sandboxes/_runtime/x/checkpoint/latest")

    assert reply == {
        "captured": False,
        "reason": (
            "this worker runs the in-process mediator; the sandbox's process "
            "tree is not in a slot, so no checkpoint verb can reach it"
        ),
    }


async def test_a_restore_leases_the_session_and_hands_over_the_image(monkeypatch) -> None:
    """The pooled shape: lease a slot, *then* tell it what to bring back.

    Creating the session is part of the job -- the image is resumed as a child of
    it, which is what keeps ``exec`` working afterwards ((b)/D9).
    """
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    image = "/var/lib/e2b-sandboxes/_runtime/sbx_route_b/checkpoint/latest"
    pool.replies["restore"] = {
        "dir": image,
        "child_id": 5,
        "pid": 1234,
        "restore_skipped": [{"fd": 7, "path": "socket:[9]"}],
    }
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))

    reply = ex.restore_checkpoint(image)

    assert [c["sandbox_id"] for c in pool.acquire_calls] == ["sbx_route_b"]
    assert pool.log[-1] == ("restore", {"dir": image}, ())
    assert reply == {
        "restored": True,
        "reason": "",
        "dir": image,
        "child_id": 5,
        "pid": 1234,
        "restore_skipped": [{"fd": 7, "path": "socket:[9]"}],
    }


async def test_a_restore_a_slot_does_not_know_is_reported_not_crashed(
    monkeypatch,
) -> None:
    """An older wheel says ``unknown verb``; the caller carries on from it."""
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    pool.replies["restore"] = rb.SandboxError("unknown verb: restore")
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"))

    reply = ex.restore_checkpoint("/var/lib/e2b-sandboxes/_runtime/x/checkpoint/latest")

    assert reply == {"restored": False, "reason": "unknown verb: restore"}


def test_in_process_chroot_shape_is_disclosed(monkeypatch, caplog) -> None:
    """Losing the tier must not turn into a cryptic create failure.

    A privileged worker with the chroot shape and no slot is exactly the case
    the fork now refuses. The refusal reason never reaches us through the FFI
    (SL-12: a null handle becomes `sandlock_instance_launch failed`), so the
    executor says it once, up front, naming why no slot was available.
    """
    import logging

    sl.SandlockExecutor._mediation_shape_disclosed = False
    with caplog.at_level(logging.ERROR, logger="envd_service.executors.sandlock"):
        ex = _executor(monkeypatch, own_identity=None)
    assert ex._own_identity_active is False
    messages = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert len(messages) == 1, messages
    assert "runs in-process, not on a supervise slot" in messages[0]
    assert "the worker passed no route-B config" in messages[0]
    assert "E2B_PER_SANDBOX_UID" in messages[0]

    # Disclosed once per process, and not at all when a slot is in use.
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="envd_service.executors.sandlock"):
        _executor(monkeypatch, own_identity=None)
        _executor(monkeypatch, own_identity=_config(mode="auto"))
    assert [r.message for r in caplog.records if r.levelno == logging.ERROR] == []


@pytest.mark.parametrize(
    "case",
    [
        # The fork's rule is about *remap privilege*, not about euid 0 alone
        # (F14): a file-cap launcher mediator is refused the same way.
        pytest.param(
            {"euid": 0, "host_uid": HOST_UID, "caps": False, "want": True},
            id="root-worker-refuses",
        ),
        pytest.param(
            {"euid": 65534, "host_uid": HOST_UID, "caps": True, "want": True},
            id="file-cap-launcher-refuses",
        ),
        # ... and the same launcher mediating as its own uid is the shape the
        # unprivileged production lane actually runs: no refusal, so the
        # tier-removal ERROR must stay quiet there instead of crying wolf.
        pytest.param(
            {"euid": 65534, "host_uid": 65534, "caps": True, "want": False},
            id="same-uid-mediator-accepted",
        ),
        pytest.param(
            {"euid": 65534, "host_uid": HOST_UID, "caps": False, "want": False},
            id="plain-nonroot-worker-accepted",
        ),
        pytest.param(
            {"euid": 0, "host_uid": 0, "caps": False, "want": False},
            id="root-sandbox-accepted",
        ),
        # N15: the pure shape has path mediation now (host root as the
        # mediator's root), so it carries the same T5 rule as the image shape
        # -- a root mediator remapping the sandbox to another uid is refused,
        # whatever the shape is. Before N15 this case was accepted *because*
        # that shape mediated nothing.
        pytest.param(
            {"euid": 0, "host_uid": HOST_UID, "caps": False, "want": True,
             "image_rootfs": None},
            id="pure-shape-refused",
        ),
    ],
)
def test_the_refusal_predicate_tracks_the_forks_privilege_rule(
    monkeypatch, case
) -> None:
    """`_in_process_mediation_is_refused` mirrors `mediation_remap_is_refused`.

    The gate has to agree with the fork or the ERROR below either cries wolf
    (unprivileged lane) or stays silent exactly when a create is about to be
    refused (privileged worker, no slot).
    """
    want = case.pop("want")
    euid = case.pop("euid")
    caps = case.pop("caps")
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    monkeypatch.setattr(
        sl, "has_effective_cap", lambda bit: caps if bit in (6, 7) else False
    )
    ex = _executor(monkeypatch, own_identity=None, **case)
    assert ex._in_process_mediation_is_refused() is want


# ------------------------------------- N83 phase 1: the declared cpu share

#: Route B's own words for the only decline these cases reach: a worker that
#: knows no control plane cannot report a slot identity (C3: the child
#: unshares and the agent writes it), so the slot is declined.
NO_REPORTER_DECLINE = (
    "E2B_SLOT_IDENTITY=agent-grant needs the control-plane reporter, and this "
    "worker does not know where its control plane is (E2B_CONTROL_PLANE_URL "
    "and E2B_NODE_ID)"
)


class _HandleStub:
    """Only present, to satisfy ``required ⇒ a handle`` (fix round 1, Finding 1).

    R15's refusal happens while the executor is constructed -- before a slot is
    leased -- so nothing in this double is ever called.
    """


async def test_the_declared_cpu_share_rides_the_lease_unclamped(monkeypatch) -> None:
    """``200`` reaches the pool as ``200`` -- the fork policy's clamp is not it.

    The policy ceiling clamps ``max_cpu`` to ``min(100, ...)`` for the fork's own
    user-space throttle; the per-sandbox cgroup is what enforces the *declared*
    share (two cores here), so the number that travels on the lease has to be
    the declared one.
    """
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, own_identity=_config(mode="auto"), cpu_percent=200)

    await ex.start(_exec_cmd(["/bin/true"]))

    assert [call["cpu_percent"] for call in pool.acquire_calls] == [200]
    # The two spellings really are different: the fork's own ceiling still
    # clamps, and that is the point of the assertion above.
    assert pool.acquire_calls[0]["policy"]["max_cpu"] == 100


def test_an_in_process_sandbox_is_refused_when_the_cgroup_is_required(
    monkeypatch,
) -> None:
    """Plan Review Focus 4: no slot means no cgroup, and ``required`` means no.

    A sandbox that route B declines runs under the in-process mediator, which
    has no per-sandbox cgroup at all -- so a deployment that asked for one must
    fail the create by name instead of starting an uncapped sandbox.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    with pytest.raises(RuntimeError) as excinfo:
        _executor(
            monkeypatch,
            own_identity=_config(
                mode="auto",
                identity_reporter=None,
                sandbox_cgroup="required",
                sandbox_cgroups=_HandleStub(),
            ),
        )
    # The decline's own words are in the refusal, in full: the operator gets
    # the fix, not just the switch that failed.
    assert str(excinfo.value) == (
        "E2B_SANDBOX_CGROUP=required refuses an in-process sandbox: this "
        "sandbox would run without a per-sandbox cgroup "
        f"({NO_REPORTER_DECLINE}). Give the sandbox a route-B slot (per-sandbox "
        "host uid + the control-plane reporter), or set "
        "E2B_SANDBOX_CGROUP=off to accept uncapped sandboxes."
    )


def test_an_in_process_sandbox_still_falls_back_when_the_cgroup_is_off(
    monkeypatch,
) -> None:
    """The shipped default: the in-process fallback is exactly as it was."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    ex = _executor(
        monkeypatch,
        own_identity=_config(mode="auto", identity_reporter=None, sandbox_cgroup="off"),
    )
    assert ex._own_identity_active is False
    assert ex._own_identity_decline == NO_REPORTER_DECLINE
