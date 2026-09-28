"""The C3 per-node agent's HTTP service (Task 2, controller ruling D3).

This is the CP→agent half of C3's two channels; there is deliberately **no**
``worker↔agent`` channel (hard rule 5). The agent is **stateless**: the control
plane's instruction carries every parameter, including the pool uid to grant, so
here there is no authorization table, no TTL and no ordering discipline. The
only local decision is whether the instruction is addressed to *this* node.

Surface (all responses JSON objects):

- ``POST /internal/nodes/{node_id}/agent/grant-slot``
  ``{"sandbox_id", "uid", "pid"}`` -> ``{"op", "sandboxID", "uid", "pid",
  "asUid"}``. Runs ``as_uid --uid X --pid N``; the *only* accepted result is
  exit 0 with exactly the ``C3-ASUID-OK pid=N uid=X`` line on stdout and an
  empty stderr. Anything else is a ``502`` named fail-closed refusal (a
  half-applied grant must never read as success).

Auth: every request must carry ``X-Internal-Key`` equal to
``E2B_C3_AGENT_TOKEN`` (constant-time); the service refuses to answer when the
token is unconfigured.

Exposure (Task 3 owns the DaemonSet): this process listens on
``E2B_C3_AGENT_HOST``/``E2B_C3_AGENT_PORT`` -- ``0.0.0.0:49985`` by default,
because the control plane dials it from another pod. The bind address is
configurable on purpose; what must bound the reach is a **NetworkPolicy
allowing only CP→agent** and the rule that ``E2B_C3_AGENT_TOKEN`` never appears
in a worker manifest or the worker image. Neither is built here.

The container-pid → host-pid reverse lookup is **not here**: it belongs to
Task 3, which owns the ``NSpid`` chain plus the target worker pod's cgroup match
and will pass the host pid in. The stub below says so at the call site.
"""

from __future__ import annotations

import logging
import secrets
import subprocess
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from deploy.c3_agent.config import Settings
from gateway_common.paths import validate_sandbox_id

logger = logging.getLogger(__name__)

#: The one line ``as_uid`` prints on success (Task 1).
AS_UID_OK_PREFIX = "C3-ASUID-OK"


class AgentRefusal(Exception):
    """A named, fail-closed refusal from the agent's privileged operation."""


class GrantSlotBody(BaseModel):
    sandbox_id: str = Field(min_length=1)
    uid: int = Field(ge=1)
    pid: int = Field(ge=1)


class AsUidRunner(Protocol):
    """Runs the face-A primitive; ``grant`` returns the OK line or refuses."""

    def grant(self, uid: int, pid: int) -> str: ...


class SubprocessAsUidRunner:
    """The production runner: ``as_uid --uid X --pid N``, strictly judged.

    Acceptance is the primitive's own contract (`deploy/priv/as_uid.c`): exit 0,
    one exact line on stdout, empty stderr. The ``pid`` here is the **host** pid
    Task 3 resolved from the worker's container pid; Task 2 does not do that
    reverse lookup and never invents a pid.
    """

    def __init__(self, path: str, *, timeout_s: float = 5.0) -> None:
        self._path = path
        self._timeout_s = float(timeout_s)

    def grant(self, uid: int, pid: int) -> str:
        expected = f"{AS_UID_OK_PREFIX} pid={pid} uid={uid}"
        try:
            proc = subprocess.run(
                [self._path, "--uid", str(uid), "--pid", str(pid)],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except OSError as exc:
            # ``strerror`` keeps the message deterministic and operator-sized
            # ("No such file or directory") instead of the platform's OSError
            # repr; the path is already in the message.
            detail = exc.strerror or type(exc).__name__
            raise AgentRefusal(
                f"could not run as_uid at {self._path}: {detail}"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise AgentRefusal(
                f"could not run as_uid at {self._path}: {type(exc).__name__}"
            ) from exc
        if proc.returncode != 0:
            raise AgentRefusal(
                f"as_uid exit {proc.returncode}: {proc.stderr.strip()}"
            )
        if proc.stdout != expected + "\n":
            raise AgentRefusal(
                f"as_uid returned an unexpected stdout for uid {uid} pid {pid}: "
                f"{proc.stdout!r}"
            )
        if proc.stderr != "":
            raise AgentRefusal(
                f"as_uid wrote to stderr for uid {uid} pid {pid}: {proc.stderr!r}"
            )
        return expected


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_key(request: Request) -> None:
    settings = _settings(request)
    if not settings.token:
        raise HTTPException(
            status_code=500, detail={"error": "c3-agent token not configured"}
        )
    provided = request.headers.get("X-Internal-Key")
    if provided is None or not _token_matches(provided, settings.token):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def _token_matches(provided: str, expected: str) -> bool:
    """Constant-time token compare; a non-ASCII header is a mismatch, not a 500.

    ``secrets.compare_digest`` raises ``TypeError`` for non-ASCII ``str`` (the
    constant-time path is ASCII/bytes only) and a header is attacker-controlled
    bytes decoded as latin-1.
    """
    try:
        return secrets.compare_digest(provided, expected)
    except TypeError:
        return False


def create_app(
    *, settings: Settings | None = None, runner: AsUidRunner | None = None
) -> FastAPI:
    settings = settings or Settings()
    runner = runner or SubprocessAsUidRunner(
        settings.as_uid_path, timeout_s=settings.as_uid_timeout_s
    )
    # No interactive surface: the instruction API is the whole contract, and
    # `/openapi.json`/`/docs` on a privileged service is inventory for free.
    app = FastAPI(
        title="E2B Sandlock C3 Agent",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.runner = runner

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    @app.post("/internal/nodes/{node_id}/agent/{op}")
    def agent_op(node_id: str, op: str, request: Request, body: GrantSlotBody) -> dict[str, Any]:
        _require_key(request)
        # The agent's only local decision: is this instruction for *me*? It is
        # refused before the op vocabulary is even consulted.
        if node_id != settings.node_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": (
                        f"request is addressed to node {node_id}, but this "
                        f"agent is node {settings.node_id}"
                    )
                },
            )
        if op != "grant-slot":
            raise HTTPException(
                status_code=404, detail={"error": f"unknown agent op {op!r}"}
            )
        # Same shape rule the control plane uses: a hostile id must not reach a
        # log line (or a later path/lookup) verbatim. The message deliberately
        # does not echo it back.
        if not validate_sandbox_id(body.sandbox_id):
            logger.warning(
                "c3-agent refused grant-slot: sandbox_id is not a valid sandbox id"
            )
            raise HTTPException(
                status_code=400,
                detail={"error": "sandbox_id is not a valid sandbox id"},
            )
        try:
            line = runner.grant(body.uid, body.pid)
        except AgentRefusal as exc:
            logger.warning(
                "c3-agent refused grant-slot for sandbox %s: %s",
                body.sandbox_id,
                exc,
            )
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {
            "op": "grant-slot",
            "sandboxID": body.sandbox_id,
            "uid": body.uid,
            "pid": body.pid,
            "asUid": line,
        }

    return app
