"""C3 Task 2 (controller ruling D3): the per-node agent's instruction service.

``c3_agent`` is the CP→agent half of C3's two channels (there is no
worker↔agent channel). It is **stateless by construction**: the control plane's
instruction carries every parameter (including the uid), so the agent holds no
authorization table, no TTL and no "push before send" ordering -- the only
self-check it needs is that the request is addressed to *itself*.

Two things are pinned here, and they are the whole security story of this
service:

* the surface: an authenticated, self-addressed ``POST
  /internal/nodes/{node_id}/agent/{op}`` whose refusal reasons are named, and
* the first op's acceptance rule: ``grant-slot`` runs ``as_uid --uid X --pid N``
  and accepts **only** exit 0 with exactly the ``C3-ASUID-OK pid=N uid=X`` line
  on stdout and nothing on stderr. Anything else is a named fail-closed
  refusal -- a partially-applied identity grant must never look like success.

The container lane (``tests/security/test_agent_image_privilege.py``) pins the
real ``as_uid`` against the kernel; this lane pins the service around it with
the runner injected.

The container-pid → host-pid reverse lookup is deliberately absent: it belongs
to Task 3, which owns the ``NSpid`` chain plus the target worker pod's cgroup
match and will pass the host pid in. The service says so at the call site.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest

from c3_agent.app import AgentRefusal, SubprocessAsUidRunner, create_app
from c3_agent.config import Settings
from c3_agent.lookup import (
    LookupRefusal,
    ProcLookup,
    SlotProcess,
    missing_slot_pid_message,
)

TOKEN = "c3-agent-sekret"
#: Ruling D12 in this lane: the URL names the **host** the agent runs on (the
#: DaemonSet's `spec.nodeName`), while the instruction body names the **worker
#: pod** that reported the pid. The two are deliberately different values here,
#: so a body/URL mix-up cannot pass unnoticed.
HOST = "k0s-worker-0"
WORKER = "e2b-worker-0"
GRANT_URL = f"/internal/nodes/{HOST}/agent/grant-slot"
#: The worker's own pid namespace identity, as the control plane carries it
#: (Task 3 ruling D9.3: the candidate must live in exactly this namespace).
PID_NAMESPACE = "pid:[4026532458]"
#: What the stub lookup answers for any container pid: the host pid the agent
#: hands to ``as_uid``. The instruction's pid is the *container* pid, so the two
#: must never be confused with one another.
HOST_PID = 990425


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


def _body(**overrides) -> dict:
    payload = {
        "sandbox_id": "sbx_grant",
        "uid": 10007,
        "pid": 4242,
        "worker": {"node_id": WORKER, "pid_namespace": PID_NAMESPACE},
    }
    payload.update(overrides)
    return payload


class _StubRunner:
    """Records what the service asked for and answers like ``as_uid`` would."""

    def __init__(self, *, refuse: str | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._refuse = refuse

    def grant(self, uid: int, pid: int) -> str:
        self.calls.append((uid, pid))
        if self._refuse is not None:
            raise AgentRefusal(self._refuse)
        return f"C3-ASUID-OK pid={pid} uid={uid}"


class _StubLookup:
    """Stands in for the ``/proc`` rendezvous; records what it was asked."""

    def __init__(
        self,
        *,
        host_pid: int = HOST_PID,
        refuse: str | None = None,
        present: bool = True,
    ) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self._host_pid = host_pid
        self._refuse = refuse
        self._present = present

    def host_pid(self, container_pid, identity, *, sandbox_id: str) -> SlotProcess:
        self.calls.append((container_pid, sandbox_id, identity.pid_namespace))
        if self._refuse is not None:
            raise LookupRefusal(self._refuse)
        return SlotProcess(
            host_pid=self._host_pid,
            start_time="4242",
            pid_namespace=identity.pid_namespace,
        )

    def still_alive(self, slot: SlotProcess) -> bool:
        return self._present


# ------------------------------------------------------------------ the surface


@pytest.mark.asyncio
async def test_grant_slot_runs_as_uid_and_answers_the_instruction() -> None:
    runner = _StubRunner()
    lookup = _StubLookup()
    app = create_app(settings=_settings(), runner=runner, lookup=lookup)
    async with _client(app) as client:
        resp = await client.post(GRANT_URL, headers=_headers(), json=_body())
        assert resp.status_code == 200
        assert resp.json() == {
            "op": "grant-slot",
            "sandboxID": "sbx_grant",
            "uid": 10007,
            "pid": 4242,
            "hostPid": HOST_PID,
            "pidNamespace": PID_NAMESPACE,
            "asUid": f"C3-ASUID-OK pid={HOST_PID} uid=10007",
        }
    # The container pid is what the instruction carried; the *host* pid is what
    # the lookup resolved and what face A writes. The uid is the control
    # plane's parameter, handed straight through.
    assert lookup.calls == [(4242, "sbx_grant", PID_NAMESPACE)]
    assert runner.calls == [(10007, HOST_PID)]


@pytest.mark.asyncio
async def test_an_instruction_for_another_host_is_refused_named() -> None:
    """The agent may not be driven on behalf of a host it is not.

    This is the *only* local decision the stateless agent makes: "addressed to
    me?". The address is the **host** (D12), so a different node name is
    refused before the runner is consulted.
    """
    runner = _StubRunner()
    app = create_app(settings=_settings(), runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            "/internal/nodes/k0s-worker-1/agent/grant-slot",
            headers=_headers(),
            json=_body(),
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
async def test_every_request_must_carry_the_agent_token() -> None:
    app = create_app(settings=_settings(), runner=_StubRunner())
    async with _client(app) as client:
        missing = await client.post(GRANT_URL, json=_body())
        assert missing.status_code == 401
        assert missing.json() == {"error": "unauthorized"}
        wrong = await client.post(GRANT_URL, headers=_headers("nope"), json=_body())
        assert wrong.status_code == 401
        assert wrong.json() == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_an_unconfigured_token_refuses_to_answer() -> None:
    """An accidentally exposed port cannot be abused: no token, no service."""
    app = create_app(settings=_settings(token=""), runner=_StubRunner())
    async with _client(app) as client:
        resp = await client.post(GRANT_URL, headers=_headers(""), json=_body())
        assert resp.status_code == 500
        assert resp.json() == {"error": "c3-agent token not configured"}


@pytest.mark.asyncio
async def test_an_unknown_op_is_refused_named() -> None:
    app = create_app(settings=_settings(), runner=_StubRunner())
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{HOST}/agent/delete-tree",
            headers=_headers(),
            json=_body(),
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": "unknown agent op 'delete-tree'"}


@pytest.mark.asyncio
async def test_the_delegate_cgroup_op_is_routed_and_its_lookup_refusal_is_named(
    tmp_path: Path,
) -> None:
    """N83 Phase 1 · Task 3: the delegating op rides the same authenticated table.

    It is the same surface as ``grant-slot``/``chown``/``rm``/``walk`` (one op
    table, one token, one refusal shape), so what this lane pins is that the new
    op is *in* that table -- a 404 would mean a name that drifted -- and that a
    lookup that finds nothing is a named 502, never a silent skip. The synthetic
    ``/proc`` in ``tests/unit/test_c3_delegate_cgroup.py`` drives the real
    delegation; here the shape rule and the op's routing are the subject.
    """
    app = create_app(
        settings=_settings(),
        runner=_StubRunner(),
        lookup=ProcLookup(tmp_path / "proc"),
    )
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/{HOST}/agent/delegate-cgroup",
            headers=_headers(),
            json={
                "worker": {
                    "node_id": WORKER,
                    "pod_uid": "6d3cdd7b-3a5e-4a1f-9a6b-0c1d2e3f4a5b",
                }
            },
        )
        assert resp.status_code == 502
        assert resp.json() == {
            "error": (
                "worker e2b-worker-0's container holds no process this agent can "
                "identify as the container's init: refusing to locate its "
                "container cgroup"
            )
        }


@pytest.mark.asyncio
async def test_a_refused_grant_is_fail_closed_and_named() -> None:
    runner = _StubRunner(
        refuse="as_uid refused: uid 999 is outside the privileged helper uid pool"
    )
    app = create_app(settings=_settings(), runner=runner, lookup=_StubLookup())
    async with _client(app) as client:
        resp = await client.post(
            GRANT_URL, headers=_headers(), json=_body(uid=999)
        )
        assert resp.status_code == 502
        assert resp.json() == {
            "error": (
                "as_uid refused: uid 999 is outside the privileged helper "
                "uid pool"
            )
        }


@pytest.mark.asyncio
async def test_a_worker_identity_that_cannot_be_resolved_is_refused_named() -> None:
    """The lookup's refusals reach the control plane by name, not as a 500.

    The text here is the *real* refusal's spelling, built the way the lookup
    builds it -- a stub whose wording drifted would let the wire contract and the
    operator-facing name diverge (the real module is driven in
    :func:`test_the_real_lookup_names_its_refusals_through_the_service`).
    """
    runner = _StubRunner()
    lookup = _StubLookup(
        refuse="container pid 4242 is not in worker e2b-worker-0's pid "
        "namespace (pid:[4026532999]): refusing"
    )
    app = create_app(settings=_settings(), runner=runner, lookup=lookup)
    async with _client(app) as client:
        resp = await client.post(GRANT_URL, headers=_headers(), json=_body())
        assert resp.status_code == 502
        assert resp.json() == {
            "error": (
                "container pid 4242 is not in worker e2b-worker-0's pid "
                "namespace (pid:[4026532999]): refusing"
            )
        }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_the_real_lookup_names_its_refusals_through_the_service(
    tmp_path: Path,
) -> None:
    """The service's 502 carries the *real* module's words, not a paraphrase.

    A synthetic ``/proc`` holding a candidate in another worker's pid namespace
    is the shape that must be refused; nothing about the text is restated here,
    so a wording change in the lookup shows up in this test rather than in
    production logs.
    """
    proc_root = tmp_path / "proc"
    entry = proc_root / "3000042"
    entry.mkdir(parents=True)
    (entry / "ns").mkdir()
    (entry / "status").write_text("Name:\tpython3\nNSpid:\t3000042\t4242\n", encoding="utf-8")
    (entry / "cgroup").write_text("0::/\n", encoding="utf-8")
    (entry / "stat").write_text(
        "3000042 (python3) S " + " ".join(["0"] * 18 + ["77"]) + "\n",
        encoding="utf-8",
    )
    os.symlink("pid:[4026532999]", entry / "ns" / "pid")
    runner = _StubRunner()
    app = create_app(
        settings=_settings(),
        runner=runner,
        lookup=ProcLookup(proc_root=proc_root),
    )
    async with _client(app) as client:
        resp = await client.post(GRANT_URL, headers=_headers(), json=_body())
        assert resp.status_code == 502
        assert resp.json() == {
            "error": (
                "container pid 4242 is not in worker e2b-worker-0's pid "
                "namespace (pid:[4026532458]): refusing"
            )
        }
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_slot_pid_that_vanishes_before_the_write_is_named() -> None:
    """The one detail the brief singles out: the child crashed mid-grant.

    The lookup resolved the host pid and ``as_uid`` then failed -- and because
    that pid is *gone* from the process table, the refusal must name it as gone
    instead of forwarding the primitive's reading of a missing ``/proc`` entry.
    Fail closed, named, never a silent continue.
    """
    runner = _StubRunner(
        refuse=f"as_uid exit 77: cannot read uid_map for pid {HOST_PID}: "
        "No such file or directory"
    )
    lookup = _StubLookup(present=False)
    app = create_app(settings=_settings(), runner=runner, lookup=lookup)
    async with _client(app) as client:
        resp = await client.post(GRANT_URL, headers=_headers(), json=_body())
        assert resp.status_code == 502
        assert resp.json() == {"error": missing_slot_pid_message("sbx_grant")}
    assert runner.calls == [(10007, HOST_PID)]


@pytest.mark.asyncio
async def test_an_instruction_without_a_worker_identity_is_refused_named() -> None:
    """D9.3: no identity, no grant -- never "the first pid whose NSpid matches".

    The instruction is the control plane's; a body that omits the worker is a
    call the agent can prove nothing about, so it is refused before the
    privileged runner is reached.
    """
    runner = _StubRunner()
    app = create_app(settings=_settings(), runner=runner, lookup=_StubLookup())
    body = _body()
    del body["worker"]
    async with _client(app) as client:
        resp = await client.post(GRANT_URL, headers=_headers(), json=body)
        assert resp.status_code == 422
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_worker_that_is_not_this_host_is_accepted() -> None:
    """D12: the URL names the host, the body names the worker -- both can differ.

    A node runs several workers (two pods in k8s, three in the multinode
    compose stack), so "which worker" and "which host" are two different
    questions and must not be compared with each other. What ties the
    instruction to the right worker is the pid namespace (and, in k8s, the pod
    UID the lookup matches against the candidate's cgroup), not a name match.
    """
    runner = _StubRunner()
    app = create_app(settings=_settings(), runner=runner, lookup=_StubLookup())
    async with _client(app) as client:
        resp = await client.post(
            GRANT_URL,
            headers=_headers(),
            json=_body(
                worker={"node_id": "e2b-worker-7", "pid_namespace": PID_NAMESPACE}
            ),
        )
        assert resp.status_code == 200
    assert runner.calls == [(10007, HOST_PID)]


@pytest.mark.asyncio
async def test_a_worker_identity_that_is_not_a_node_id_is_refused_named() -> None:
    """The worker field is still shape-checked before it reaches a log line.

    It is not security-relevant any more (the control plane is the authority
    that paired the two names), but a hostile string must not be echoed into the
    agent's log or a later lookup argument.
    """
    runner = _StubRunner()
    app = create_app(settings=_settings(), runner=runner, lookup=_StubLookup())
    async with _client(app) as client:
        resp = await client.post(
            GRANT_URL,
            headers=_headers(),
            json=_body(
                worker={
                    "node_id": "../../etc/passwd",
                    "pid_namespace": PID_NAMESPACE,
                }
            ),
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "worker.node_id is not a valid node id"}
    assert runner.calls == []


# ------------------------------------------- the subprocess acceptance rule


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-as-uid"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_subprocess_runner_accepts_exactly_the_ok_line(tmp_path: Path) -> None:
    """Exit 0 + the exact one-line stdout + empty stderr is the *only* success."""
    good = _script(tmp_path, "echo 'C3-ASUID-OK pid=4242 uid=10007'\n")
    runner = SubprocessAsUidRunner(str(good))
    assert runner.grant(10007, 4242) == "C3-ASUID-OK pid=4242 uid=10007"


def test_subprocess_runner_refuses_anything_that_is_not_the_ok_line(
    tmp_path: Path,
) -> None:
    """Every other shape is named: a half-applied grant must not read as success.

    The four shapes cover the primitive's own contract (Task 1): non-zero exit
    (usage 2, refusal 77), a stdout that is not the exact line, a missing line,
    and *any* stderr -- stdout is the grant, stderr is a refusal channel. The
    messages are asserted as whole strings (not ``match=``): the point is the
    exact refusal an operator reads, not that a substring is present.
    """
    with pytest.raises(AgentRefusal) as nonzero:
        SubprocessAsUidRunner(
            str(_script(tmp_path, "echo 'outside the pool' >&2\nexit 77\n"))
        ).grant(999, 4242)
    assert str(nonzero.value) == "as_uid exit 77: outside the pool"
    with pytest.raises(AgentRefusal) as wrong_line:
        SubprocessAsUidRunner(
            str(_script(tmp_path, "echo 'C3-ASUID-OK pid=1 uid=2'\n"))
        ).grant(10007, 4242)
    assert str(wrong_line.value) == (
        "as_uid returned an unexpected stdout for uid 10007 pid 4242: "
        "'C3-ASUID-OK pid=1 uid=2\\n'"
    )
    with pytest.raises(AgentRefusal) as empty_line:
        SubprocessAsUidRunner(str(_script(tmp_path, "true\n"))).grant(10007, 4242)
    assert str(empty_line.value) == (
        "as_uid returned an unexpected stdout for uid 10007 pid 4242: ''"
    )
    with pytest.raises(AgentRefusal) as noisy_stderr:
        SubprocessAsUidRunner(
            str(_script(tmp_path, "echo oops >&2\necho 'C3-ASUID-OK pid=4242 uid=10007'\n"))
        ).grant(10007, 4242)
    assert str(noisy_stderr.value) == (
        "as_uid wrote to stderr for uid 10007 pid 4242: 'oops\\n'"
    )


def test_subprocess_runner_refuses_a_missing_binary(tmp_path: Path) -> None:
    """``as_uid`` not installed is a named refusal, never an exception out."""
    missing = str(tmp_path / "does-not-exist")
    with pytest.raises(AgentRefusal) as excinfo:
        SubprocessAsUidRunner(missing).grant(10007, 4242)
    assert str(excinfo.value) == (
        f"could not run as_uid at {missing}: No such file or directory"
    )


# --------------------------------------------------------------------- startup


def test_the_service_refuses_to_start_without_its_own_identity() -> None:
    """The self-check needs a node id; starting without one is a hard error."""
    from c3_agent.__main__ import _startup_error

    assert _startup_error(_settings(node_id="")) == (
        "E2B_C3_AGENT_NODE_ID (or E2B_NODE_ID) is required; the agent must "
        "know which node it is"
    )
    assert _startup_error(_settings(token="")) == (
        "E2B_C3_AGENT_TOKEN is required; refusing to start without auth"
    )
    # `status_text` is injected so this case means the same thing on every host:
    # the check below reads /proc/self/status, and the test-runner image runs
    # under `seccomp=unconfined` (Seccomp: 0), which would otherwise be a
    # different answer here than on a developer's macOS box (no /proc at all).
    assert _startup_error(_settings(), status_text="Seccomp:\t2\nNoNewPrivs:\t0\n") is None


def test_the_service_refuses_to_start_unfiltered_or_with_no_new_privs() -> None:
    """The two silent ways face A stops granting, caught before it serves.

    Measured (docs/security-audit/c3-agent-syscall-filter-2026-10-05.md §1):
    a runtime profile does **not** set NNP, so the file capabilities on
    `as_uid` survive it -- but `NoNewPrivs: 1` kills them on the exec that
    matters (the one a child does later, which is how `as_uid` is reached).
    Both failures are invisible until the first slot start, and both look like
    a capability bug rather than a flag somebody added.
    """
    from c3_agent.__main__ import _startup_error

    assert _startup_error(
        _settings(), status_text="Seccomp:\t2\nNoNewPrivs:\t0\n"
    ) is None

    assert _startup_error(
        _settings(), status_text="Seccomp:\t0\nNoNewPrivs:\t0\n"
    ) == (
        "no seccomp filter is loaded (Seccomp: 0): this container is not "
        "running under the profile it was hardened with "
        "(E2B_C3_AGENT_REQUIRE_FILTER=0 runs unfiltered on purpose)"
    )

    assert _startup_error(
        _settings(), status_text="Seccomp:\t2\nNoNewPrivs:\t1\n"
    ) == (
        "NoNewPrivs is set: the kernel will ignore as_uid's file "
        "capabilities, so every grant would fail silently "
        "(E2B_C3_AGENT_REQUIRE_FILTER=0 runs unfiltered on purpose)"
    )

    # The escape hatch, for shapes that are unfiltered on purpose (a developer's
    # Linux box, the offline lanes). Both refusals become warnings.
    for status in ("Seccomp:\t0\nNoNewPrivs:\t0\n", "Seccomp:\t2\nNoNewPrivs:\t1\n"):
        assert (
            _startup_error(_settings(require_filter=False), status_text=status)
            is None
        )


# ----------------------------------------------------------------- hardening


@pytest.mark.asyncio
async def test_the_service_exposes_no_interactive_surface() -> None:
    """A privileged service ships no docs schema to enumerate."""
    app = create_app(settings=_settings(), runner=_StubRunner())
    async with _client(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            resp = await client.get(path)
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_hostile_sandbox_id_is_refused_before_the_runner() -> None:
    """The id is validated the same way the control plane validates it.

    The refusal is named and does not echo the id back, and the privileged
    runner and the ``/proc`` lookup are never reached.
    """
    runner = _StubRunner()
    lookup = _StubLookup()
    app = create_app(settings=_settings(), runner=runner, lookup=lookup)
    async with _client(app) as client:
        resp = await client.post(
            GRANT_URL, headers=_headers(), json=_body(sandbox_id="../../etc/passwd")
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "sandbox_id is not a valid sandbox id"}
    assert runner.calls == []
    assert lookup.calls == []


@pytest.mark.asyncio
async def test_a_non_ascii_token_is_a_401_not_a_500() -> None:
    """``compare_digest`` raises on non-ASCII ``str``; a header can be anything."""
    app = create_app(settings=_settings(), runner=_StubRunner())
    async with _client(app) as client:
        resp = await client.post(
            GRANT_URL,
            headers={b"X-Internal-Key": "\u00e9".encode("utf-8")},
            json=_body(),
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}


def test_the_listen_host_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bind address is deliberate (Task 3 bounds it with a NetworkPolicy)."""
    monkeypatch.setenv("E2B_C3_AGENT_HOST", "127.0.0.1")
    assert Settings(token=TOKEN, node_id=HOST).host == "127.0.0.1"
    monkeypatch.delenv("E2B_C3_AGENT_HOST")
    assert Settings(token=TOKEN, node_id=HOST).host == "0.0.0.0"
