"""C3 Task 2 (controller ruling D3): the per-node agent's instruction service.

``deploy/c3_agent`` is the CP→agent half of C3's two channels (there is no
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

import stat
from pathlib import Path

import httpx
import pytest

from deploy.c3_agent.app import AgentRefusal, SubprocessAsUidRunner, create_app
from deploy.c3_agent.config import Settings

TOKEN = "c3-agent-sekret"
NODE = "e2b-worker-0"
GRANT_URL = f"/internal/nodes/{NODE}/agent/grant-slot"


def _settings(**overrides) -> Settings:
    defaults = dict(token=TOKEN, node_id=NODE)
    defaults.update(overrides)
    return Settings(**defaults)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    )


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"X-Internal-Key": token}


def _body(**overrides) -> dict:
    payload = {"sandbox_id": "sbx_grant", "uid": 10007, "pid": 4242}
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


# ------------------------------------------------------------------ the surface


@pytest.mark.asyncio
async def test_grant_slot_runs_as_uid_and_answers_the_instruction() -> None:
    runner = _StubRunner()
    app = create_app(settings=_settings(), runner=runner)
    async with _client(app) as client:
        resp = await client.post(GRANT_URL, headers=_headers(), json=_body())
        assert resp.status_code == 200
        assert resp.json() == {
            "op": "grant-slot",
            "sandboxID": "sbx_grant",
            "uid": 10007,
            "pid": 4242,
            "asUid": "C3-ASUID-OK pid=4242 uid=10007",
        }
    # The uid is the control plane's parameter, handed straight through.
    assert runner.calls == [(10007, 4242)]


@pytest.mark.asyncio
async def test_an_instruction_for_another_node_is_refused_named() -> None:
    """The agent may not be driven on behalf of a node it is not.

    This is the *only* local decision the stateless agent makes: "addressed to
    me?". It is refused before the runner is consulted.
    """
    runner = _StubRunner()
    app = create_app(settings=_settings(), runner=runner)
    async with _client(app) as client:
        resp = await client.post(
            f"/internal/nodes/e2b-worker-1/agent/grant-slot",
            headers=_headers(),
            json=_body(),
        )
        assert resp.status_code == 403
        assert resp.json() == {
            "error": (
                "request is addressed to node e2b-worker-1, but this agent "
                "is node e2b-worker-0"
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
            f"/internal/nodes/{NODE}/agent/delete-tree",
            headers=_headers(),
            json=_body(),
        )
        assert resp.status_code == 404
        assert resp.json() == {"error": "unknown agent op 'delete-tree'"}


@pytest.mark.asyncio
async def test_a_refused_grant_is_fail_closed_and_named() -> None:
    runner = _StubRunner(
        refuse="as_uid refused: uid 999 is outside the privileged helper uid pool"
    )
    app = create_app(settings=_settings(), runner=runner)
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
    and *any* stderr -- stdout is the grant, stderr is a refusal channel.
    """
    with pytest.raises(AgentRefusal, match="exit 77: .*outside the pool"):
        SubprocessAsUidRunner(
            str(_script(tmp_path, "echo 'outside the pool' >&2\nexit 77\n"))
        ).grant(999, 4242)
    with pytest.raises(AgentRefusal, match="unexpected stdout"):
        SubprocessAsUidRunner(
            str(_script(tmp_path, "echo 'C3-ASUID-OK pid=1 uid=2'\n"))
        ).grant(10007, 4242)
    with pytest.raises(AgentRefusal, match="unexpected stdout"):
        SubprocessAsUidRunner(str(_script(tmp_path, "true\n"))).grant(10007, 4242)
    with pytest.raises(AgentRefusal, match="stderr"):
        SubprocessAsUidRunner(
            str(_script(tmp_path, "echo oops >&2\necho 'C3-ASUID-OK pid=4242 uid=10007'\n"))
        ).grant(10007, 4242)


def test_subprocess_runner_refuses_a_missing_binary(tmp_path: Path) -> None:
    """``as_uid`` not installed is a named refusal, never an exception out."""
    with pytest.raises(AgentRefusal, match="could not run"):
        SubprocessAsUidRunner(str(tmp_path / "does-not-exist")).grant(10007, 4242)


# --------------------------------------------------------------------- startup


def test_the_service_refuses_to_start_without_its_own_identity() -> None:
    """The self-check needs a node id; starting without one is a hard error."""
    from deploy.c3_agent.__main__ import _startup_error

    assert _startup_error(_settings(node_id="")) == (
        "E2B_C3_AGENT_NODE_ID (or E2B_NODE_ID) is required; the agent must "
        "know which node it is"
    )
    assert _startup_error(_settings(token="")) == (
        "E2B_C3_AGENT_TOKEN is required; refusing to start without auth"
    )
    assert _startup_error(_settings()) is None
