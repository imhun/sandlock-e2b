"""route-B plumbing: policy wire, W1 slot lease, and the instance shim.

Everything here runs off-Linux: the slot *spawner* and the channel are
injected, so the assertions cover what the executor will actually put on the
wire (policy document, verb arguments, stdio descriptor hygiene) rather than
the native supervise binary. The real two-uid evidence lives in
``tests/contract/test_route_b_slot_pool.py``.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import envd_service.route_b as rb
from envd_service.route_b import (
    PARKING_PROGRAM,
    RouteBInstance,
    SlotDeadError,
    W1SlotPool,
    supervise_policy_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _private_registry(monkeypatch, tmp_path):
    """Keep the unit fleet off the real ``/tmp`` registry location.

    The production path is fixed by the fork (``/tmp/sandlock-ctl-<uid>
    -registry``); the tests only need a deterministic per-uid path, and the
    formula itself is pinned by :func:`test_registry_socket_path_matches_fork_formula`.
    """

    def _path(uid: int, name: str) -> Path:
        return (
            tmp_path
            / f"sandlock-ctl-{uid}-registry"
            / f"{rb._fnv1a_hex(name)}.d"
            / "control.sock"
        )

    monkeypatch.setattr(rb, "_registry_sock_path", _path)
    return tmp_path


class FakeProcess:
    """A stand-in for the ``Popen`` of a spawned slot."""

    def __init__(self, pid: int = 4242, returncode: int | None = None) -> None:
        self.pid = pid
        self.stderr = None
        self.returncode = returncode
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
    """Records every verb; answers from a queue of per-verb replies."""

    def __init__(self, path: str, token: str, replies: dict, log: list) -> None:
        self.path = path
        self.token = token
        self._replies = replies
        self._log = log
        self.dups: list[int] = []

    def request(self, verb, args=None, fds=()):
        fds = tuple(fds)
        self._log.append((verb, args, fds))
        # Emulate the kernel: SCM_RIGHTS duplicates the ends into the peer, so
        # the "child" keeps its own references after the caller closes its.
        self.dups = [os.dup(fd) for fd in fds]
        reply = self._replies.get(verb, {})
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self) -> None:
        for fd in self.dups:
            try:
                os.close(fd)
            except OSError:
                pass
        self.dups = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _pool(tmp_path, *, replies=None, log=None, size=2, uid_start=20000, spawner=None,
          channel_factory=None, socket_timeout_s=2.0, transport="fd"):
    """A fleet whose spawner binds the registered socket like a real slot.

    Returns ``(pool, spawned, log, channels)``; ``spawned`` grows one
    ``FakeProcess`` per lease, the way a W1 restart replaces the process but
    keeps the uid.
    """
    log = log if log is not None else []
    replies = replies if replies is not None else {"stats": {"launched": True, "pid": 7}}
    pool_transport = transport
    spawned: list[FakeProcess] = []
    channels: list[FakeChannel] = []

    def _spawn(**kw):
        # A transport-fd lease hands the spawner a control descriptor; the fake
        # asserts it arrived and binds nothing on disk.
        assert (kw.get("control_fd") is not None) == (
            pool_transport == "fd"
        ), sorted(kw)
        if pool_transport == "path":
            sock = rb._registry_sock_path(kw["uid"], kw["name"])
            sock.parent.mkdir(parents=True, exist_ok=True)
            sock.touch()
        proc = FakeProcess()
        spawned.append(proc)
        return proc

    def _factory(handle):
        ch = FakeChannel(str(handle.sock_path), handle.token, replies, log)
        channels.append(ch)
        return ch

    pool = W1SlotPool(
        uid_start=uid_start,
        size=size,
        tmp_root=tmp_path / "slots",
        supervise_bin=tmp_path / "sandlock-supervise",
        spawner=spawner or _spawn,
        channel_factory=channel_factory or _factory,
        socket_timeout_s=socket_timeout_s,
        transport=transport,
    )
    return pool, spawned, log, channels


# ------------------------------------------------------------------ wire


def test_registry_socket_path_matches_fork_formula(monkeypatch):
    """The client-side path is the fork's: per-uid registry root, FNV-1a hash
    of the slot name, ``<hash>.d/control.sock``."""
    monkeypatch.undo()  # the autouse fixture redirects this very function
    path = rb._registry_sock_path(20007, "rb-sbx_x")
    assert path == Path(
        f"/tmp/sandlock-ctl-20007-registry/{rb._fnv1a_hex('rb-sbx_x')}.d/control.sock"
    )


def test_policy_document_uses_wire_spellings_and_drops_unsupplied_fields():
    ceiling = {
        "fs_writable": ["/ws"],
        "fs_readable": ["/usr", "/"],
        "fs_denied": ["/proc/kcore"],
        "net_allow": [],
        "host_mask": None,
        "egress_proxy": None,
        "notify_rate_limit": None,
        "max_memory": "1024M",
        "uid": 20000,
        "gid": 20000,
        "mediation_run_as": "supervisor",
        "net_allow_bind": [50006],
        "net_isolation": True,
        "port_mappings": {50006: 8080},
        "chroot": "/rootfs",
        "fs_mount": {"/workspace": "/ws", "/dev/null": "/dev/null"},
    }
    assert supervise_policy_document(ceiling) == {
        "fs_writable": ["/ws"],
        "fs_readable": ["/usr", "/"],
        "fs_denied": ["/proc/kcore"],
        "net_allow": [],
        "max_memory": "1024M",
        "uid": 20000,
        "gid": 20000,
        "net_allow_bind": [50006],
        "net_isolation": True,
        "port_mappings": {50006: 8080},
        "chroot": "/rootfs",
        "fs_mount": ["/workspace:/ws", "/dev/null:/dev/null"],
    }


def test_policy_document_drops_mediation_tier_by_name_not_by_value():
    """``mediation_run_as`` is the *in-process* downgrade tier: a slot's
    mediator already is this uid, so the key must never reach the wire -- not
    even when the ceiling carries the safe value."""
    for tier in ("supervisor", "caller"):
        doc = supervise_policy_document({"uid": 1, "mediation_run_as": tier})
        assert doc == {"uid": 1}


def test_policy_document_refuses_a_field_the_wire_does_not_know():
    with pytest.raises(
        ValueError,
        match=r"^route-B policy ceiling carries field\(s\) the supervise wire "
        r"does not accept: `name`, `policy_fn`$".replace("`", ""),
    ):
        supervise_policy_document({"name": "sbx", "policy_fn": None or "x"})


def test_supervise_policy_fields_are_the_fork_wire_fields():
    """Drift guard: the local field list must equal
    ``sandlock-supervise/src/policy.rs::POLICY_FIELDS`` exactly, so a new fork
    policy field cannot be silently dropped by this side."""
    import re

    source = REPO_ROOT / "third_party/sandlock/crates/sandlock-supervise/src/policy.rs"
    if not source.exists():  # pragma: no cover - source checkout only
        pytest.skip("fork submodule not checked out")
    text = source.read_text(encoding="utf-8")
    block = re.search(
        r"pub const POLICY_FIELDS: &\[\&str\] = &\[(.*?)\];", text, re.S
    ).group(1)
    fork_fields = set(re.findall(r'"([a-z_0-9]+)"', block))
    assert fork_fields == set(rb.SUPERVISE_POLICY_FIELDS)


def test_parking_program_stops_instead_of_spinning():
    """M0 must never exit (main exit collapses the generation) and must never
    cost anything. ``read x < /dev/zero`` -- the obvious pick -- burns a core
    forever, because an exec session's main stdio is /dev/null and ``read``
    never sees a line terminator; self-stop is the zero-cost park."""
    assert PARKING_PROGRAM == {
        "argv": ["/bin/sh", "-c", "while :; do kill -STOP $$; done"]
    }
    assert "/dev/zero" not in PARKING_PROGRAM["argv"][2]


# ------------------------------------------------------------------ slots


async def test_acquire_leases_the_requested_uid_and_lease_documents(tmp_path):
    pool, spawned, log, channels = _pool(tmp_path)
    handle = await pool.acquire("sbx_a", {"uid": 20001}, uid=20001)
    assert handle.uid == 20001
    assert handle.sandbox_id == "sbx_a"
    assert handle.name == "rb-sbx_a"
    assert handle.process is spawned[0]
    assert handle.instance_pid == 7
    # transport 1: the credential is the descriptor, so nothing carries a
    # token and no registry path exists.
    assert handle.token is None
    assert handle.sock_path is None
    assert handle.control_socket is not None
    assert handle.verb_timeout_s == 15.0
    assert json.loads(handle.policy_path.read_text()) == {"uid": 20001}
    assert json.loads(handle.program_path.read_text()) == PARKING_PROGRAM
    assert handle.policy_path.parent.exists()
    assert handle.control_socket.fileno() != -1
    assert [(verb, args) for verb, args, _ in log] == [("stats", None)]
    assert pool.acquired_uid("sbx_a") == 20001
    assert [slot.uid for slot in pool.live_slots] == [20001]


def test_the_pool_hands_the_spawner_a_usable_control_descriptor(tmp_path):
    """Transport 1 is only as good as the descriptor it hands over: the pool
    must give the spawner a live `AF_UNIX` `SOCK_STREAM` end, keep its own end
    usable afterwards, and leave no path or token on the handle."""
    import socket

    seen: dict = {}

    def _spawn(**kw):
        seen["control_fd"] = kw["control_fd"]
        proc = FakeProcess()
        spawned.append(proc)
        return proc

    pool, spawned, log, channels = _pool(tmp_path, spawner=_spawn)
    handle = pool.acquire_sync("sbx_fd", {}, uid=20000)
    assert seen["control_fd"] is not None and seen["control_fd"] >= 0
    assert handle.control_socket.type == socket.SOCK_STREAM
    assert handle.control_socket.family == socket.AF_UNIX
    assert handle.sock_path is None and handle.token is None
    # The verbs go through the descriptor: the factory sees no path at all.
    assert channels[0].path == "None"
    assert handle.verb_timeout_s == 15.0
    worker_fd = handle.control_socket.fileno()
    pool.retire(handle)
    assert spawned[0].returncode == 0
    # retire closes the worker end, so a slot that outlived us sees EOF on its
    # control stream and tears its own generation down instead of serving a
    # worker that is already gone.
    assert handle.control_socket is None
    with pytest.raises(OSError):
        os.fstat(worker_fd)


def test_slot_pools_are_cached_per_transport(tmp_path, monkeypatch):
    """A registered fleet and an fd-handoff fleet must never share a ledger."""
    from envd_service.route_b import RouteBConfig, reset_slot_pools, slot_pool_for

    monkeypatch.setattr(
        rb, "default_supervise_bin", lambda: tmp_path / "sandlock-supervise"
    )
    reset_slot_pools()
    base = dict(uid_start=20000, uid_size=2, tmp_root=tmp_path / "reg")
    fd_pool = slot_pool_for(RouteBConfig(**base, transport="fd"))
    path_pool = slot_pool_for(RouteBConfig(**base, transport="path"))
    assert fd_pool is not path_pool
    assert fd_pool.transport == "fd" and path_pool.transport == "path"
    assert (
        slot_pool_for(RouteBConfig(**base, transport="fd")) is fd_pool
    ), "an identical config must reuse the cached fleet"
    reset_slot_pools()


def test_default_channel_factory_follows_the_handle_transport(tmp_path, monkeypatch):
    """The fleet's default client must not smuggle a token back into the picture
    on transport 1 (that token would only exist to be read out of argv)."""
    import socket as _socket
    import sys
    import types

    seen: dict = {}

    class _StubChannel:
        def __init__(self, path=None, token="", *, fd=None, timeout_ms=None):
            seen.update(path=path, token=token, fd=fd, timeout_ms=timeout_ms)

        def request(self, verb, args=None, fds=()):
            return {}

        def close(self):
            pass

    package = types.ModuleType("sandlock")
    module = types.ModuleType("sandlock.supervise")
    module.SuperviseChannel = _StubChannel
    monkeypatch.setitem(sys.modules, "sandlock", package)
    monkeypatch.setitem(sys.modules, "sandlock.supervise", module)

    worker, server = _socket.socketpair()
    try:
        fd_handle = rb.SlotHandle(
            sandbox_id="sbx_a",
            uid=20000,
            name="sbx_a",
            control_socket=worker,
            verb_timeout_s=7.5,
        )
        rb.default_channel_factory(fd_handle)
        assert seen == {
            "path": None,
            "token": "",
            "fd": worker.fileno(),
            "timeout_ms": 7500,
        }

        path_handle = rb.SlotHandle(
            sandbox_id="sbx_b",
            uid=20000,
            name="sbx_b",
            token="registered-token",
            sock_path=tmp_path / "control.sock",
        )
        seen.clear()
        rb.default_channel_factory(path_handle)
        assert seen["path"] == str(tmp_path / "control.sock")
        assert seen["token"] == "registered-token" and seen["fd"] is None
    finally:
        worker.close()
        server.close()


async def test_acquire_without_uid_takes_the_least_recently_freed(tmp_path):
    pool, spawned, log, channels = _pool(tmp_path, size=2)
    first = await pool.acquire("sbx_first", {"uid": 20000})
    second = await pool.acquire("sbx_second", {"uid": 20001})
    assert (first.uid, second.uid) == (20000, 20001)
    await pool.release("sbx_first")
    third = await pool.acquire("sbx_third", {"uid": 20000})
    assert third.uid == 20000


async def test_acquire_refuses_a_uid_outside_the_segment(tmp_path):
    pool, spawned, log, channels = _pool(tmp_path, size=2)
    with pytest.raises(
        ValueError,
        match=r"^route-B uid 9999 for sandbox sbx_a is outside the slot "
        r"segment 20000\.\.20001$",
    ):
        await pool.acquire("sbx_a", {}, uid=9999)


async def test_a_live_uid_is_never_leased_twice(tmp_path):
    """W1: recycle is a process restart, never two generations on one uid."""
    pool, spawned, log, channels = _pool(tmp_path, size=2)
    await pool.acquire("sbx_a", {}, uid=20001)
    with pytest.raises(RuntimeError, match=(
        r"^route-B uid 20001 already has a live slot \(sandbox sbx_a\); W1 "
        r"recycles a uid only by restarting its process, never by sharing it$"
    )):
        await pool.acquire("sbx_b", {}, uid=20001)
    with pytest.raises(
        ValueError,
        match=r"^sandbox sbx_a already holds a route-B slot "
        r"\(one slot per sandbox; release it first\)$",
    ):
        await pool.acquire("sbx_a", {}, uid=20000)


async def test_acquire_waits_for_the_slot_to_report_a_launched_instance(tmp_path):
    """The slot binds before launching, so ``stats`` is the readiness probe."""
    log: list = []
    pool, spawned, log, channels = _pool(tmp_path, replies={"stats": {"launched": False}})
    flipped = {"launched": False}

    def _factory(handle):
        return FakeChannel(
            str(handle.sock_path), handle.token, {"stats": dict(flipped)}, log
        )

    pool.channel_factory = _factory

    def _later():
        import time

        time.sleep(0.2)
        flipped.update({"launched": True, "pid": 99})

    threading.Thread(target=_later, daemon=True).start()
    handle = await pool.acquire("sbx_ready", {}, uid=20000)
    # The lease only returns on a launched answer, and everything it asked in
    # between was the readiness probe (never ``run``: the slot launches first).
    assert handle.instance_pid == 99
    assert [verb for verb, _, _ in log] == ["stats"] * len(log)
    assert len(log) >= 2


async def test_stale_socket_is_removed_before_the_spawn(tmp_path, caplog):
    """Registered transport only -- the fd handoff never touches the fs."""
    sock = rb._registry_sock_path(20000, "rb-sbx_stale")
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.touch()
    pool, spawned, log, channels = _pool(tmp_path, transport="path")
    with caplog.at_level("WARNING", logger="envd_service.route_b"):
        handle = await pool.acquire("sbx_stale", {}, uid=20000)
    assert handle.uid == 20000
    assert any(
        r.message.startswith("route-B slot rb-sbx_stale: removed stale socket")
        for r in caplog.records
    )


async def test_a_slot_that_dies_on_the_way_up_is_a_dead_error(tmp_path):
    """``SlotDeadError`` carries "dead" so the executor's rebuild-once path
    treats it like a dead in-process instance, and the uid goes back."""
    dead = FakeProcess(returncode=1)
    pool, spawned, log, channels = _pool(tmp_path, spawner=lambda **kw: dead)
    with pytest.raises(SlotDeadError, match=r"exited before answering on"):
        await pool.acquire("sbx_dead", {}, uid=20000)
    assert pool.live_slots == []
    # The uid went back: the next lease (a W1 restart) can use it again.
    pool2, spawned2, log2, channels2 = _pool(tmp_path)
    again = await pool2.acquire("sbx_dead", {}, uid=20000)
    assert again.uid == 20000


async def test_release_sends_shutdown_and_frees_the_uid(tmp_path):
    pool, spawned, log, channels = _pool(tmp_path)
    handle = await pool.acquire("sbx_a", {}, uid=20000)
    await pool.release("sbx_a")
    assert [verb for verb, _, _ in log] == ["stats", "shutdown"]
    assert log[1][1] is None
    assert handle.process.returncode == 0
    assert pool.acquired_uid("sbx_a") is None
    again = await pool.acquire("sbx_b", {}, uid=20000)
    assert again.uid == 20000
    await pool.release("sbx_b")
    await pool.release("sbx_b")  # idempotent


async def test_release_falls_back_to_kill_when_shutdown_is_refused(tmp_path, caplog):
    class _Refusing(FakeChannel):
        def request(self, verb, args=None, fds=()):
            self._log.append((verb, args, tuple(fds)))
            raise rb.SandlockError("connection refused")

    def _refusing(handle):
        return _Refusing(str(handle.sock_path), handle.token, {}, log)

    class _Stubborn(FakeProcess):
        """Ignores the shutdown verb *and* the reap, like a wedged slot."""

        def wait(self, timeout=None):
            import subprocess

            raise subprocess.TimeoutExpired(self.pid, timeout)

    pool, spawned, log, channels = _pool(tmp_path)
    stubborn = _Stubborn()
    handle = await pool.acquire("sbx_a", {}, uid=20000)
    handle.process = stubborn
    pool.channel_factory = _refusing
    with caplog.at_level("WARNING", logger="envd_service.route_b"):
        await pool.release("sbx_a")
    assert stubborn.killed == 1
    assert any(
        "route-B shutdown for sbx_a failed" in r.message for r in caplog.records
    )
    assert pool.acquired_uid("sbx_a") is None


# ------------------------------------------------------------------ instance shim


def _instance(tmp_path, replies=None, log=None):
    replies = replies or {}
    log = log if log is not None else []
    handle = rb.SlotHandle(
        sandbox_id="sbx_a",
        uid=20000,
        name="sbx_a",
        token="tok",
        sock_path=tmp_path / "control.sock",
        policy_path=tmp_path / "policy.json",
        program_path=tmp_path / "program.json",
        process=FakeProcess(),
    )

    def _factory(handle):
        return FakeChannel(str(handle.sock_path), handle.token, replies, log)

    pool = W1SlotPool(
        uid_start=20000,
        size=1,
        tmp_root=tmp_path / "slots",
        supervise_bin=tmp_path / "bin",
        channel_factory=_factory,
    )
    inst = RouteBInstance(pool=pool, handle=handle, channel_factory=_factory)
    return inst, log, handle


def _closed_fd(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_exec_sends_one_verb_with_params_and_three_child_ends(tmp_path):
    inst, log, handle = _instance(
        tmp_path, {"exec": {"child_id": 3, "pid": 1234}}
    )
    proc = inst.exec(
        ["/bin/sh", "-c", "true"],
        rb.ExecStdioPIPED if hasattr(rb, "ExecStdioPIPED") else 1,
        cwd="/workspace",
        env={"PATH": "/bin"},
        clean_env=True,
        bind_ports=[50006],
    )
    assert log == [
        (
            "exec",
            {
                "argv": ["/bin/sh", "-c", "true"],
                "cwd": "/workspace",
                "env": {"PATH": "/bin"},
                "clean_env": True,
                "bind_ports": [50006],
            },
            log[0][2],
        )
    ]
    assert len(log[0][2]) == 3
    assert (proc.child_id, proc.pid) == (3, 1234)
    assert proc.stdout is not None and proc.stderr is not None
    assert proc.pty is None
    proc.close()


def test_exec_closes_its_own_copies_so_output_reaches_eof(tmp_path):
    """Holding the child's write ends would keep our own pipes open forever:
    ``_drive`` waits for output EOF before the exit code, so an un-closed copy
    hangs the command at zero output."""
    inst, log, handle = _instance(
        tmp_path, {"exec": {"child_id": 4, "pid": 1235}}
    )
    proc = inst.exec(["/bin/cat"], 1)
    child_stdin_fd, child_stdout_fd, child_stderr_fd = log[0][2]
    assert (_closed_fd(child_stdin_fd), _closed_fd(child_stdout_fd),
            _closed_fd(child_stderr_fd)) == (True, True, True)

    channel = inst._channel
    child_stdin_dup, child_stdout_dup, child_stderr_dup = channel.dups
    # The child end of stdout is a write end; ours is the read end.
    os.write(child_stdout_dup, b"hello")
    os.write(child_stderr_dup, b"boom")
    os.close(child_stdout_dup)
    os.close(child_stderr_dup)
    assert proc.stdout.read() == b"hello"
    assert proc.stderr.read() == b"boom"
    os.close(child_stdin_dup)
    proc.close()


def test_exec_writes_go_to_the_child_stdin_end(tmp_path):
    inst, log, handle = _instance(tmp_path, {"exec": {"child_id": 5, "pid": 1236}})
    proc = inst.exec(["/bin/cat"], 1)
    channel = inst._channel
    child_stdin_dup = channel.dups[0]
    proc.stdin.write(b"ping\n")
    assert os.read(child_stdin_dup, 16) == b"ping\n"
    channel.dups = []
    proc.close()
    assert _closed_fd(child_stdin_dup) is False  # the "child" still holds it
    os.close(child_stdin_dup)


def test_exec_pty_mode_keeps_the_master_and_drops_the_slave(tmp_path, monkeypatch):
    """PTY exec is worker-side: we own the master, the child owns the slave."""
    import termios

    inst, log, handle = _instance(tmp_path, {"exec": {"child_id": 6, "pid": 1237}})
    proc = inst.exec(["/bin/sh"], 3)
    assert (proc.stdout, proc.stderr) == (None, None)
    assert proc.pty is not None
    child_stdin_fd, child_stdout_fd, child_stderr_fd = log[0][2]
    assert child_stdin_fd == child_stdout_fd == child_stderr_fd
    assert _closed_fd(child_stdin_fd) is True

    # ``resize`` must be a TIOCSWINSZ on *our* master fd with the requested
    # size; reading the value back off a macOS ptmx is not trustworthy (the
    # size a fresh pair reports is stale kernel state), so the call itself is
    # what is asserted here. The end-to-end pty round trip is covered by the
    # root-gated contract test on Linux.
    import fcntl
    import struct

    calls = []

    def _fake_ioctl(fd, request, *rest):
        calls.append((fd, request, rest))
        return b"\0" * 8

    monkeypatch.setattr(fcntl, "ioctl", _fake_ioctl)
    proc.resize(40, 120)
    monkeypatch.undo()
    assert len(calls) == 1
    fd, request, rest = calls[0]
    assert fd == proc.pty.fileno() == proc._pty_master_fd
    assert request == termios.TIOCSWINSZ
    assert struct.unpack("HHHH", rest[0]) == (40, 120, 0, 0)
    assert os.isatty(proc.pty.fileno()) is True
    proc.close()


def test_exec_failure_closes_every_descriptor_it_created(tmp_path):
    boom = rb.SandboxError("instance exec failed: no such file")
    inst, log, handle = _instance(tmp_path, {"exec": boom})
    with pytest.raises(rb.SandboxError, match="no such file"):
        inst.exec(["/nonexistent"], 1)
    assert all(_closed_fd(fd) for fd in log[0][2])


def test_exec_pty_failure_closes_master_and_slave(tmp_path):
    boom = rb.SandboxError("instance exec failed: refused")
    inst, log, handle = _instance(tmp_path, {"exec": boom})
    with pytest.raises(rb.SandboxError, match="refused"):
        inst.exec(["/bin/sh"], 3)
    assert all(_closed_fd(fd) for fd in log[0][2])


def test_kill_child_carries_the_signal_number(tmp_path):
    inst, log, handle = _instance(tmp_path, {"exec": {"child_id": 8, "pid": 1238}})
    proc = inst.exec(["/bin/sh"], 1)
    inst._channel.dups = []
    proc.kill(19)
    assert log[-1] == ("kill_child", {"child_id": 8, "signum": 19}, ())
    proc.kill(9)
    assert log[-1] == ("kill_child", {"child_id": 8, "signum": 9}, ())


def test_wait_polls_liveness_before_the_slot_blocking_verb(tmp_path, monkeypatch):
    """``wait_child`` monopolizes the slot's single-threaded accept loop, so
    it is issued only once the child's host pid is gone."""
    inst, log, handle = _instance(
        tmp_path,
        {
            "exec": {"child_id": 9, "pid": 1239},
            "wait_child": {"code": 0, "signal": None, "killed": False,
                           "timed_out": False},
        },
    )
    proc = inst.exec(["/bin/true"], 1)
    inst._channel.dups = []
    states = iter([True, True, False])
    monkeypatch.setattr(
        rb.RouteBExecProcess, "_child_alive", lambda self: next(states)
    )
    assert proc.wait().exit_code == 0
    assert log[-1][0] == "wait_child"
    assert log[-1][1] == {"child_id": 9}


def test_wait_maps_every_non_code_exit_to_minus_one(tmp_path, monkeypatch):
    """Parity with ``sandlock_result_exit_code`` (= ``code().unwrap_or(-1)``):
    signal/kill/timeout never become a synthetic 128+N here."""
    for reply, expected in (
        ({"code": 7, "signal": None, "killed": False, "timed_out": False}, 7),
        ({"code": None, "signal": 9, "killed": False, "timed_out": False}, -1),
        ({"code": None, "signal": None, "killed": True, "timed_out": False}, -1),
        ({"code": None, "signal": None, "killed": False, "timed_out": True}, -1),
    ):
        inst, log, handle = _instance(
            tmp_path, {"exec": {"child_id": 10, "pid": 1240}, "wait_child": reply}
        )
        proc = inst.exec(["/bin/true"], 1)
        inst._channel.dups = []
        monkeypatch.setattr(rb.RouteBExecProcess, "_child_alive", lambda self: False)
        assert proc.wait().exit_code == expected
        # Idempotent: a second wait replays the cached result, no extra verb.
        before = len(log)
        assert proc.wait().exit_code == expected
        assert len(log) == before


def test_update_network_reports_stale_children_and_maps_refusals(tmp_path):
    inst, log, handle = _instance(
        tmp_path, {"update_network": {"stale_child_ids": [1, 4]}}
    )
    assert inst.update_network(["198.18.0.99"]) == [1, 4]
    assert log[-1] == ("update_network", {"ips": ["198.18.0.99"]}, ())

    refusing = _instance(tmp_path, {"update_network": rb.SandboxError("outside ceiling")})
    with pytest.raises(PermissionError, match="outside ceiling"):
        refusing[0].update_network([])


def test_transport_loss_is_reported_as_a_dead_instance(tmp_path):
    """The executor rebuilds once on a message containing "dead"; a lost slot
    is exactly that case."""
    inst, log, handle = _instance(tmp_path, {"exec": rb.SandlockError("ECONNREFUSED")})
    with pytest.raises(SlotDeadError, match=r"is dead: verb 'exec' lost the slot"):
        inst.exec(["/bin/true"], 1)


def test_a_client_side_channel_failure_is_not_a_policy_refusal(tmp_path):
    """The F16 client's error path is broken (it raises ``AttributeError``
    instead of ``SandlockError`` on a refused connect), and a broken channel
    must still read as "slot dead" rather than as an opaque failure or as a
    served refusal: the executor's rebuild-once keys off the dead class.
    """

    class _BrokenChannel:
        def __init__(self, handle):
            self.handle = handle

        def request(self, verb, args=None, fds=()):
            raise AttributeError(
                "'_ctypes.CArgObject' object has no attribute 'contents'"
            )

        def close(self):
            pass

    inst, log, handle = _instance(tmp_path)
    inst._channel_factory = _BrokenChannel
    with pytest.raises(SlotDeadError, match="lost the slot: AttributeError"):
        inst.request("stats")


def test_verbs_after_close_never_reach_a_released_slot(tmp_path):
    inst, log, handle = _instance(
        tmp_path, {"shutdown": {}, "exec": {"child_id": 1, "pid": 1}}
    )
    inst.close()
    assert log == [("shutdown", None, ())]
    assert handle.process.returncode == 0
    with pytest.raises(RuntimeError, match=r"route-B instance sbx_a is closed"):
        inst.stats()
    inst.close()  # idempotent: no second shutdown verb
    assert [v for v, _, _ in log] == ["shutdown"]


def test_verb_on_an_exited_slot_is_dead(tmp_path):
    inst, log, handle = _instance(tmp_path, {"stats": {"launched": True}})
    handle.process.returncode = -9
    with pytest.raises(SlotDeadError, match=r"slot sbx_a \(pid 4242\) exited"):
        inst.stats()
