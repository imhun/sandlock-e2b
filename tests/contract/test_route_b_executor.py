"""The SandlockExecutor driving a real supervise slot (route B, end to end).

``tests/contract/test_route_b_slot_pool.py`` proves the slot pool and the
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

import json
import os
import stat
import time
from pathlib import Path

import pytest

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor
from envd_service.route_b import RouteBConfig, reset_slot_pools, slot_pool_for
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
        route_b=RouteBConfig(
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


async def test_executor_command_runs_as_the_leased_host_uid(workspace) -> None:
    ex = _executor(workspace, "sbx_rbe_id")
    try:
        assert ex._route_b_active is True
        running = await ex.start(
            _config(
                ["/bin/sh", "-c", "id -u; id -g; echo done"],
                str(workspace),
            )
        )
        code, out, err = await _collect(running)
        assert (code, out, err) == (0, b"%d\n%d\ndone\n" % (UID, UID), b"")
        # The mediator identity is visible from the worker side too: the slot
        # process itself runs as the leased uid, not as root.
        slot = slot_pool_for(ex._route_b).slot("sbx_rbe_id")
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
    pool = slot_pool_for(ex._route_b)
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
    pool = slot_pool_for(ex._route_b)
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
    channel because route B is the chroot shape's default backend."""
    ex = _executor(workspace, "sbx_rbe_127")
    try:
        running = await ex.start(_config(["/nonexistent-e2b-bin"], str(workspace)))
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
from envd_service.route_b import W1SlotPool

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
