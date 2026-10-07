"""The SandlockExecutor driving a real supervise slot (route B, end to end).

``tests/contract/test_own_identity_slot_pool.py`` proves the slot pool and the
Python channel client work at two distinct uids;
``tests/contract/test_uid_permissions.py`` proves the mediated-write ownership
through the HTTP/worker surface. This file is the layer in between: the
executor itself, with the same ``start()`` / PTY / stdin / ``close()`` calls the
process manager makes, against a slot that really runs as the sandbox host uid.

What is only checkable here (not off-Linux, not with a faked fleet):

* the generation's parking main program costs nothing -- a parked tree that
  burns CPU would tax every route-B sandbox for its whole lifetime;
* the child really executes as the leased uid, including through PTY mode;
* PTY window sizes set on the worker's master reach the child;
* ``close()`` leaves the uid clean: process gone, registered socket gone
  (W1 recycles a uid only after a clean slate), and the next generation can
  take the same uid again.

Needs a root worker (starting a slot at another uid needs privilege), Linux,
and the supervise binary from the wheel.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor
from envd_service.own_identity import (
    PARKING_SCRIPT,
    OwnIdentityConfig,
    reset_slot_pools,
    slot_pool_for,
)
from tests.security.conftest import sandlock_ready

UID = 21200

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not sandlock_ready(),
    reason=(
        "route-B executor tests need a root worker, Linux and the sandlock "
        "wheel's supervise binary (privileged Docker test runner)"
    ),
)


def _clock_ticks(*pids: int) -> int:
    """Summed utime+stime (in clock ticks) of the given processes.

    Read from ``/proc`` rather than ``os.times()`` because the sandbox tree
    belongs to another uid; a root worker may still read its /proc entries.
    """
    total = 0
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        # After the comm field: state, ppid, ... utime is the 12th, stime 13th.
        total += int(fields[11]) + int(fields[12])
    return total


def _proc_state(pid: int) -> str:
    """``"running"``, ``"zombie"`` (ended, not yet reaped) or ``"gone"``.

    Distinguishing the last two matters: the runner's pid 1 does not always
    reap an adopted orphan, so a *deleted* worker leaves its exited slot behind
    as a zombie, which still has a /proc entry.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return "gone"
    state = stat.rsplit(")", 1)[1].split()[0]
    return "zombie" if state == "Z" else "running"


def _tree_pids(root_pid: int) -> list[int]:
    """``root_pid`` plus every descendant visible through ``/proc``."""
    pids = [root_pid]
    stack = [root_pid]
    while stack:
        parent = stack.pop()
        try:
            children = Path(f"/proc/{parent}/task/{parent}/children").read_text()
        except OSError:
            children = ""
        for child in children.split():
            pids.append(int(child))
            stack.append(int(child))
    return pids


def _executor(workspace: Path, sandbox_id: str) -> SandlockExecutor:
    """A pure-shape executor forced onto route B (``mode="on"``).

    The chroot shape's identity evidence is covered by
    ``test_uid_permissions``; forcing route B here keeps the slot behaviour
    under test independent of whether the runner pulled a base image.
    """
    # The workspace arrives on the runner's ownership-capable storage (the
    # ``workspace`` fixture), and it -- like every ancestor of a slot's paths
    # -- has to stay traversable by a *foreign* uid: a 0700 parent is an EACCES
    # for the slot's own Landlock ruleset and policy document.
    os.chown(workspace, UID, UID)
    os.chmod(workspace, 0o700)
    scratch = workspace.parent / "route-b" / sandbox_id
    return SandlockExecutor(
        workspace_dir=str(workspace),
        base_image=None,
        image_rootfs=None,
        host_uid=UID,
        per_sandbox_uid=True,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=1024,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=sandbox_id,
        own_identity=OwnIdentityConfig(
            mode="on",
            uid_start=UID,
            uid_size=2,
            tmp_root=scratch,
        ),
    )


async def _collect(running) -> tuple[int, bytes, bytes]:
    out = {"stdout": [], "stderr": []}
    async for kind, chunk in running.output():
        if kind in out:
            out[kind].append(chunk)
    code = await running.exit_code()
    return code, b"".join(out["stdout"]), b"".join(out["stderr"])


def _config(cmd: list[str], cwd: str, **over) -> ExecConfig:
    cfg = {"cmd": cmd, "env": {}, "cwd": cwd, "stdin_enabled": False}
    cfg.update(over)
    return ExecConfig(**cfg)


async def test_executor_command_runs_in_the_leased_generation(workspace) -> None:
    """The command runs in the slot leased for this sandbox, carrying both of
    the identities it is supposed to carry.

    Inside its namespace the workload is uid 0 (fork F18's self-map: the same
    in-guest identity a privileged supervisor produces by writing `0 -> host_uid`
    for the child), while on the host everything it writes belongs to the
    sandbox's own uid -- the fact route B exists to establish. Asserting only
    one of the two would let the other regress unnoticed: no self-map and the
    guest sees its host uid; a map without the dropped privilege and the writes
    come back owned by root.
    """
    ex = _executor(workspace, "sbx_rbe_id")
    try:
        assert ex._own_identity_active is True
        running = await ex.start(
            _config(
                [
                    "/bin/sh",
                    "-c",
                    "id -u; id -g; echo done > from-command.txt",
                ],
                str(workspace),
            )
        )
        code, out, err = await _collect(running)
        assert (code, out, err) == (0, b"0\n0\n", b"")
        made = (workspace / "from-command.txt").stat()
        assert (made.st_uid, made.st_gid) == (UID, UID), (
            f"the write landed as {made.st_uid}:{made.st_gid}, not the leased "
            f"{UID}:{UID}: the namespace map is not carrying the host identity"
        )
        # The mediator itself: the slot process runs as the leased uid, not as
        # root -- what makes the ownership above a DAC fact instead of a
        # mediation artifact.
        slot = slot_pool_for(ex._own_identity).slot("sbx_rbe_id")
        assert slot is not None
        status = Path(f"/proc/{slot.process.pid}/status").read_text()
        uid_line = [line for line in status.splitlines() if line.startswith("Uid:")][0]
        assert uid_line.split() == ["Uid:", str(UID), str(UID), str(UID), str(UID)]
    finally:
        ex.close()


async def test_sandbox_written_file_is_owned_by_the_sandbox_uid(workspace) -> None:
    """T5's ownership rule at the executor layer: the file the command creates
    is owned by the sandbox's host uid, and the sandbox may chmod its own
    file (a supervisor-tier mediator made both impossible)."""
    ex = _executor(workspace, "sbx_rbe_owner")
    try:
        running = await ex.start(
            _config(
                ["/bin/sh", "-c", "printf DATA > owned.txt && chmod 640 owned.txt"],
                str(workspace),
            )
        )
        code, out, err = await _collect(running)
        assert (code, out, err) == (0, b"", b"")
        meta = (workspace / "owned.txt").stat()
        assert meta.st_uid == UID
        assert stat.S_IMODE(meta.st_mode) == 0o640
        assert (workspace / "owned.txt").read_text() == "DATA"
    finally:
        ex.close()


async def test_parked_main_program_costs_nothing(workspace) -> None:
    """The generation's M0 is a park, not a workload.

    The obvious ``read x < /dev/zero`` spins at 100 % CPU for the whole
    sandbox lifetime (an exec session's main stdio is /dev/null, so ``read``
    never terminates a line); the self-stop park must not move the clock.
    """
    ex = _executor(workspace, "sbx_rbe_park")
    pool = slot_pool_for(ex._own_identity)
    try:
        running = await ex.start(_config(["/bin/true"], str(workspace)))
        await _collect(running)
        slot = pool.slot("sbx_rbe_park")
        tree = _tree_pids(slot.process.pid)
        before = _clock_ticks(*tree)
        time.sleep(1.0)
        after = _clock_ticks(*_tree_pids(slot.process.pid))
        assert after - before <= 2, (
            f"the parked generation burned {after - before} clock ticks in "
            "1 s of wall time; route-B slots must park at zero cost"
        )
    finally:
        ex.close()


#: The fork's unified closed-instance refusal, byte for byte
#: (``sandlock-core/src/error.rs``), as it reaches the worker: the slot formats
#: a served failure as ``instance exec failed: {e}`` and the core renders the
#: runtime error as ``process error: {…}``. Pinned exactly on purpose -- the
#: executor must classify this *without* reading the sentence (it branches on
#: the refusal's stable ``code`` instead), so this string is here to document
#: what the channel now carries *next to* the code, not to license a text
#: match.
INSTANCE_CLOSED_REFUSAL = (
    "instance exec failed: process error: instance is closed (shut down, or "
    "the init channel closed after the main-exit container end); no new work "
    "is accepted"
)


def _raw_state(pid: int) -> str:
    """The bare ``/proc`` state char (``S``/``T``/``Z``/…), unlike
    :func:`_proc_state` which folds "stopped" into "running"."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return "gone"


def test_parked_main_survives_stray_catchable_signals() -> None:
    """The park must not be killable by a *catchable* signal.

    A stopped process keeps a catchable signal pending and delivers it on the
    next SIGCONT, so a park without the ``trap ''`` prologue dies the moment
    anything resumes it. Measured in the frozen image: the unprefixed script
    exits 143 (SIGTERM) on SIGCONT after SIGTERM-while-stopped; the shipped one
    is still parked and still costs zero clock ticks.

    That distinction is the whole ballgame for a route-B sandbox: the M0 main
    exiting is a *container end* (``ChildKind::Main``), after which every verb
    on that generation -- every later command of that sandbox -- is refused
    with the unified closed-instance code.
    """
    proc = subprocess.Popen(
        ["/bin/sh", "-c", PARKING_SCRIPT], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _raw_state(proc.pid) != "T":
            time.sleep(0.02)
        assert _raw_state(proc.pid) == "T", (
            "the park must stop itself (the zero-CPU property depends on it)"
        )
        before = _clock_ticks(proc.pid)
        for signum in (
            signal.SIGTERM,
            signal.SIGHUP,
            signal.SIGINT,
            signal.SIGQUIT,
            signal.SIGUSR1,
            signal.SIGUSR2,
        ):
            os.kill(proc.pid, signum)
        os.kill(proc.pid, signal.SIGCONT)
        time.sleep(0.5)
        assert proc.poll() is None, (
            f"the parked main exited from a catchable signal "
            f"(returncode={proc.returncode})"
        )
        assert _raw_state(proc.pid) == "T", "the park must re-stop after SIGCONT"
        after = _clock_ticks(proc.pid)
        assert after - before <= 2, (
            f"the park burned {after - before} clock ticks while idle"
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)


async def test_collapsed_generation_is_rebuilt_once_not_permanent(
    workspace, caplog
) -> None:
    """A collapsed generation must cost one rebuild, not the sandbox's life.

    The generation is a container: when its M0 main exits, init collapses
    every group and the *slot process keeps serving* -- answering every verb
    with the unified closed-instance refusal. That refusal crosses the route-B
    channel as ``err`` prose plus a stable ``code`` (fork F19/SL-13); before
    the code it was prose only, the executor's typed session-gone mapping
    never saw it, and the sandbox stayed dead for good (production symptom:
    ``error_type=SandboxError`` on a sandbox's *first* command, every later one
    the same). Here the main is killed the way any stray SIGKILL to it would
    (the container end is the same either way), and the *next* command must
    run -- on a generation the executor rebuilt.
    """
    from sandlock.exceptions import SlotRefusal

    sandbox_id = "sbx_rbe_collapse"
    ex = _executor(workspace, sandbox_id)
    pool = slot_pool_for(ex._own_identity)
    try:
        running = await ex.start(_config(["/bin/true"], str(workspace)))
        assert (await _collect(running))[0] == 0
        first_slot = pool.slot(sandbox_id)
        assert first_slot is not None
        main_pid = first_slot.instance_pid
        assert isinstance(main_pid, int) and main_pid > 0

        # End the container: the M0 main dies, init collapses and exits. The
        # slot process is untouched -- that is precisely the shape that made
        # this permanent.
        os.kill(main_pid, signal.SIGKILL)
        deadline = time.monotonic() + 30.0
        stats: dict = {}
        while time.monotonic() < deadline:
            stats = await asyncio.to_thread(ex._instance.stats)
            if stats.get("instance_state") == "Exited":
                break
            time.sleep(0.05)
        assert stats.get("instance_state") == "Exited", (
            f"the generation never read Exited after its main was killed: {stats}"
        )

        # The refused verb: the same prose the worker always received, plus
        # the code that says *why* -- there is no `stats` round trip here.
        with pytest.raises(SlotRefusal) as refusal:
            await asyncio.to_thread(ex._instance.exec, ["/bin/true"])
        assert str(refusal.value) == INSTANCE_CLOSED_REFUSAL
        assert refusal.value.code == "generation_closed", refusal.value.code

        # ...and the executor turns that into a rebuild plus one retry.
        with caplog.at_level(
            "INFO", logger="envd_service.executors.sandlock"
        ):
            second = await ex.start(_config(["/bin/true"], str(workspace)))
        assert (await _collect(second))[0] == 0
        assert [
            record.message
            for record in caplog.records
            if record.message.startswith("sandlock instance closed during exec")
        ] == [
            "sandlock instance closed during exec; rebuilding once "
            f"sandbox_id={sandbox_id} instance_name={sandbox_id} "
            "argv=['/bin/true']"
        ]
        rebuilt = pool.slot(sandbox_id)
        assert rebuilt is not None and rebuilt is not first_slot
        assert rebuilt.instance_pid != main_pid, (
            "the retry must run on a fresh generation, not the collapsed one"
        )
    finally:
        ex.close()


async def test_live_generation_refusal_is_not_rebuilt(
    workspace, monkeypatch, caplog
) -> None:
    """The complement of the pin above: a *Live* session's refusal stands.

    A per-exec parameter wider than the instance ceiling is a policy refusal,
    not a gone session (``sandlock-supervise`` answers ``exec params exceed the
    instance policy ceiling: bind_ports 65000 is outside the allowed set
    (EPERM)``, with the stable code ``policy_denied``). Rebuilding on it would
    silently retry a command the deployment refused, so the code must not be
    one of the two session-gone values and the error must reach the caller
    unchanged.
    """
    from sandlock.exceptions import SlotRefusal

    sandbox_id = "sbx_rbe_refusal"
    ex = _executor(workspace, sandbox_id)
    pool = slot_pool_for(ex._own_identity)
    try:
        running = await ex.start(_config(["/bin/true"], str(workspace)))
        assert (await _collect(running))[0] == 0
        before_slot = pool.slot(sandbox_id)
        assert before_slot is not None
        monkeypatch.setattr(
            type(ex), "_bind_ports_for", lambda self, config: [65000]
        )
        with caplog.at_level("INFO", logger="envd_service.executors.sandlock"):
            with pytest.raises(SlotRefusal) as refusal:
                await ex.start(_config(["/bin/true"], str(workspace)))
        assert str(refusal.value) == (
            "instance exec failed: process error: exec params exceed the "
            "instance policy ceiling: bind_ports 65000 is outside the allowed "
            "set (EPERM)"
        )
        assert refusal.value.code == "policy_denied", refusal.value.code
        assert [
            record.message
            for record in caplog.records
            if record.message.startswith("sandlock instance ")
            and "rebuilding once" in record.message
        ] == []
        assert pool.slot(sandbox_id) is before_slot
        assert _proc_state(before_slot.process.pid) == "running"
    finally:
        ex.close()


def _generation_init_pid(slot_pid: int, main_pid: int) -> int:
    """The ``sandlock-init`` of the generation running under ``slot_pid``.

    The tree is ``slot -> sandlock-init -> M0 main``: init is the slot's
    direct child that is not the main. Killing init is the *machinery*
    failure (its control link dies with no main-exit frame), i.e. the shape
    that must read ``Dead`` -- as opposed to killing the main, which is a
    clean container end and reads ``Exited``.
    """
    children = Path(f"/proc/{slot_pid}/task/{slot_pid}/children").read_text().split()
    for child in children:
        if int(child) != main_pid:
            return int(child)
    raise AssertionError(
        f"slot {slot_pid} has no init child besides the main {main_pid}: "
        f"{children}"
    )


async def test_dead_generation_refusal_is_coded_and_rebuilt_once(
    workspace, caplog
) -> None:
    """A *dead* generation's refusal is coded ``generation_dead`` and rebuilt.

    The machinery form, next to the clean ``Exited`` one above: killing
    ``sandlock-init`` ends the generation's control link with no main-exit
    frame, so the session reads ``Dead`` and every later verb is refused with
    the dead-instance code (``sandlock-core/src/error.rs``,
    ``SandboxRuntimeError::InstanceDead``). The slot keeps serving, so the
    refusal arrives as prose + code -- and the executor's recovery is the
    same rebuild-once it applies to a *typed* dead session, exactly as it was
    when the classification had to ask for ``stats`` first.
    """
    from sandlock.exceptions import SlotRefusal

    sandbox_id = "sbx_rbe_dead"
    ex = _executor(workspace, sandbox_id)
    pool = slot_pool_for(ex._own_identity)
    try:
        running = await ex.start(_config(["/bin/true"], str(workspace)))
        assert (await _collect(running))[0] == 0
        first_slot = pool.slot(sandbox_id)
        assert first_slot is not None
        main_pid = first_slot.instance_pid
        assert isinstance(main_pid, int) and main_pid > 0
        init_pid = _generation_init_pid(first_slot.process.pid, main_pid)

        os.kill(init_pid, signal.SIGKILL)
        deadline = time.monotonic() + 30.0
        stats: dict = {}
        while time.monotonic() < deadline:
            stats = await asyncio.to_thread(ex._instance.stats)
            if stats.get("instance_state") == "Dead":
                break
            time.sleep(0.05)
        assert stats.get("instance_state") == "Dead", (
            f"the generation never read Dead after its init was killed: {stats}"
        )

        with pytest.raises(SlotRefusal) as refusal:
            await asyncio.to_thread(ex._instance.exec, ["/bin/true"])
        assert refusal.value.code == "generation_dead", refusal.value.code
        assert str(refusal.value) == (
            "instance exec failed: process error: instance is dead "
            "(listener/reaper/control-channel failure); every verb returns "
            "this code and the instance is never silently relaunched"
        )

        with caplog.at_level("INFO", logger="envd_service.executors.sandlock"):
            second = await ex.start(_config(["/bin/true"], str(workspace)))
        assert (await _collect(second))[0] == 0
        assert [
            record.message
            for record in caplog.records
            if record.message.startswith("sandlock instance ")
            and "rebuilding once" in record.message
        ] == [
            "sandlock instance dead during exec; rebuilding once "
            f"sandbox_id={sandbox_id} instance_name={sandbox_id} "
            "argv=['/bin/true']"
        ]
        rebuilt = pool.slot(sandbox_id)
        assert rebuilt is not None and rebuilt is not first_slot
        assert rebuilt.instance_pid != main_pid, (
            "the retry must run on a fresh generation, not the dead one"
        )
    finally:
        ex.close()


async def test_pty_stdio_and_window_size_reach_the_child(workspace) -> None:
    ex = _executor(workspace, "sbx_rbe_pty")
    try:
        running = await ex.start(
            _config(
                ["/bin/sh", "-c", "stty size; printf pty-out"],
                str(workspace),
                pty=True,
                rows=44,
                cols=132,
            )
        )
        chunks = []
        async for kind, chunk in running.output():
            chunks.append((kind, chunk))
        code = await running.exit_code()
        assert code == 0
        stream = b"".join(chunk for kind, chunk in chunks if kind == "pty")
        # The pty line discipline turns the child's \n into \r\n (ONLCR), so
        # the window size the *worker's* master was resized to comes back
        # through the child's ``stty`` exactly as set.
        assert stream == b"44 132\r\npty-out"
    finally:
        ex.close()


async def test_stdin_round_trip_and_kill_by_signal(workspace) -> None:
    ex = _executor(workspace, "sbx_rbe_stdin")
    try:
        running = await ex.start(
            _config(["/bin/cat"], str(workspace), stdin_enabled=True)
        )
        running.send_stdin(b"echo me\n")
        running.close_stdin()
        code, out, err = await _collect(running)
        assert (code, out, err) == (0, b"echo me\n", b"")

        # A slot child is signalled by number: SIGSTOP actually stops it,
        # which the in-process SIGKILL-only kill cannot do (FUP #8).
        stopped = await ex.start(
            _config(["/bin/sh", "-c", "while :; do sleep 0.05; done"], str(workspace))
        )
        assert stopped.supports_signal_pause is True
        stopped.kill(19)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = Path(f"/proc/{stopped.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "T":
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"child pid {stopped.pid} was not stopped by SIGSTOP")
        stopped.kill(9)
        code, out, err = await _collect(stopped)
        assert code == -1  # a signal death reports -1, never 128+9
    finally:
        ex.close()


async def test_close_leaves_no_slot_and_the_uid_is_reusable(workspace) -> None:
    """W1 recycle is a *clean* restart: process, channel and control-dir
    residue all go before the uid may serve another generation."""
    ex = _executor(workspace, "sbx_rbe_recycle")
    pool = slot_pool_for(ex._own_identity)
    first = ex._ensure_instance()
    pid = first._handle.process.pid
    # Transport 1: there is no socket path and no token to leave behind.
    assert first.sock_path is None and first._handle.token is None
    worker_fd = first._handle.control_socket.fileno()
    ex.close()

    deadline = time.monotonic() + 10.0
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists(), f"slot pid {pid} survived close()"
    with pytest.raises(OSError):
        os.fstat(worker_fd)
    ctl_root = Path(f"/tmp/sandlock-ctl-{UID}")
    residue = sorted(str(x) for x in ctl_root.rglob("control.sock")) if ctl_root.exists() else []
    assert residue == [], f"the generation left its control socket behind: {residue}"
    assert pool.acquired_uid("sbx_rbe_recycle") is None

    # Same uid, next generation (the pool still holds the segment).
    second = _executor(workspace, "sbx_rbe_recycle2")
    try:
        assert second._host_uid == UID
        running = await second.start(_config(["/bin/echo", "again"], str(workspace)))
        code, out, err = await _collect(running)
        assert (code, out, err) == (0, b"again\n", b"")
        assert second._ensure_instance()._handle.uid == UID
    finally:
        second.close()


async def test_missing_binary_exits_127_through_the_slot(workspace) -> None:
    """A missing in-sandbox executable is the child's exit status (127, no
    output), not an exception -- the same fork execvp semantics the in-process
    instance has (``test_sandbox_lifecycle_rebuild``), now pinned across the
    channel because route B is the chroot shape's default backend.

    The path sits **inside** the sandbox's readable set on purpose. N15 gave
    the pure shape a real policy (the host root, identity translation), so a
    path outside it is *refused* (EACCES) rather than reported as missing --
    that refusal is what stops a sandbox probing host paths for existence, and
    it is the same answer a ``stat`` of that path gets. Both halves are pinned
    below so the difference stays deliberate.
    """
    ex = _executor(workspace, "sbx_rbe_127")
    try:
        running = await ex.start(_config(["/usr/bin/e2b-no-such-binary"], str(workspace)))
        code, out, err = await _collect(running)
        assert (code, out, err) == (127, b"", b"")

        running = await ex.start(_config(["/nonexistent-e2b-bin"], str(workspace)))
        code, out, err = await _collect(running)
        assert (code, out, err) == (
            127,
            b"",
            b'sandlock-init: exec "/nonexistent-e2b-bin" failed (errno 13)\n',
        )
    finally:
        ex.close()


async def test_the_missing_binary_contract_survives_the_diagnostic_trace(
    monkeypatch, workspace
) -> None:
    """127 with no output, whether or not the fork's diagnostic trace is on.

    Measured 2026-09-23: the fork's exec-failure path read `errno` twice -- once
    for the breadcrumb, then again *after* ``realroot::record_failure()`` had
    tried to open its trace file. When that open is denied (the default path is
    ``/tmp/sandlock-real-root-error`` and the sandbox's ruleset grants no write
    to ``/tmp``), the second read returned the *open's* EACCES, so a missing
    binary reported ``sandlock-init: exec "/nonexistent-e2b-bin" failed
    (errno 13)`` on the guest's stderr. The trace being *on* hid it (``note()``
    runs just before ``execvp`` and opens the file first, leaving errno alone
    afterwards), which is why the N35 lane -- it sets that variable -- never saw
    this while phase 1 of ``deploy/scripts/test-prod-shaped.sh`` did.

    Both settings are pinned here instead of trusting the environment: the
    variable reaches the slot because the pool spawns it from this process's
    environment, and each iteration leases a fresh sandbox id so it gets a
    freshly spawned slot.

    The probe path is inside the sandbox's readable set, for the reason given
    in ``test_missing_binary_exits_127_through_the_slot``: N15 refuses paths
    outside it with EACCES, and this case is about the *errno the kernel
    reported*, which a refusal would replace.
    """
    for trace_path in (None, "/tmp/e2b-contract-trace"):
        if trace_path is None:
            monkeypatch.delenv("SANLOCK_REALROOT_TRACE", raising=False)
            sandbox_id = "sbx_rbe_127_trace_off"
        else:
            monkeypatch.setenv("SANLOCK_REALROOT_TRACE", trace_path)
            sandbox_id = "sbx_rbe_127_trace_on"
        ex = _executor(workspace, sandbox_id)
        try:
            running = await ex.start(
                _config(["/usr/bin/e2b-no-such-binary"], str(workspace))
            )
            code, out, err = await _collect(running)
            assert (code, out, err) == (127, b"", b"")
        finally:
            ex.close()


async def test_concurrent_commands_share_one_slot_without_stalling(workspace) -> None:
    """A slot answers one verb at a time -- that must not turn into one command
    at a time.

    Both transports serialise: ``serve_registered_path`` accepts sequentially,
    and the fd transport guards its single persistent stream. So a
    ``wait_child`` issued while its child is still running would park the whole
    generation. The contract that keeps this usable is (a) the executor never
    waits on a live child (it watches the host pid first) and (b) a long-lived
    child -- the shape of an in-sandbox MCP gateway -- does not block the
    commands that arrive after it.
    """
    ex = _executor(workspace, "sbx_rbe_concurrent")
    try:
        sleeper = await ex.start(
            _config(["/bin/sh", "-c", "sleep 30"], str(workspace))
        )
        for index in range(3):
            running = await ex.start(
                _config(
                    ["/bin/sh", "-c", f"printf c{index}"],
                    str(workspace),
                )
            )
            code, out, err = await _collect(running)
            assert (code, out, err) == (0, f"c{index}".encode(), b"")

        # The parked child is still there, and its exit is collectable once it
        # is signalled -- through the same sequential channel.
        assert Path(f"/proc/{sleeper.pid}").exists()
        sleeper.kill(9)
        code = await sleeper.exit_code()
        assert code == -1
    finally:
        ex.close()


LEASE_HELPER = '''
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, "/workspace")
from envd_service.own_identity import W1SlotPool

async def main():
    policy = json.loads(Path(sys.argv[1]).read_text())
    pool = W1SlotPool(uid_start=int(sys.argv[2]), size=1,
                      tmp_root=Path(sys.argv[3]) / "slots")
    handle = await pool.acquire("sbx_orphan_probe", policy)
    print(handle.process.pid, flush=True)
    await asyncio.sleep(600)

asyncio.run(main())
'''


def test_worker_death_ends_the_generation(workspace) -> None:
    """Transport 1's lifecycle guarantee: the slot's control stream is owned by
    the worker that leased it, so a worker that dies takes the generation with
    it instead of leaving a live, unattended sandbox.

    Over the registered transport the socket file outlives the worker and the
    slot keeps serving whoever presents the token; that is exactly the
    orphan shape this transport removes.
    """
    import subprocess
    import sys

    ex = _executor(workspace, "sbx_orphan_owner")
    try:
        policy_path = workspace / "lease-policy.json"
        # A minimal ceiling the slot can read; the lease is only about the
        # channel, not about what the sandbox may do.
        policy_path.write_text(json.dumps({
            "fs_readable": ["/usr", "/lib", "/lib64", "/bin", "/etc"],
            "fs_writable": [str(workspace)],
            "env": {"PATH": "/usr/bin:/bin"},
        }), encoding="utf-8")
        os.chmod(policy_path, 0o644)
        scratch = workspace.parent / "route-b-orphan"
        scratch.mkdir(parents=True, exist_ok=True)
        helper = subprocess.Popen(
            [sys.executable, "-B", "-c", LEASE_HELPER, str(policy_path), str(UID),
             str(scratch)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/workspace",
        )
        line = helper.stdout.readline().decode().strip()
        assert line.isdigit(), (line, helper.stderr.read().decode()[:400])
        slot_pid = int(line)
        assert Path(f"/proc/{slot_pid}").exists()

        helper.kill()
        helper.wait(timeout=30)
        # The helper was the slot's only reaper, so after it dies the exited
        # slot can only be waited on by the container's pid 1 -- which does not
        # always reap promptly. A zombie is the proof we want ("the process
        # ended"); a live process is not.
        deadline = time.monotonic() + 30.0
        while _proc_state(slot_pid) == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _proc_state(slot_pid) != "running", (
            f"slot pid {slot_pid} was still running 30 s after the worker that "
            "held its control descriptor died: the generation did not tear "
            "itself down on channel EOF"
        )
        # Nothing of the generation may outlive it either: the tree under the
        # slot (init + the parked main) must be gone or unreaped-but-dead.
        for pid in _tree_pids(slot_pid):
            assert _proc_state(pid) != "running", (
                f"process {pid} of the generation survived its slot"
            )
    finally:
        ex.close()


GUEST_IDENTITY_CODE = (
    "id -u; id -g; "
    "mkfifo mk.fifo && echo fifo-ok; "
    "mknod blk b 8 0; echo mknod-rc=$?; "
    "echo end"
)


async def test_slot_restores_in_guest_root_without_device_nodes(workspace) -> None:
    """Parity for the guest identity, and the one thing it buys back is fenced.

    A route-B slot mediator *is* the sandbox uid, so nobody can write maps for
    it the way a privileged supervisor writes them for its child; without help
    the guest would see its own host uid instead of uid 0 (fork F18 self-maps
    `0 -> euid` inside the sandbox's namespace, matching the in-process shape).
    That namespace then carries CAP_MKNOD, so the seccomp filter that denies
    **device** nodes -- while keeping `mkfifo`, the same syscall with S_IFIFO --
    is what stops the guest from minting a raw-disk handle and opening it.
    """
    ex = _executor(workspace, "sbx_rbe_guest_identity")
    try:
        code, out, err = await _collect(
            await ex.start(
                _config(["/bin/sh", "-c", GUEST_IDENTITY_CODE], str(workspace))
            )
        )
        assert code == 0, (code, out, err)
        lines = out.decode().splitlines()
        # Inside the namespace the workload is uid/gid 0; host-side ownership is
        # unchanged by the map (the kernel compares the kuid), which the
        # ownership contracts above assert independently.
        assert lines[:2] == ["0", "0"], out
        assert lines[-1] == "end", out
        assert "fifo-ok" in lines, out        # mkfifo must keep working
        assert "mknod-rc=1" in lines, out  # device nodes must not be creatable
        assert not (workspace / "blk").exists(), out
        assert (workspace / "mk.fifo").is_fifo(), out
        # The failing tool's own diagnostic (message wording varies by coreutils
        # version, so only the program name is pinned here); *why* it is denied
        # by file type -- and why S_IFIFO is not -- is pinned at the mechanism
        # level in the fork's `test_arg_filters_block_device_nodes_but_not_fifos`.
        assert err.startswith(b"mknod:"), err
    finally:
        ex.close()
