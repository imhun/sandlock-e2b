"""The C3 per-node agent's HTTP service (Task 2, controller ruling D3).

This is the CP→agent half of C3's two channels; there is deliberately **no**
``worker↔agent`` channel (hard rule 5). The agent is **stateless**: the control
plane's instruction carries every parameter, including the pool uid to grant, so
here there is no authorization table, no TTL and no ordering discipline. The
only local decision is whether the instruction is addressed to *this* node.

**The node id in the URL is this agent's own identity, and that identity is the
host** (ruling D12): one agent per node, while a node may run several workers.
In k8s it is ``spec.nodeName`` (the DaemonSet's downward API); in compose it is
the agent's service name. The **worker's** identity -- its node id (== pod
name), its pod UID and the pid namespace it recorded -- travels inside the
instruction body, and that is what the reverse lookup matches against. There is
deliberately no check of "does the body's worker match the URL": they name two
different things, and the control plane -- not this service -- is the authority
that paired them.

Surface (all responses JSON objects):

- ``POST /internal/nodes/{node_id}/agent/grant-slot``
  ``{"sandbox_id", "uid", "pid", "worker"}`` -> ``{"op", "sandboxID", "uid",
  "pid", "hostPid", "pidNamespace", "asUid"}``. ``pid`` is the pid the *worker*
  knows (its own pid namespace); ``worker`` is who the control plane says that
  pid belongs to. ``node_id`` in the path is *this host* (D12). The agent
  resolves the host pid first
  (:mod:`c3_agent.lookup`), then runs ``as_uid --uid X --pid <host>``;
  the *only* accepted result is exit 0 with exactly the ``C3-ASUID-OK
  pid=N uid=X`` line on stdout and an empty stderr. Anything else is a ``502``
  named fail-closed refusal (a half-applied grant must never read as success).
- ``POST /internal/nodes/{node_id}/agent/{chown|rm|walk}`` (face B, Task 4)
  ``{"sandbox_id", "path", "uid"?, "gid"?, "recursive"?, "worker_owned"?,
  "worker": {"uid", "gid", "node_id"?, "container_id"?}}`` -> the verb's own
  answer. The **verb list is the whitelist** (D18.2) and an unknown verb is
  refused by name; ``path`` and ``uid`` are the control plane's values (hard
  rules 1/3 -- the worker never names either), and the path discipline is
  ``e2b-maint``'s (:mod:`c3_agent.fileops` execs that same binary with
  the same roots).

  ``worker.container_id`` is the compose lane's anchor (rulings D21 option 2
  and **D25**): when it is present the worker's uid/gid are read **from the
  kernel** -- candidates matched by the container id that appears in their
  world-readable ``/proc/<pid>/cgroup``, the identity then read from
  ``/proc/<pid>/status``
  (:meth:`c3_agent.lookup.ProcLookup.worker_uid_gid`) -- and the values
  in the body are only a claim to be confirmed. A claim the kernel does not
  confirm -- or an anchor no process carries, or candidates whose identities
  disagree -- is a named 502 and no ``e2b-maint`` runs. No capability and no
  uid change is involved, which is why face B keeps its three-capability set
  (judgment 4). The k8s lane sends no anchor: its value was verified by the
  control plane against the worker pod's ``securityContext``, and it is used
  exactly as sent.

Auth: every request must carry ``X-Internal-Key`` equal to
``E2B_C3_AGENT_TOKEN`` (constant-time); the service refuses to answer when the
token is unconfigured.

Task 6 adds the one connection the agent *initiates*: the periodic inventory
scan (:mod:`c3_agent.scan`) reports the sandbox-shaped trees it can see
to ``POST /internal/nodes/{host}/agent/inventory``, and the control plane
answers with its decision. The report carries ids and nothing else, the agent
decides nothing, and it removes nothing on its own -- the removal is still one
``e2b-maint rm`` under a control-plane instruction with a control-plane path.

Exposure (Task 3 owns the DaemonSet): this process listens on
``E2B_C3_AGENT_HOST``/``E2B_C3_AGENT_PORT`` -- ``0.0.0.0:49985`` by default,
because the control plane dials it from another pod. The bind address is
configurable on purpose; what must bound the reach is a **NetworkPolicy
allowing only CP→agent** and the rule that ``E2B_C3_AGENT_TOKEN`` never appears
in a worker manifest or the worker image. Neither is built here.

The container-pid → host-pid reverse lookup lives beside this module, in
``c3_agent/lookup.py``: it is a pure function of a ``/proc`` tree so the
DaemonSet drives the same code the lanes drive against a synthetic one.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from c3_agent.config import Settings
from c3_agent.errors import AgentRefusal
from c3_agent.fileops import (
    FILE_OP_VERBS,
    AgentFileOpRefusal,
    FileOpInstruction,
    FileOpShapeRefusal,
    MaintRunner,
    SubprocessMaintRunner,
    run_file_op,
)
from c3_agent.lookup import (
    LookupRefusal,
    ProcLookup,
    ProcWorkerIdentityResolver,
    WorkerAnchor,
    WorkerIdentity,
    WorkerIdentityResolver,
    missing_slot_pid_message,
)
from c3_agent.materialize import (
    PARTIAL_COPY,
    MaterializeRefusal,
    materialize_tree,
)
from c3_agent.scan import InventoryScanner, scanner_for
from gateway_common.create_grant import GrantRefusal, verify
from gateway_common.paths import validate_node_id, validate_sandbox_id

logger = logging.getLogger(__name__)

#: The one line ``as_uid`` prints on success (Task 1).
AS_UID_OK_PREFIX = "C3-ASUID-OK"

#: Distinguishes "the caller said nothing, build the default scanner" from
#: "the caller says this container does not scan" (``None``) -- the same
#: injection shape ``control_plane/app.py`` uses for its registries.
_UNSET: Any = object()


class WorkerInstruction(BaseModel):
    """Who the control plane says the reported container pid belongs to.

    ``node_id`` is the **worker's** identity -- its node id, which is the
    StatefulSet pod name -- not the node this agent runs on (D12: the URL says
    which host; this field says which worker *on* that host).

    ``pid_namespace`` is the worker's own reported identity (the one value that
    exists in both lanes and in both directions -- ruling D9.3); ``pod_uid`` is
    the k8s lane's independent proof, resolved by the control plane from the pod
    API. Both are required *when their lane has them*: a body without the
    identity is rejected by the schema, and a lane that cannot supply one is
    refused by the control plane before this service is dialled.
    """

    node_id: str = Field(min_length=1)
    pid_namespace: str = Field(min_length=1)
    pod_uid: str | None = None


class GrantSlotBody(BaseModel):
    sandbox_id: str = Field(min_length=1)
    uid: int = Field(ge=1)
    pid: int = Field(ge=1)
    worker: WorkerInstruction


class WorkerCredentials(BaseModel):
    """The worker's own identity, as the control plane's node record holds it.

    Both halves are needed for face B and neither may come from the agent's own
    ``getuid()``/``getgid()``: the agent is root, so its own identity would turn
    ``chown --worker`` into "hand the tree to root" (see
    :mod:`c3_agent.fileops`).

    ``container_id`` is present exactly when the control plane's shape could
    not verify the claim itself (the compose lane: no pod spec to read) and is
    therefore asking the agent to confirm it against the kernel (rulings D21
    option 2 and D25). Its presence is what selects that path, so a k8s
    instruction -- whose value the control plane already verified -- carries
    none and behaves exactly as it did before. ``node_id`` is the *worker's*
    name, carried only so a kernel-side refusal can name it (the URL carries
    the agent's own identity, which is a different fact -- D12).
    """

    uid: int = Field(ge=1)
    gid: int = Field(ge=1)
    node_id: str | None = Field(default=None, min_length=1)
    container_id: str | None = Field(default=None, min_length=1)


class FileOpBody(BaseModel):
    """One file-operation instruction (face B).

    ``path`` is the **control plane's** value -- derived from its own records
    and settings (hard rule 3 / C3 §14.4) -- and this service only shape-checks
    it; ``uid`` is the pooled uid those records named. ``worker_owned`` selects
    ``maint.c``'s ``--worker`` form (the slot documents: owner stays the
    worker, only the group moves), which is why it is a separate field from
    ``uid`` rather than a sentinel value.
    """

    sandbox_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    uid: int | None = Field(default=None, ge=1)
    gid: int | None = Field(default=None, ge=1)
    recursive: bool = False
    worker_owned: bool = False
    #: The worker this instruction acts as, for the verbs that act as one
    #: (``chown``: the ``--worker`` form and the group gate). A **self-heal
    #: removal** carries none, and it may: the control plane's sweep deletes a
    #: tree no record claims, as nobody, and a worker that crashed and never
    #: came back has no identity to name.
    worker: WorkerCredentials | None = None


class AsUidRunner(Protocol):
    """Runs the face-A primitive; ``grant`` returns the OK line or refuses."""

    def grant(self, uid: int, pid: int) -> str: ...


class SubprocessAsUidRunner:
    """The production runner: ``as_uid --uid X --pid N``, strictly judged.

    Acceptance is the primitive's own contract (`c3_agent/priv/as_uid.c`): exit 0,
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
    *,
    settings: Settings | None = None,
    runner: AsUidRunner | None = None,
    maint_runner: MaintRunner | None = None,
    lookup: ProcLookup | None = None,
    identity_resolver: WorkerIdentityResolver | None = None,
    inventory: Any = _UNSET,
) -> FastAPI:
    settings = settings or Settings()
    runner = runner or SubprocessAsUidRunner(
        settings.as_uid_path, timeout_s=settings.as_uid_timeout_s
    )
    # Face B: the same binary the worker's broker execs, judged the same strict
    # way (``maint.c`` prints nothing on success, one entry line per node for
    # ``walk``). The roots and the uid pool travel in the child's environment,
    # so nothing here resolves a path.
    maint_runner = maint_runner or SubprocessMaintRunner(
        settings.maint_path, timeout_s=settings.maint_timeout_s
    )
    # The host's process table: face A runs with ``hostPID: true`` (Task 3's
    # DaemonSet), which is what makes the worker's container pid visible here.
    lookup = lookup or ProcLookup()
    # The compose lane's worker identity (D21 option 2): read from the kernel by
    # a child that runs as the workers' own identity, because the kernel only
    # lets a process read the namespace of a process whose identity matches its
    # own (and face B, deliberately, has neither the workers' uid nor
    # ``CAP_SYS_PTRACE``). The k8s lane never reaches it -- its instructions
    # carry no anchor.
    identity_resolver = identity_resolver or ProcWorkerIdentityResolver(lookup)
    # Task 6's eyes: built from the container's own knobs unless the caller
    # injected one (tests, an embedder) -- and ``None`` means "this container
    # does not scan", which only the face that mounts the workspaces should be
    # asked to do (the manifests say so by name).
    if inventory is _UNSET:
        inventory = scanner_for(settings)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        scanner: InventoryScanner | None = app.state.inventory
        stop = asyncio.Event()
        task: asyncio.Task | None = None
        if scanner is not None:
            task = asyncio.create_task(scanner.run(stop))
            logger.info(
                "c3-agent inventory: the scan loop started (first round in "
                "%.0fs, then every %.0fs, deferral backoff capped at %.0fs)",
                scanner.schedule.first_delay_s,
                scanner.schedule.interval_s,
                scanner.schedule.backoff_max_s,
            )
        try:
            yield
        finally:
            if task is not None:
                stop.set()
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    # No interactive surface: the instruction API is the whole contract, and
    # `/openapi.json`/`/docs` on a privileged service is inventory for free.
    app = FastAPI(
        title="E2B Sandlock C3 Agent",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.runner = runner
    app.state.maint_runner = maint_runner
    app.state.lookup = lookup
    app.state.identity_resolver = identity_resolver
    app.state.inventory = inventory

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    @app.post("/internal/nodes/{node_id}/agent/{op}")
    async def agent_op(node_id: str, op: str, request: Request) -> dict[str, Any]:
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
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(
                status_code=400, detail={"error": "invalid JSON body"}
            ) from None
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"error": f"the {op} instruction must be a JSON object"},
            )
        # D18.2: a verb whitelist with explicitly named verbs. An unknown op is
        # refused by name here, before any body is interpreted.
        #
        # Both ops run their privileged work in a worker thread (fourth review,
        # ①): the FastAPI handler is ``async`` only because reading the body is,
        # and ``SubprocessAsUidRunner``/``SubprocessMaintRunner`` are synchronous
        # ``subprocess.run`` calls -- 5 s for ``as_uid``, up to 300 s for a
        # ``chown``/``rm``/``walk``. Inline they would run on uvicorn's single
        # event loop, so one teardown or tree walk would stop the agent from
        # accepting connections at all: concurrent slot grants would blow through
        # the control plane's 5 s deadline as 504s, and the thread-pool premise
        # written for ``E2B_C3_AGENT_MAX_CONCURRENCY`` (control_plane/config.py)
        # would be false. ``to_thread`` is what keeps that reasoning true.
        if op == "grant-slot":
            return await asyncio.to_thread(
                _grant_slot, _validated(GrantSlotBody, body)
            )
        if op in FILE_OP_VERBS:
            return await asyncio.to_thread(_file_op, op, _validated(FileOpBody, body))
        raise HTTPException(
            status_code=404, detail={"error": f"unknown agent op {op!r}"}
        )

    def _validated(model, body: dict[str, Any]):
        """The per-op body model, or the 4xx the wire contract names.

        A body that does not fit its op is a *shape* refusal, so a missing or
        misspelled field answers 422 (what the single-op service answered when
        FastAPI did this) instead of surfacing as an unhandled error.
        """
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": f"the instruction body does not fit {model.__name__}"},
            ) from exc

    def _file_op(op: str, body: FileOpBody) -> dict[str, Any]:
        if not validate_sandbox_id(body.sandbox_id):
            logger.warning("c3-agent refused %s: sandbox_id is not valid", op)
            raise HTTPException(
                status_code=400,
                detail={"error": "sandbox_id is not a valid sandbox id"},
            )
        worker_uid, worker_gid = _worker_identity(op, body)
        instruction = FileOpInstruction(
            sandbox_id=body.sandbox_id,
            path=body.path,
            uid=body.uid,
            gid=body.gid,
            recursive=body.recursive,
            worker_owned=body.worker_owned,
            worker_uid=worker_uid,
            worker_gid=worker_gid,
        )
        try:
            return run_file_op(
                op, instruction, runner=maint_runner, settings=settings
            )
        except FileOpShapeRefusal as exc:
            # A bad instruction, not a refused privileged step: the control
            # plane sent a shape ``maint.c`` has no call for.
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except AgentFileOpRefusal as exc:
            logger.warning(
                "c3-agent refused %s for sandbox %s: %s",
                op,
                body.sandbox_id,
                exc,
            )
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    def _worker_identity(op: str, body: FileOpBody) -> tuple[int | None, int | None]:
        """The identity ``maint.c`` must act as, kernel-confirmed when anchored.

        Two shapes reach here, and the instruction itself says which:

        * **no anchor** -- the k8s lane. The control plane verified the value
          against the worker pod's ``securityContext``, so it is used as sent
          (this is the pre-existing behaviour, unchanged);
        * **an anchor** -- the compose lane (D21 option 2, D25). The value
          sent is a *claim*: the kernel's answer for the worker's own processes
          is the identity, and a claim the kernel does not confirm is refused
          **by name**, before any ``chown``/``rm``/``walk`` is exec'd.

        A refusal is a 502 with the kernel's own words, the same status a
        refused ``e2b-maint`` step gets: the control plane must not read "the
        identity could not be confirmed" as "the step happened".
        """
        if body.worker is None:
            return None, None
        anchor = body.worker.container_id
        if anchor is None:
            return body.worker.uid, body.worker.gid
        if body.worker.node_id is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        "a kernel-anchored worker instruction must name the "
                        "worker (worker.node_id): refusing"
                    )
                },
            )
        identity = WorkerAnchor(
            node_id=body.worker.node_id, container_id=anchor
        )
        try:
            return identity_resolver.resolve(
                identity, claimed=(body.worker.uid, body.worker.gid)
            )
        except LookupRefusal as exc:
            logger.warning(
                "c3-agent refused %s for sandbox %s: %s",
                op,
                body.sandbox_id,
                exc,
            )
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    def _grant_slot(body: GrantSlotBody) -> dict[str, Any]:
        # D12: the path's node id is *this host* while `worker.node_id` is the
        # worker pod that reported the pid -- two different names, so they are
        # not compared. What is checked is that the worker identity is a shape
        # that can be named in a log line at all; the proof that the pid really
        # belongs to that worker is the pid namespace (and, in k8s, the pod UID
        # -> cgroup) that the lookup matches, both of which come from the
        # control plane.
        if not validate_node_id(body.worker.node_id):
            logger.warning(
                "c3-agent refused grant-slot: the worker identity is not a "
                "node id"
            )
            raise HTTPException(
                status_code=400,
                detail={"error": "worker.node_id is not a valid node id"},
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
        identity = WorkerIdentity(
            node_id=body.worker.node_id,
            pid_namespace=body.worker.pid_namespace,
            pod_uid=body.worker.pod_uid,
        )
        try:
            slot = lookup.host_pid(
                body.pid, identity, sandbox_id=body.sandbox_id
            )
        except LookupRefusal as exc:
            logger.warning(
                "c3-agent refused grant-slot for sandbox %s: %s",
                body.sandbox_id,
                exc,
            )
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        try:
            line = runner.grant(body.uid, slot.host_pid)
        except AgentRefusal as exc:
            if not lookup.still_alive(slot):
                # The child ended (or its pid was recycled) between the report
                # and the write. Name that, rather than forwarding the
                # primitive's reading of a missing ``/proc`` entry (D9.5):
                # "the slot's pid is gone" is the one failure an operator has to
                # be able to grep for.
                logger.warning(
                    "c3-agent refused grant-slot for sandbox %s: the host pid "
                    "%d is gone",
                    body.sandbox_id,
                    slot.host_pid,
                )
                raise HTTPException(
                    status_code=502,
                    detail={"error": missing_slot_pid_message(body.sandbox_id)},
                ) from exc
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
            "hostPid": slot.host_pid,
            "pidNamespace": body.worker.pid_namespace,
            "asUid": line,
        }

    #: The single-use table: one entry per accepted ``jti``, dropped once the
    #: grant it belongs to has expired (plus a margin, so two nodes' clocks do
    #: not have to agree for a *replay* to be refused -- the TTL is already
    #: over by then). Kept on ``state`` rather than the module so two apps in
    #: one process (tests, an embedder) cannot spend each other's grants.
    spent_grants: dict[str, float] = {}
    app.state.spent_grants = spent_grants

    @app.post("/internal/grants/file-op")
    async def grant_file_op(request: Request) -> dict[str, Any]:
        """Run one ``materialize-tree`` plan a worker carried here itself.

        This is the create path's shortcut around the control plane's relay
        (design §4.1): the worker mints a plan from the control plane, then
        delivers it to its **own** node's agent, so the expensive
        worker→CP→agent→CP→worker loop happens once (the mint) instead of once
        per step.

        It deliberately does **not** reuse ``_require_key``: the plan *is* the
        credential, and it is a better one than the fleet-wide key -- signed,
        addressed to this host by name, short-lived, single-use, and valid for
        exactly one op whose every path the control plane derived. The body
        carries nothing else, so there is nothing for a caller to steer.

        Every check is fail-closed and named, and the order matters: the
        signature is checked before the payload is read, the host before the
        clock, and the ``jti`` is spent **before** any privileged work starts
        (so a replay during the first request's flight is refused rather than
        racing it).
        """
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(
                status_code=400, detail={"error": "invalid JSON body"}
            ) from None
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail={"error": "a grant instruction must be a JSON object"},
            )
        extra = set(body) - {"grant"}
        if extra:
            # Refused by name: an ignored ``path`` would leave its author
            # believing it had been considered (the same rule every other
            # surface here uses).
            raise HTTPException(
                status_code=400,
                detail={
                    "error": (
                        "a grant instruction carries only its grant: refusing "
                        + ", ".join(sorted(extra))
                    )
                },
            )
        token = body.get("grant")
        if not isinstance(token, str) or not token:
            raise HTTPException(
                status_code=400,
                detail={"error": "a grant instruction must carry its grant"},
            )
        if not settings.token:
            raise HTTPException(
                status_code=500,
                detail={"error": "c3-agent token not configured"},
            )
        try:
            payload = verify(token, secret=settings.token, host=settings.node_id)
        except GrantRefusal as exc:
            logger.warning("c3-agent refused a create grant: %s", exc)
            raise HTTPException(
                status_code=_grant_status(exc.reason),
                detail={"error": str(exc)},
            ) from exc
        jti = payload.get("jti")
        exp = payload.get("exp")
        if not isinstance(jti, str) or not jti:
            raise HTTPException(
                status_code=400, detail={"error": "the grant names no jti"}
            )
        now = time.time()
        for spent in [key for key, deadline in spent_grants.items() if deadline < now]:
            spent_grants.pop(spent, None)
        if jti in spent_grants:
            # Named, and actionable: the worker's rule is "mint a new one and
            # retry once" -- never "replay this one" (design §4.2).
            logger.warning(
                "c3-agent refused a create grant for sandbox %s: already used",
                payload.get("sandbox_id"),
            )
            raise HTTPException(
                status_code=409, detail={"error": "grant already used"}
            )
        spent_grants[jti] = float(exp if isinstance(exp, (int, float)) else now)
        try:
            answer = await asyncio.to_thread(
                materialize_tree, payload, settings=settings, runner=maint_runner
            )
        except MaterializeRefusal as exc:
            logger.warning(
                "c3-agent refused to materialize sandbox %s: %s",
                payload.get("sandbox_id"),
                exc,
            )
            raise HTTPException(
                status_code=_materialize_status(exc.reason),
                detail={"error": str(exc)},
            ) from exc
        except AgentFileOpRefusal as exc:
            # The privileged step ran and refused (a 502, the same status the
            # relayed path gives): "the step failed" must never read as "the
            # tree is ready".
            logger.warning(
                "c3-agent refused to materialize sandbox %s: %s",
                payload.get("sandbox_id"),
                exc,
            )
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        except FileOpShapeRefusal as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"materialized": answer}

    return app


def _grant_status(reason: str) -> int:
    """The status for one ``GrantRefusal`` reason.

    ``wrong-host`` is an authorization answer (the credential is genuine but
    belongs to another node), while everything else is the credential's
    authenticity or validity -- 401. ``op-not-allowed`` is a bad *plan* (400),
    not a bad credential.
    """
    if reason == "wrong-host":
        return 403
    if reason == "op-not-allowed":
        return 400
    return 401


def _materialize_status(reason: str) -> int:
    """The status for one ``MaterializeRefusal`` reason.

    A half-applied copy is a *failed privileged step* (502: the caller must not
    read it as success), while a path outside the roots or a destination that
    is a symlink is a plan this agent will not act on at all (400).
    """
    if reason == PARTIAL_COPY:
        return 502
    return 400
