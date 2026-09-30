"""Face B: the agent's file-operation verbs (C3 Task 4, rulings D18.2/D18.3).

``c3_agent/priv/maint.c`` already carries the three verbs the platform needs --
``chown`` / ``rm`` / ``walk`` -- and the whole path discipline behind them
(``realpath`` + the four-root whitelist + the uid-pool gate, all in
``priv_common.c``). C3 does not re-implement any of that: this module is a
**thin, strict judgement of that binary's answer**, and the service around it
(:mod:`c3_agent.app`) is what turns an instruction into one exec.

Why the agent execs the binary at all, instead of the worker doing it: C3 §11.2
-- the agent is the *executor* of file operations, never a component that hands
an identity to a worker helper. So a request that reaches here has already been
through the control plane, which is the only component that may name a path or
a uid (hard rules 1/3); this module never resolves a path and never invents one.

Two details are the difference between "the same binary" and "the same
behaviour", and both are in :func:`maint_env`:

* the **four roots** and the **uid pool** -- ``priv_common.c`` reads them from
  the environment, and the values must be the ones the DaemonSet's face B was
  given (Task 3 mounted exactly those roots);
* ``E2B_BROKER_WORKER_UID`` / ``E2B_BROKER_WORKER_GID`` -- behind ``serve`` the
  daemon writes the *authenticated peer's* identity into these for every
  request, which is what makes ``chown --worker`` mean "the worker" and what
  lets ``--gid <worker gid>`` pass the group gate. Exec'd directly by the
  agent, which is **root**, neither would be true: ``--worker`` would hand the
  tree to *root*. The control plane therefore carries the worker's identity and
  this module writes it into the child's environment.

  ⚠ **What that identity is -- and is not** (fourth review, ②): it is *not* the
  worker's own claim. A worker reports one, but the control plane **verifies**
  the claim against a trusted source (the worker pod's ``securityContext`` in
  k8s; ``control_plane/worker_identity_source.py``) and stores nothing when it
  cannot, so what arrives here is a deployment fact -- or the operation never
  reaches this process (the file-op endpoint refuses by name for a node with no
  identity). In C1 the same value came from ``SO_PEERCRED``; the frontier this
  module must not widen is "a worker names the identity its privileged steps act
  as", and the CP-side source is what keeps that closed.

Judgement (``maint.c``'s own contract):

* exit 0 is the only success;
* ``chown`` and ``rm`` print **nothing** on success, so any stdout is a
  refusal -- a half-applied step must never read as one that happened;
* ``walk`` prints ``<kind> <uid> <gid> <mode> <size> <path>`` per entry and the
  lines are relayed verbatim (one parser, on the caller's side), but a line
  that is not that shape is refused by name rather than passed off as an
  answer;
* a non-zero exit reaches the control plane as the binary's own words.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from c3_agent.errors import AgentRefusal

#: The verb whitelist (D18.2). Anything else is refused **by name** by the
#: service before this module is consulted -- the same vocabulary ``maint.c``
#: documents, and deliberately not a second one.
FILE_OP_VERBS: tuple[str, ...] = ("chown", "rm", "walk")

#: ``maint.c``'s own refusal exit (``PRIV_EXIT_REFUSED``); named here only so
#: the refusal text of a non-zero exit stays reproducible in tests.
PRIV_EXIT_REFUSED = 77

#: The entry kinds ``maint.c`` emits: directory, regular file, symlink, and
#: ``o`` for "other" -- a fifo, socket or device node. ``o`` is not decoration:
#: a tree that holds one (a sandbox's unix socket, say) makes ``maint.c`` print
#: that kind, and a judged-by-name whitelist that left it out would refuse the
#: whole answer -- degrading ``/metrics`` and the per-sandbox accounting in the
#: agent shape. The old ``priv_helpers`` parser accepted every kind it was given
#: (fourth review, minor), so this is the same vocabulary.
WALK_KINDS = ("d", "f", "l", "o")


class AgentFileOpRefusal(AgentRefusal):
    """A named, fail-closed refusal from one file-operation verb."""


class FileOpShapeRefusal(ValueError):
    """The instruction itself does not name a legal ``maint.c`` call.

    Distinct from :class:`AgentFileOpRefusal` on purpose, and the difference is
    the status code the control plane sees: this one is a *bad instruction*
    (400 -- a relative path, a ``chown`` that names neither or both targets),
    while the other is "the privileged step ran and refused" (502).
    """


@dataclass(frozen=True)
class FileOpInstruction:
    """One control-plane instruction, as this service reads it.

    ``uid`` is the pooled uid the control plane's records named (never 0 --
    the pool gate in ``priv_common.c`` refuses that too), and
    ``worker_owned`` selects ``maint.c``'s ``--worker`` form, which keeps the
    owner as the worker and only scopes the group. Both are the control
    plane's to choose; this service only refuses a body that names neither or
    both.
    """

    sandbox_id: str
    path: str
    uid: int | None = None
    gid: int | None = None
    recursive: bool = False
    worker_owned: bool = False
    #: The worker this instruction acts as, when it acts as one. ``chown``
    #: always does (``--worker`` *is* the identity, and ``--gid``'s gate
    #: compares against the worker's own gid), so its absence is a shape
    #: refusal there. The **self-heal removal** does not: the control plane's
    #: sweep deletes a tree no record claims, as nobody -- carrying a worker
    #: identity there would mean naming one for a step that does not use it
    #: (and a worker that crashed and never came back has none to name).
    worker_uid: int | None = None
    worker_gid: int | None = None


class MaintRunner(Protocol):
    """Runs one ``e2b-maint`` invocation; returns stdout or refuses by name."""

    def run(self, argv: list[str], *, env: Mapping[str, str]) -> str: ...


def maint_env(
    settings, *, worker_uid: int | None, worker_gid: int | None
) -> dict[str, str]:
    """The environment ``e2b-maint`` must see (see the module docstring).

    ``E2B_STATE_BASE`` is set unconditionally to the resolved state base, the
    way :meth:`envd_service.priv_helpers.PrivHelpers.subprocess_env` does: the
    binary reads one variable and falls back to the workspace base only for
    want of a value, and "this deployment has no state base of its own" is
    spelled as the workspace base itself. The other two roots are named only
    when the deployment names them -- an unset image cache must not become a
    whitelisted directory nobody meant.
    """
    env = {
        "E2B_UID_POOL_START": str(settings.uid_pool_start),
        "E2B_UID_POOL_SIZE": str(settings.uid_pool_size),
        "E2B_WORKSPACE_BASE": str(settings.workspace_base),
        "E2B_STATE_BASE": str(settings.state_base or settings.workspace_base),
    }
    if worker_uid is not None and worker_gid is not None:
        # Both halves or neither (the same rule the control plane applies to a
        # worker's own report): behind ``serve`` these come from the
        # authenticated peer, and exec'd directly the binary falls back to its
        # own (root) identity -- which is why a step that acts as the worker
        # must name one, and a step that acts as nobody must not.
        env["E2B_BROKER_WORKER_UID"] = str(worker_uid)
        env["E2B_BROKER_WORKER_GID"] = str(worker_gid)
    if settings.shared_volume_root:
        env["E2B_SHARED_VOLUME_ROOT"] = str(settings.shared_volume_root)
    if settings.image_cache_dir:
        env["E2B_IMAGE_CACHE_DIR"] = str(settings.image_cache_dir)
    return env


def build_maint_argv(verb: str, instruction: FileOpInstruction, *, settings) -> list[str]:
    """The exact argv for one verb -- ``maint.c``'s own flag grammar.

    The order of the flags is not load-bearing for the binary, but it *is*
    load-bearing for the wire contract this repo pins: one builder, so the
    agent's exec and the tests' expectation cannot drift.
    """
    if verb not in FILE_OP_VERBS:
        raise AgentFileOpRefusal(f"unknown e2b-maint verb {verb!r}: refusing")
    argv = [str(settings.maint_path), verb]
    if verb == "chown":
        if instruction.worker_owned:
            argv.append("--worker")
        else:
            argv += ["--uid", str(instruction.uid)]
        if instruction.gid is not None:
            argv += ["--gid", str(instruction.gid)]
        if instruction.recursive:
            argv.append("--recursive")
    argv += ["--path", instruction.path]
    return argv


class SubprocessMaintRunner:
    """The production runner: one ``e2b-maint`` exec, strictly judged.

    ``process_runner`` is the single seam a test replaces; it defaults to
    :func:`subprocess.run`, so the shipped path has no test-only branch in it.
    """

    def __init__(
        self,
        path: str,
        *,
        timeout_s: float = 300.0,
        process_runner=None,
    ) -> None:
        self._path = str(path)
        self._timeout_s = float(timeout_s)
        self._run_process = process_runner or subprocess.run

    def run(self, argv: list[str], *, env: Mapping[str, str]) -> str:
        verb = argv[1] if len(argv) > 1 else ""
        try:
            proc = self._run_process(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
                env=dict(env),
            )
        except OSError as exc:
            # ``strerror`` keeps the message deterministic and
            # operator-sized ("No such file or directory") instead of the
            # platform's OSError repr; the path is already in the message.
            detail = exc.strerror or type(exc).__name__
            raise AgentFileOpRefusal(
                f"could not run e2b-maint at {self._path}: {detail}"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise AgentFileOpRefusal(
                f"could not run e2b-maint at {self._path}: {type(exc).__name__}"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise AgentFileOpRefusal(
                f"e2b-maint {verb} refused (exit {proc.returncode}): {detail}"
            )
        return proc.stdout or ""


def _judge_walk_output(stdout: str) -> None:
    """Every non-empty line must be ``maint.c``'s documented entry shape."""
    for line in stdout.splitlines():
        if not line:
            continue
        parts = line.split(" ", 5)
        ok = (
            len(parts) == 6
            and parts[0] in WALK_KINDS
            and parts[1].isdigit()
            and parts[2].isdigit()
            and _is_octal(parts[3])
            and parts[4].isdigit()
            and parts[5] != ""
        )
        if not ok:
            raise AgentFileOpRefusal(
                "e2b-maint walk answered a line that is not the documented "
                "'<kind> <uid> <gid> <mode> <size> <path>' shape: "
                f"{line!r}: refusing"
            )


def _is_octal(text: str) -> bool:
    return bool(text) and all(character in "01234567" for character in text)


def run_file_op(
    verb: str,
    instruction: FileOpInstruction,
    *,
    runner: MaintRunner,
    settings,
) -> dict[str, Any]:
    """Execute one verb and return the answer the control plane relays.

    Raises :class:`AgentFileOpRefusal` for everything that is not exactly
    ``maint.c``'s success contract -- including "a verb that prints nothing
    printed something" and "a ``walk`` line that is not an entry".
    """
    if not instruction.path.startswith("/"):
        # The realpath + root whitelist is ``maint.c``'s; this is only the
        # shape check that keeps a relative path from being interpreted
        # against whatever working directory the agent happens to have.
        raise FileOpShapeRefusal("path is not an absolute path: refusing")
    if verb == "chown":
        if instruction.worker_owned and instruction.uid is not None:
            raise FileOpShapeRefusal(
                "chown takes uid or worker, not both: refusing"
            )
        if not instruction.worker_owned and instruction.uid is None:
            raise FileOpShapeRefusal(
                "chown needs one of uid (a pooled uid) or worker (the "
                "worker's own identity): refusing"
            )
        if instruction.worker_uid is None or instruction.worker_gid is None:
            # ``--worker`` writes ``priv_worker_uid()`` (the child's
            # environment) and ``--gid`` is checked against the worker's own
            # gid; without that identity the first would name *root* and the
            # second would be refused by the binary. Fail here, by shape.
            raise FileOpShapeRefusal(
                "a chown instruction needs the worker's own identity "
                "(it is the --worker form and the group gate): refusing"
            )
    argv = build_maint_argv(verb, instruction, settings=settings)
    env = maint_env(
        settings,
        worker_uid=instruction.worker_uid,
        worker_gid=instruction.worker_gid,
    )
    stdout = runner.run(argv, env=env)
    if verb == "walk":
        _judge_walk_output(stdout)
        return {"op": "walk", "path": instruction.path, "stdout": stdout}
    if stdout != "":
        raise AgentFileOpRefusal(
            f"e2b-maint {verb} wrote to stdout ({stdout!r}), which its "
            "contract does not: refusing"
        )
    if verb == "rm":
        return {"op": "rm", "path": instruction.path}
    return {
        "op": "chown",
        "path": instruction.path,
        "uid": instruction.uid,
        "gid": instruction.gid
        if instruction.gid is not None
        else (None if instruction.worker_owned else instruction.uid),
        "recursive": instruction.recursive,
    }
