"""C3 Task 4 (rulings D18.2/D18.3): the agent's **file-operation** verbs.

Face B is the half of the agent that stands where the worker's ``e2b-maint``
used to: ``chown`` / ``rm`` / ``walk``, executed by the agent itself (C3 §11.2
-- file operations are never handed to a worker helper). The path discipline is
``maint.c``'s, unchanged: this service only shape-checks the instruction and
execs the *same* binary the worker's broker execs, with the same roots and pool
in its environment. Nothing here resolves a path.

What this lane pins:

* the verb whitelist (D18.2): ``chown``/``rm``/``walk`` are served, an unknown
  verb is refused **by name** (the pre-existing test for ``delete-tree`` keeps
  covering the refusal path);
* the exact argv and environment the agent hands ``e2b-maint`` -- the worker's
  identity in ``E2B_BROKER_WORKER_UID/GID`` is what makes ``--worker`` and the
  group gate mean the same thing they mean behind ``serve``;
* fail-closed judgement: a non-zero exit, an unexpected stdout on a verb that
  prints nothing, and a ``walk`` line that is not the documented shape are all
  named refusals -- a half-applied chown must never read as success.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from c3_agent.app import create_app
from c3_agent.config import Settings
from c3_agent.fileops import (
    AgentFileOpRefusal,
    FileOpInstruction,
    FileOpShapeRefusal,
    SubprocessMaintRunner,
    run_file_op,
)
from control_plane.c3_agent_client import (
    AgentTarget,
    C3AgentClient,
    StaticAgentAddressResolver,
)

TOKEN = "c3-agent-sekret"
#: D12: the URL names the **host** this agent runs on.
HOST = "k0s-worker-0"
#: The worker's identity (its node id) as the control plane spells it.
WORKER = "e2b-worker-0"
#: The worker's own identity, as the control plane learned it from the node
#: record. ``chown --worker`` and the ``--gid`` gate both need it, and the
#: agent is told it rather than reading it from its own ``getuid()`` (it is
#: root, so its own identity would mean "chown to root").
WORKER_UID = 65534
WORKER_GID = 65534
#: The sandbox's pooled uid -- the control plane's parameter, never the
#: agent's.
POOL_UID = 10007
WORKSPACE = "/var/lib/e2b-sandboxes/workspaces/sbx_fileops"
RUNTIME = "/var/lib/e2b-sandboxes/state/_runtime/sbx_fileops"

CHOWN_URL = f"/internal/nodes/{HOST}/agent/chown"
RM_URL = f"/internal/nodes/{HOST}/agent/rm"
WALK_URL = f"/internal/nodes/{HOST}/agent/walk"


def _settings(**overrides) -> Settings:
    defaults = dict(token=TOKEN, node_id=HOST)
    defaults.update(overrides)
    return Settings(**defaults)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    )


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"X-Internal-Key": token}


def _worker() -> dict[str, int]:
    return {"uid": WORKER_UID, "gid": WORKER_GID}


class _StubMaintRunner:
    """Records the argv/environment and answers like ``e2b-maint`` would."""

    def __init__(self, *, stdout: str = "", refuse: str | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._stdout = stdout
        self._refuse = refuse

    def run(self, argv: list[str], *, env: dict[str, str]) -> str:
        self.calls.append((list(argv), dict(env)))
        if self._refuse is not None:
            raise AgentFileOpRefusal(self._refuse)
        return self._stdout


def _expected_env(settings: Settings) -> dict[str, str]:
    """The environment the agent must give ``e2b-maint``.

    The four roots and the uid pool are ``priv_common.c``'s inputs (its
    defaults are the same values), and the two ``E2B_BROKER_WORKER_*`` are the
    ``serve`` daemon's own contract for "the worker this request acts as":
    directly exec'd by root, ``--worker`` would otherwise mean *root*.
    """
    return {
        "E2B_UID_POOL_START": str(settings.uid_pool_start),
        "E2B_UID_POOL_SIZE": str(settings.uid_pool_size),
        "E2B_WORKSPACE_BASE": settings.workspace_base,
        # Resolved, never empty: ``priv_state_base`` reads one variable and
        # falls back to the workspace base only for want of a value, so the
        # agent writes the value it means (the same thing
        # ``PrivHelpers.subprocess_env`` does).
        "E2B_STATE_BASE": settings.state_base or settings.workspace_base,
        "E2B_BROKER_WORKER_UID": str(WORKER_UID),
        "E2B_BROKER_WORKER_GID": str(WORKER_GID),
    }


@pytest.mark.asyncio
async def test_chown_runs_maint_with_the_instruction() -> None:
    """The tree hand-over: ``--uid <pooled> --gid <worker> --recursive``."""
    runner = _StubMaintRunner()
    settings = _settings()
    app = create_app(settings=settings, maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            CHOWN_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "uid": POOL_UID,
                "gid": WORKER_GID,
                "recursive": True,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "op": "chown",
            "path": WORKSPACE,
            "uid": POOL_UID,
            "gid": WORKER_GID,
            "recursive": True,
        }
    assert runner.calls == [
        (
            [
                str(settings.maint_path),
                "chown",
                "--uid",
                str(POOL_UID),
                "--gid",
                str(WORKER_GID),
                "--recursive",
                "--path",
                WORKSPACE,
            ],
            _expected_env(settings),
        )
    ]


@pytest.mark.asyncio
async def test_chown_worker_form_names_no_uid() -> None:
    """The slot-document form: the owner stays the worker, the group is the slot."""
    runner = _StubMaintRunner()
    settings = _settings()
    app = create_app(settings=settings, maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            CHOWN_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": f"{RUNTIME}/rb-sbx_fileops/policy.json",
                "worker_owned": True,
                "gid": POOL_UID,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "op": "chown",
            "path": f"{RUNTIME}/rb-sbx_fileops/policy.json",
            "uid": None,
            "gid": POOL_UID,
            "recursive": False,
        }
    assert runner.calls == [
        (
            [
                str(settings.maint_path),
                "chown",
                "--worker",
                "--gid",
                str(POOL_UID),
                "--path",
                f"{RUNTIME}/rb-sbx_fileops/policy.json",
            ],
            _expected_env(settings),
        )
    ]


def test_a_worker_chown_without_a_worker_uid_is_refused() -> None:
    """The half of the shape gate ``maint_env``'s gid-only case leans on.

    ``--worker`` writes ``priv_worker_uid()`` -- read from
    ``E2B_BROKER_WORKER_UID`` -- into the tree as its owner. That variable is
    now written **only** when the instruction carries a uid, because the create
    path's ``chown --uid … --gid …`` needs the group gate and no worker uid
    (``c3_agent.materialize``). The two facts are safe together exactly as long
    as ``--worker`` cannot run without a uid, so that is pinned here rather
    than inferred from the HTTP surface, which cannot even express the shape
    (``WorkerCredentials`` requires both halves).
    """
    instruction = FileOpInstruction(
        sandbox_id="sbx_fileops",
        path=RUNTIME,
        gid=WORKER_GID,
        recursive=True,
        worker_owned=True,
        worker_gid=WORKER_GID,
    )
    with pytest.raises(FileOpShapeRefusal) as excinfo:
        run_file_op(
            "chown",
            instruction,
            runner=_StubMaintRunner(),
            settings=_settings(),
        )
    assert str(excinfo.value) == (
        "a --worker chown needs the worker's own identity (it is what --worker "
        "writes into the tree; without it the owner would be root): refusing"
    )


@pytest.mark.asyncio
async def test_rm_runs_maint_and_answers() -> None:
    runner = _StubMaintRunner()
    settings = _settings()
    app = create_app(settings=settings, maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            RM_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"op": "rm", "path": WORKSPACE}
    assert runner.calls == [
        (
            [str(settings.maint_path), "rm", "--path", WORKSPACE],
            _expected_env(settings),
        )
    ]


@pytest.mark.asyncio
async def test_walk_relays_the_entry_lines() -> None:
    """``walk`` is the one verb with an answer: one line per entry, verbatim."""
    stdout = (
        f"d {WORKER_UID} {WORKER_GID} 770 512 {WORKSPACE}\n"
        f"f {POOL_UID} {POOL_UID} 644 4096 {WORKSPACE}/note.txt\n"
        # ``o`` -- a fifo/socket/device -- is part of the vocabulary too: a
        # regular file, a directory and a symlink are not the whole tree.
        f"o {POOL_UID} {POOL_UID} 644 0 {WORKSPACE}/agent.sock\n"
    )
    runner = _StubMaintRunner(stdout=stdout)
    settings = _settings()
    app = create_app(settings=settings, maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            WALK_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "op": "walk",
            "path": WORKSPACE,
            "stdout": stdout,
        }
    assert runner.calls == [
        (
            [str(settings.maint_path), "walk", "--path", WORKSPACE],
            _expected_env(settings),
        )
    ]


@pytest.mark.asyncio
async def test_a_relative_path_is_refused_named() -> None:
    """The agent shape-checks; the realpath + root whitelist stays ``maint.c``'s."""
    runner = _StubMaintRunner()
    app = create_app(settings=_settings(), maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            RM_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": "workspaces/sbx_fileops",
                "worker": _worker(),
            },
        )
        assert resp.status_code == 400
        assert resp.json() == {
            "error": "path is not an absolute path: refusing"
        }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_chown_with_no_target_is_refused_named() -> None:
    runner = _StubMaintRunner()
    app = create_app(settings=_settings(), maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            CHOWN_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 400
        assert resp.json() == {
            "error": (
                "chown needs one of uid (a pooled uid) or worker (the "
                "worker's own identity): refusing"
            )
        }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_both_chown_targets_at_once_is_refused_named() -> None:
    runner = _StubMaintRunner()
    app = create_app(settings=_settings(), maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            CHOWN_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "uid": POOL_UID,
                "worker_owned": True,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 400
        assert resp.json() == {
            "error": (
                "chown takes uid or worker, not both: refusing"
            )
        }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_maint_refusal_is_fail_closed_and_named() -> None:
    runner = _StubMaintRunner(
        refuse=(
            "e2b-maint rm refused (exit 77): e2b-maint: refused: "
            f"{WORKSPACE} is not under any privileged helper root"
        )
    )
    app = create_app(settings=_settings(), maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            RM_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 502
        assert resp.json() == {
            "error": (
                "e2b-maint rm refused (exit 77): e2b-maint: refused: "
                f"{WORKSPACE} is not under any privileged helper root"
            )
        }


@pytest.mark.asyncio
async def test_stdout_from_a_verb_that_prints_nothing_is_refused_named() -> None:
    """A half-applied step must never read as success.

    ``chown``/``rm`` print nothing on success (``maint.c``); output on those
    verbs means the answer is not the contract, so it is a refusal rather than
    a silently accepted extra.
    """
    runner = _StubMaintRunner(stdout="e2b-maint: something happened\n")
    app = create_app(settings=_settings(), maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            RM_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 502
        assert resp.json() == {
            "error": (
                "e2b-maint rm wrote to stdout ('e2b-maint: something "
                "happened\\n'), which its contract does not: refusing"
            )
        }


@pytest.mark.asyncio
async def test_a_walk_line_that_is_not_the_shape_is_refused_named() -> None:
    runner = _StubMaintRunner(stdout=f"d {WORKER_UID} {WORKER_GID} 770 512\n")
    app = create_app(settings=_settings(), maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            WALK_URL,
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 502
        assert resp.json() == {
            "error": (
                "e2b-maint walk answered a line that is not the documented "
                "'<kind> <uid> <gid> <mode> <size> <path>' shape: "
                "'d 65534 65534 770 512': refusing"
            )
        }


@pytest.mark.asyncio
async def test_a_file_op_for_another_host_is_refused_named() -> None:
    runner = _StubMaintRunner()
    app = create_app(settings=_settings(), maint_runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/k0s-worker-1/agent/rm",
            headers=_headers(),
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 403
        assert resp.json() == {
            "error": (
                "request is addressed to node k0s-worker-1, but this agent "
                "is node k0s-worker-0"
            )
        }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_file_op_without_the_token_is_refused() -> None:
    app = create_app(settings=_settings(), maint_runner=_StubMaintRunner())
    async with _client(app) as client:
        resp = await client.post(
            RM_URL,
            json={
                "sandbox_id": "sbx_fileops",
                "path": WORKSPACE,
                "worker": _worker(),
            },
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}


class _RecordingProcess:
    """A stand-in for ``subprocess.run``'s result."""

    def __init__(self, *, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_the_subprocess_runner_judges_every_exit_code() -> None:
    """The production runner's own contract, driven without a real binary.

    ``maint.c``'s refusals are ``PRIV_EXIT_REFUSED`` (77) with the reason on
    stderr, and usage errors are 2 -- both have to reach the control plane as
    the binary's own words, and only a clean exit 0 may return stdout.
    """
    runner = SubprocessMaintRunner("/var/lib/e2b-priv/e2b-maint")
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        assert kwargs["env"]["E2B_BROKER_WORKER_GID"] == str(WORKER_GID)
        return _RecordingProcess(
            returncode=77,
            stdout="",
            stderr="e2b-maint: refused: /nope is not under any root\n",
        )

    runner._run_process = fake_run  # the one seam the lane stubs
    with pytest.raises(AgentFileOpRefusal) as excinfo:
        runner.run(
            ["/var/lib/e2b-priv/e2b-maint", "rm", "--path", WORKSPACE],
            env={"E2B_BROKER_WORKER_GID": str(WORKER_GID)},
        )
    assert str(excinfo.value) == (
        "e2b-maint rm refused (exit 77): e2b-maint: refused: /nope is not "
        "under any root"
    )
    assert seen == [["/var/lib/e2b-priv/e2b-maint", "rm", "--path", WORKSPACE]]


@pytest.mark.asyncio
async def test_two_instructions_do_not_serialize_on_the_event_loop() -> None:
    """Fourth review ①: the privileged exec runs off the agent's event loop.

    ``agent_op`` is ``async`` (reading the body is), and both face A's
    ``as_uid`` and face B's ``e2b-maint`` are *synchronous* ``subprocess.run``
    calls -- up to 300 s for a teardown or a tree walk. Run inline they would
    hold uvicorn's single loop, so the agent would stop accepting connections
    for the whole operation: concurrent slot grants would miss the control
    plane's 5 s deadline, and the thread-pool premise behind
    ``E2B_C3_AGENT_MAX_CONCURRENCY`` would be false. The assertion is therefore
    *overlap* of two concurrent instructions, measured where the privileged work
    happens.
    """
    intervals: list[tuple[float, float]] = []

    class _SlowRunner:
        def run(self, argv, *, env):
            started = time.monotonic()
            time.sleep(0.2)
            intervals.append((started, time.monotonic()))
            return ""

    app = create_app(settings=_settings(), maint_runner=_SlowRunner())
    async with _client(app) as client:
        first, second = await asyncio.gather(
            client.post(
                RM_URL,
                headers=_headers(),
                json={
                    "sandbox_id": "sbx_fileops",
                    "path": WORKSPACE,
                    "worker": _worker(),
                },
            ),
            client.post(
                WALK_URL,
                headers=_headers(),
                json={
                    "sandbox_id": "sbx_fileops",
                    "path": WORKSPACE,
                    "worker": _worker(),
                },
            ),
        )
    assert (first.status_code, second.status_code) == (200, 200)
    assert len(intervals) == 2
    # The two execs really ran at the same time: the later one started before
    # the earlier one finished. Serialized (inline) execution would have the
    # second start *after* the first end -- and, worse, no other request would
    # even be read while the first was running.
    started = sorted(interval[0] for interval in intervals)
    finished = {interval[0]: interval[1] for interval in intervals}
    assert started[1] < finished[started[0]]


@pytest.mark.asyncio
async def test_the_real_agent_service_accepts_the_clients_instruction() -> None:
    """The wire, pinned across the hop rather than from one side.

    The control plane's client and the agent's service are two deployables that
    are never imported together in production, so a key renamed on one side
    would otherwise only show up as a named refusal on the other. Here the
    client dials the *real* service (with the raw ``e2b-maint`` exec stubbed).
    """
    runner = _StubMaintRunner()
    app = create_app(settings=_settings(), maint_runner=runner)
    client = C3AgentClient(
            resolver=StaticAgentAddressResolver(
                {
                    WORKER: AgentTarget(
                        node_identity=HOST,
                        url="http://agent",
                        # D22: the file verbs go to face B's own endpoint; in
                        # this lane both faces are the same test service.
                        maint_url="http://agent",
                    )
                }
            ),
        token=TOKEN,
        timeout_s=2.0,
        file_op_timeout_s=2.0,
        transport=httpx.ASGITransport(app=app),
    )
    answer = await client.chown(
        node_id=WORKER,
        sandbox_id="sbx_fileops",
        path=WORKSPACE,
        uid=POOL_UID,
        gid=WORKER_GID,
        recursive=True,
        worker_uid=WORKER_UID,
        worker_gid=WORKER_GID,
    )
    assert answer == {
        "op": "chown",
        "path": WORKSPACE,
        "uid": POOL_UID,
        "gid": WORKER_GID,
        "recursive": True,
    }
    assert runner.calls == [
        (
            [
                "/var/lib/e2b-priv/e2b-maint",
                "chown",
                "--uid",
                str(POOL_UID),
                "--gid",
                str(WORKER_GID),
                "--recursive",
                "--path",
                WORKSPACE,
            ],
            _expected_env(_settings()),
        )
    ]
