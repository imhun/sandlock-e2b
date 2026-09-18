"""G1a contract: pause/resume delivery reaches a real remote worker.

The combined shape (``test_pause_resume_sandlock.py``) freezes exec children
through one runtime registry shared by the control plane and the envd
service. This file locks the separated (multinode) shape: the control plane
pushes pause/resume to the hosting worker agent (``/agent/sandboxes/{id}/
pause|resume``), and a real sandlock worker must freeze a background command
until the SDK's ``Sandbox.connect`` auto-resume thaws it.

The freeze is proven, not inferred. The background command waits for a marker
file and can only print ``done`` once that marker exists; the tests poll the
worker's own runtime state for a SIGSTOPped exec child (``/proc/<pid>/stat``
shows ``T``) instead of racing a fixed silence window against the command's
own deadline, and then pin the state's *second* half (N28/A): the marker that
would satisfy the child cannot be created while the sandbox is paused, because
a paused sandbox refuses new execs and new writes outright. The 2.0s
``queue.Empty`` window follows that refusal with the child demonstrably
frozen, and the same write is admitted again once ``Sandbox.connect`` resumes
-- at which point the thawed child completes.

Refusal shape, measured off the SDK rather than assumed (probe:
``tmp/k0s/probe_pause_errors.py``, 2026-09-18): the command path answers
``failed_precondition``, which the vendored SDK renders as
``SandboxException("Code.FAILED_PRECONDITION: <our message>")`` -- the
``Code.`` prefix is the SDK's own formatting of an unmapped code, so the
assertion below spells it out instead of matching a substring.

``test_paused_sandbox_survives_a_stalled_worker_heartbeat`` runs the same
delivery while the hosting node *looks* lost (E6.1's node-health sweep
orphans a node that stopped heartbeating) and pins that a paused sandbox
keeps its paused state through it -- the state ``Sandbox.connect``'s
auto-resume is gated on, and the only thing that can push the thaw to the
worker holding the frozen child.

Skipped outside the Linux sandlock runner (macOS host runs cover the delivery
mapping unit-level; the container runs this file with
``E2B_TEST_STRICT_SKIPS=1`` and an empty ``E2B_BASE_IMAGE`` for the pure
sandlock shape).
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time

import pytest

from e2b import Sandbox, SandboxException
from tests.security.conftest import sandlock_ready

pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "sandlock pause/resume multinode contract tests need Linux + "
        "sandlock (run inside the Docker test runner)"
    ),
)

#: Same silence window as the combined contract. Its premise is guaranteed
#: here: by the time it starts, the child's completion condition is already
#: satisfied (see the module docstring).
PAUSED_SILENCE_WINDOW_S = 2.0

#: The worker's runtime must show the frozen child quickly; this is a bound on
#: the *delivery*, not a race with the command's own deadline.
FREEZE_BUDGET_S = 10.0
#: How long ``Sandbox.connect``'s auto-resume may take to thaw the child and
#: deliver its ``done``/exit-0 outcome.
THAW_BUDGET_S = 15.0

#: The background command's completion condition: it can only print ``done``
#: once this file exists, and the tests create it *after* the freeze.
MARKER = "pafl-go"

#: What the paused sandbox answers a new command with, as the SDK renders it
#: (see the module docstring: the ``Code.`` prefix comes from the vendored
#: SDK's own formatting of a Connect code it does not map to a typed
#: exception).
PAUSED_COMMAND_REFUSAL = (
    "Code.FAILED_PRECONDITION: Sandbox is paused; run a command only while "
    "it is running (resume it first)"
)


def _assert_paused_refuses_commands(sandbox) -> None:
    """A paused sandbox admits no new exec, so no marker can be created."""
    with pytest.raises(SandboxException) as refused:
        sandbox.commands.run(f"touch {MARKER}")
    assert str(refused.value) == PAUSED_COMMAND_REFUSAL


def _proc_state(pid: int) -> str:
    """The child's ``/proc/<pid>/stat`` state letter (``"?"`` if unreadable)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat_file:
            data = stat_file.read()
    except OSError:
        return "?"
    return data[data.rindex(b")") + 2 :].split()[0].decode()


def _group_states(pgid: int) -> dict[int, str]:
    """``/proc`` state letter of every process in process group ``pgid``."""
    states: dict[int, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as stat_file:
                fields = stat_file.read().split(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) != pgid:
            continue
        states[int(entry)] = fields[0].decode()
    return states


def _stopped_group(pid: int) -> bool:
    """Whether no member of ``pid``'s process group is still running.

    The agent pause route freezes the child's whole *process group* (one
    group per exec child, fork F1.7), so that is what a "frozen" sandbox
    command means: the kernel reports ``T`` for the child and for every
    descendant that inherited its group. A member that already exited while
    the group is stopped is a **zombie** (``Z``): its parent is frozen and
    cannot reap it, so it stays in the group, in state ``Z``, for as long as
    the sandbox is paused. It runs no user code, so it does not un-freeze the
    group -- requiring literally every member to be ``T`` (the pre-fix rule)
    failed this check while the child the caller cares about was demonstrably
    frozen (observed: ``worker-side (pid, /proc state) pairs: [(893, 'T')]``,
    i.e. a ``T`` exec child plus an unreaped member, on ~1/4 of loaded
    whole-lane rounds).
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    for state in _group_states(pgid).values():
        # ``T`` = stopped, ``Z``/``X`` = dead (unreapable while the group is
        # stopped): neither is executing user code. Anything else (``R``,
        # ``S``, ``D``, ...) means the group is not frozen.
        if state not in ("T", "Z", "X"):
            return False
    return True


def _frozen_child_pid(harness, sandbox_id: str) -> int | None:
    """The sandbox's exec child pid once its group is stopped, else None.

    Read off the hosting worker app (the harness exposes the worker-side
    runtimes for exactly this kind of comparison): the worker's process table
    names the child, and the kernel's own ``T`` state is what "paused" means
    for that child's group. Polled, because the agent pause request is
    asynchronous to this process.
    """
    for app in harness["worker_apps"]:
        ctx = app.state.runtimes.get(sandbox_id)
        if ctx is None:
            continue
        for entry in ctx.processes.list():
            pid = entry.get("pid")
            if isinstance(pid, int) and _stopped_group(pid):
                return pid
    return None


def _wait_for_frozen(harness, sandbox_id: str, budget_s: float) -> int:
    """Block until the worker shows a SIGSTOPped child, or fail with its state."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        pid = _frozen_child_pid(harness, sandbox_id)
        if pid is not None:
            return pid
        time.sleep(0.05)
    states = [
        (entry.get("pid"), _proc_state(entry.get("pid")))
        for app in harness["worker_apps"]
        for ctx in [app.state.runtimes.get(sandbox_id)]
        if ctx is not None
        for entry in ctx.processes.list()
    ]
    raise AssertionError(
        f"sandbox {sandbox_id} was paused but no exec child reached the "
        f"kernel's stopped state within {budget_s:g}s (worker-side "
        f"(pid, /proc state) pairs: {states})"
    )

def test_pause_delivery_freezes_remote_child_until_connect_resumes(
    multinode_two_workers,
) -> None:
    """A background command on a remote sandlock worker is frozen by
    pause() and completes only after Sandbox.connect() auto-resumes it."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(
        api_url=harness["api_url"],
        sandbox_url=harness["sandbox_url"],
        api_key="local-key",
    )
    handle = None
    waiter = None
    try:
        handle = sandbox.commands.run(
            f"while [ ! -f {MARKER} ]; do sleep 0.1; done; echo done",
            background=True,
        )
        ended = queue.Queue(maxsize=1)

        def _wait_for_end() -> None:
            try:
                ended.put(handle.wait())
            except BaseException as exc:  # surface any wait failure exactly
                ended.put(exc)

        waiter = threading.Thread(target=_wait_for_end, daemon=True)
        waiter.start()

        assert sandbox.pause() is True
        # The pause reached the worker: its runtime holds a SIGSTOPped child.
        # This is the property the old silence window only inferred, and it no
        # longer races the command's own deadline.
        frozen_pid = _wait_for_frozen(harness, sandbox.sandbox_id, FREEZE_BUDGET_S)
        # The marker cannot be created while the sandbox is paused (N28/A):
        # new execs are refused, so the child stays *un-satisfied* as well as
        # frozen.
        _assert_paused_refuses_commands(sandbox)
        assert _proc_state(frozen_pid) == "T"
        with pytest.raises(queue.Empty):
            ended.get(timeout=PAUSED_SILENCE_WINDOW_S)

        Sandbox.connect(
            sandbox.sandbox_id,
            api_url=harness["api_url"],
            sandbox_url=harness["sandbox_url"],
            api_key="local-key",
        )
        # Resumed: the same command is admitted, satisfies the child's
        # completion condition, and the thawed child finishes.
        assert sandbox.commands.run(f"touch {MARKER}").exit_code == 0
        outcome = ended.get(timeout=THAW_BUDGET_S)
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome.stdout == "done\n"
        assert outcome.stderr == ""
        assert outcome.exit_code == 0
    finally:
        if handle is not None:
            handle.kill()
        if waiter is not None:
            waiter.join(timeout=5)
        sandbox.kill()


def test_paused_sandbox_survives_a_stalled_worker_heartbeat(
    multinode_two_workers,
) -> None:
    """The same delivery contract while the hosting node looks lost (E6.1).

    A remote node whose heartbeat goes stale is marked unhealthy, and the
    control plane's health sweep orphans the sandboxes on it (E6.1) so TTL
    cannot delete them under a live worker. A *paused* sandbox must survive
    that sweep with its state intact: ``paused`` is what makes
    ``Sandbox.connect`` -- the SDK's only public resume surface -- push the
    thaw to the worker holding the frozen child. Before this test's fix the
    sweep flipped the record to ``orphaned``, ``connect`` therefore skipped
    its auto-resume, and the worker's SIGSTOPped child stayed frozen forever
    while the SDK was told the sandbox was connected.
    """
    harness = multinode_two_workers
    sandbox = Sandbox.create(
        api_url=harness["api_url"],
        sandbox_url=harness["sandbox_url"],
        api_key="local-key",
    )
    # A second, *running* sandbox: the sweep must orphan it, which is what
    # proves the sweep actually ran in this round.
    bystander = Sandbox.create(
        api_url=harness["api_url"],
        sandbox_url=harness["sandbox_url"],
        api_key="local-key",
    )
    handle = None
    waiter = None
    try:
        handle = sandbox.commands.run(
            f"while [ ! -f {MARKER} ]; do sleep 0.1; done; echo done",
            background=True,
        )
        ended = queue.Queue(maxsize=1)

        def _wait_for_end() -> None:
            try:
                ended.put(handle.wait())
            except BaseException as exc:  # surface any wait failure exactly
                ended.put(exc)

        waiter = threading.Thread(target=_wait_for_end, daemon=True)
        waiter.start()

        assert sandbox.pause() is True
        _wait_for_frozen(harness, sandbox.sandbox_id, FREEZE_BUDGET_S)

        registry = harness["control_app"].state.registry
        nodes = harness["nodes"]
        record = registry.get(sandbox.sandbox_id)
        assert record.state == "paused"

        # Every node's heartbeat goes stale; the product's own sweep call (the
        # one the 1s health loop makes) then runs against the real registry.
        stale = time.time() - 3600.0
        for node in nodes.list():
            node.heartbeat_at = stale
        assert nodes.reap_unhealthy(registry) != []
        assert registry.get(bystander.sandbox_id).state == "orphaned"
        assert registry.get(sandbox.sandbox_id).state == "paused"

        # The worker's next heartbeat (5s cadence) puts the node back on the
        # healthy list -- what the resume needs to re-book capacity on it.
        # Only the record's own state survived the gap.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if nodes.get(record.node_id).status == "healthy":
                break
            time.sleep(0.2)
        assert nodes.get(record.node_id).status == "healthy"

        # Still paused: the node came back, the state did not change.
        _assert_paused_refuses_commands(sandbox)
        with pytest.raises(queue.Empty):
            ended.get(timeout=PAUSED_SILENCE_WINDOW_S)

        Sandbox.connect(
            sandbox.sandbox_id,
            api_url=harness["api_url"],
            sandbox_url=harness["sandbox_url"],
            api_key="local-key",
        )
        assert sandbox.commands.run(f"touch {MARKER}").exit_code == 0
        outcome = ended.get(timeout=THAW_BUDGET_S)
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome.stdout == "done\n"
        assert outcome.stderr == ""
        assert outcome.exit_code == 0
    finally:
        if handle is not None:
            handle.kill()
        if waiter is not None:
            waiter.join(timeout=5)
        sandbox.kill()
        bystander.kill()


def test_stopped_group_counts_a_member_that_died_inside_it() -> None:
    """A member that dies while the group is stopped must not un-freeze it.

    Deterministic shape of the observed whole-lane failure: SIGSTOP the group,
    then kill one member from outside the group. Its parent -- the frozen group
    leader -- can no longer reap it, so the group holds a ``Z`` member for as
    long as it stays stopped. The child under test is ``T`` throughout; the
    pre-fix rule (every member literally ``T``) called that "not frozen" and
    failed ``_wait_for_frozen`` with ``[(<pid>, 'T')]``.

    No harness needed: it is the freeze *check* that is under test, and the
    kernel states are read exactly as ``_wait_for_frozen`` reads them.
    """
    process = subprocess.Popen(
        ["sh", "-c", "sleep 300 & sleep 300"],
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    try:
        deadline = time.monotonic() + 5.0
        while len(_group_states(pgid)) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        members = _group_states(pgid)
        assert process.pid in members, members
        assert len(members) >= 2, members

        os.killpg(pgid, signal.SIGSTOP)
        # The test is about a member that dies *while* the group is stopped,
        # so the stop has to have landed before the kill: SIGSTOP delivery is
        # asynchronous, and a still-running leader reaps the killed child
        # immediately -- the zombie this asserts on never appears (observed
        # under load as ``{pid: 'T', pid: 'T'}`` and ``None == 'Z'``). The wait
        # is bounded and asserted, so a genuine failure stays loud.
        stop_deadline = time.monotonic() + 5.0
        while time.monotonic() < stop_deadline:
            if all(state == "T" for state in _group_states(pgid).values()):
                break
            time.sleep(0.02)
        assert all(state == "T" for state in _group_states(pgid).values()), (
            _group_states(pgid)
        )
        victim = max(pid for pid in members if pid != process.pid)
        os.kill(victim, signal.SIGKILL)

        states: dict[int, str] = {}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            states = _group_states(pgid)
            if states.get(victim) == "Z" and states.get(process.pid) == "T":
                break
            time.sleep(0.02)
        assert states.get(process.pid) == "T", states
        assert states.get(victim) == "Z", states
        assert _stopped_group(process.pid) is True
    finally:
        try:
            os.killpg(pgid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
