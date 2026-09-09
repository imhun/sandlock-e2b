"""SandlockExecutor on route B: the supervise slot as the exec instance.

The native ``sandlock-supervise`` binary is Linux-only and needs privilege to
start at another uid, so the *fleet* is faked here and the assertions are
about what the executor puts on the wire: which uid is leased, which policy
document the generation gets, how each ``start``/``update_network``/``close``
maps to a verb, and that closed/dead recovery restarts the slot instead of
silently falling back to the in-process mediator. The real two-uid evidence is
``tests/contract/test_route_b_slot_pool.py``; the executor running end-to-end
against a real slot is ``tests/contract/test_route_b_executor.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import envd_service.executors.sandlock as sl
import envd_service.route_b as rb
from envd_service.executors.base import ExecConfig
from envd_service.route_b import RouteBConfig, SlotDeadError, SlotHandle
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
        self, sandbox_id, policy_json, program_json=None, *, uid=None, name=None
    ):
        self.acquire_calls.append(
            {
                "sandbox_id": sandbox_id,
                "policy": policy_json,
                "program": program_json,
                "uid": uid,
                "name": name,
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


def _config(**over) -> RouteBConfig:
    cfg = {
        "mode": "auto",
        "slots": 0,
        "uid_start": 20000,
        "uid_size": 16,
        "tmp_root": Path("tmp/unit-route-b-registry"),
    }
    cfg.update(over)
    return RouteBConfig(**cfg)


def _executor(monkeypatch, *, route_b, base_image="python:3.11-slim",
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
        "route_b": route_b,
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
        pytest.param({"route_b": None, "want": False}, id="unconfigured"),
        pytest.param({"route_b": _config(mode="off"), "want": False}, id="mode-off"),
        pytest.param({"route_b": _config(mode="auto"), "want": True}, id="auto-chroot"),
        pytest.param(
            {
                "route_b": _config(mode="auto"),
                "base_image": None,
                "image_rootfs": None,
                "want": False,
            },
            id="auto-pure",
        ),
        pytest.param(
            {
                "route_b": _config(mode="auto", slots=4),
                "base_image": None,
                "image_rootfs": None,
                "want": True,
            },
            id="slots-opt-in-pure",
        ),
        pytest.param(
            {
                "route_b": _config(mode="on"),
                "base_image": None,
                "image_rootfs": None,
                "want": True,
            },
            id="mode-on-pure",
        ),
        pytest.param(
            {
                "route_b": _config(mode="auto"),
                "per_sandbox_uid": False,
                "host_uid": None,
                "want": False,
            },
            id="shared-uid",
        ),
        pytest.param(
            {"route_b": _config(mode="auto"), "host_uid": None, "want": False},
            id="no-host-uid",
        ),
    ],
)
def test_route_b_selection_matrix(monkeypatch, case) -> None:
    shape = {k: v for k, v in case.items() if k != "want"}
    ex = _executor(monkeypatch, **shape)
    assert ex._route_b_active is case["want"]


def test_non_root_auto_keeps_the_in_process_supervisor_tier(monkeypatch) -> None:
    """A worker that cannot start a slot stays on route A -- and keeps the
    documented T5 owner semantics (``mediation_run_as=supervisor``)."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
    assert ex._route_b_active is False
    # A non-root worker mediates as the sandbox's own uid already, so it never
    # needed the downgrade tier; only a root worker on the chroot shape does.
    assert ex._policy_ceiling()["mediation_run_as"] == "caller"


def test_forced_route_b_without_a_privileged_starter_fails_loudly(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    with pytest.raises(
        RuntimeError,
        match=(
            r"^route B was requested but this worker cannot start a slot as uid "
            r"20007 \(needs root / CAP_SETUID or an injected launcher spawner\)$"
        ),
    ):
        _executor(monkeypatch, route_b=_config(mode="on"))


def test_forced_route_b_without_a_host_uid_fails_loudly(monkeypatch) -> None:
    with pytest.raises(
        RuntimeError,
        match=r"^route B was requested \(E2B_ROUTE_B=on / E2B_ROUTE_B_SLOTS>0\) "
        r"but this sandbox has no per-sandbox host uid",
    ):
        _executor(monkeypatch, route_b=_config(mode="on"), host_uid=None)


def test_an_old_wheel_without_the_fd_client_falls_back(monkeypatch) -> None:
    """No `sandlock_supervise_connect_fd` in the installed FFI means no
    transport-1 client. `auto` keeps the in-process backend rather than
    silently downgrading to a token-in-argv registered lease; a forced request
    says what to rebuild."""
    import envd_service.route_b as route_b_mod

    monkeypatch.setattr(route_b_mod, "fd_client_available", lambda: False)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
    assert ex._route_b_active is False
    with pytest.raises(
        RuntimeError,
        match=(
            r"^route B was requested with transport=fd, but the installed "
            r"sandlock wheel has no sandlock_supervise_connect_fd"
        ),
    ):
        _executor(monkeypatch, route_b=_config(mode="on"))


def test_missing_supervise_binary_keeps_the_in_process_backend(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        rb, "default_supervise_bin", lambda: tmp_path / "not-installed"
    )
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
    assert ex._route_b_active is False


def test_route_b_drops_the_supervisor_downgrade_tier(monkeypatch) -> None:
    """The downgrade is an in-process-only escape hatch: the document that
    reaches the slot carries no ``mediation_run_as`` at all."""
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
    ceiling = ex._policy_ceiling()
    assert ceiling["mediation_run_as"] == "supervisor"
    document = rb.supervise_policy_document(ceiling)
    assert "mediation_run_as" not in document
    assert document["uid"] == HOST_UID == document["gid"]


# ------------------------------------------------------------------ lease


async def test_first_exec_leases_this_sandbox_uid_with_the_full_ceiling(
    monkeypatch,
) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))

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
        }
    ]
    policy = pool.acquire_calls[0]["policy"]
    assert policy["chroot"] == str(ROOTFS)
    assert policy["fs_writable"] == [WORKSPACE]
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
        route_b=_config(mode="auto"),
        enable_net_isolation=True,
        fd_inject_connect=True,
        port_mappings={50006: 8080},
        extra_fs_writable=["/var/lib/e2b-volumes/vol_1"],
    )
    ex.set_mcp_bind_port(50006)
    await ex.start(_exec_cmd())
    policy = pool.acquire_calls[0]["policy"]
    assert policy["net_allow_bind"] == [50006]
    assert policy["net_isolation"] is True
    assert policy["fd_inject_connect"] is True
    assert policy["port_mappings"] == {50006: 8080}
    assert policy["fs_writable"] == [WORKSPACE, "/var/lib/e2b-volumes/vol_1"]


async def test_mcp_gateway_exec_is_the_only_one_allowed_to_bind(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
    ex.set_mcp_bind_port(50006)
    await ex.start(_exec_cmd(["python", "-m", "mcp-gateway"]))
    await ex.start(_exec_cmd())
    assert pool.log[0][1]["bind_ports"] == [50006]
    assert "bind_ports" not in pool.log[1][1]


async def test_exec_params_travel_as_verb_args(monkeypatch) -> None:
    """Per-exec cwd/env/clean_env keep their in-process meaning: cwd maps into
    the chroot view (``/workspace``) and bash falls back to sh."""
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
    await ex.start(
        _exec_cmd(["/bin/bash", "-lc", "pwd"], env={"FOO": "bar"}, cwd="")
    )
    assert pool.log[0][1] == {
        "argv": ["/bin/sh", "-lc", "pwd"],
        "cwd": "/workspace",
        "env": {"FOO": "bar"},
        "clean_env": True,
    }


async def test_output_streams_and_exit_code_comes_from_wait_child(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool(child_output=b"hello\n")
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
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
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
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
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
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

    def _dead_on_first(sandbox_id, policy_json, program_json=None, *, uid=None, name=None):
        attempts.append(uid)
        if len(attempts) == 1:
            raise SlotDeadError(
                f"route-B slot {name} (uid {uid}) exited before binding: boom"
            )
        return record(sandbox_id, policy_json, program_json, uid=uid, name=name)

    pool.acquire_sync = _dead_on_first
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
    running = await ex.start(_exec_cmd())
    assert attempts == [HOST_UID, HOST_UID]
    assert running.pid == 5150


async def test_close_ends_the_generation_and_frees_the_uid(monkeypatch) -> None:
    ROOTFS.mkdir(parents=True, exist_ok=True)
    pool = FakePool()
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    ex = _executor(monkeypatch, route_b=_config(mode="auto"))
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
        route_b=_config(mode="auto"),
        enable_network=True,
        network={"allowOut": ["198.18.0.99"]},
        allow_internet_access=True,
    )
    await ex.start(_exec_cmd())
    with caplog.at_level("INFO", logger="envd_service.executors.sandlock"):
        ex.update_network({"allowOut": []})
    assert pool.log[-1] == ("update_network", {"ips": []}, ())
    assert "stale_child_count=1" in caplog.text
