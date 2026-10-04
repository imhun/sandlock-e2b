"""The worker's file-operation client: ``{sandbox_id, op}`` out, results back.

This is the worker's half of C3's face B. Everything privileged the worker
still has to *ask for* -- handing a tree to a sandbox's uid, removing one,
measuring one, scoping a slot's documents -- travels from here to the control
plane, which derives the path and instructs the agent that executes it
(``control_plane/api/internal.py::node_file_op``).

Two properties are the point of the module, and they are why it is a separate
client rather than a transport inside :mod:`envd_service.priv_helpers`:

* the request carries **no path and no uid** -- only ``sandbox_id``, the op
  name and the op's own parameters (hard rules 1/3, §14.4). The old transport
  handed ``e2b-maint`` an argv built *here*, which is exactly what the control
  plane may no longer accept;
* the op vocabulary is a closed list (D18.2). An op this module does not know
  is not sent; an op the control plane does not know is refused by name.

Failures are named and fail-closed (D18.1): the caller must never be able to
read "the control plane refused / could not be reached" as "the step
happened", and there is deliberately **no** fallback to a local privileged
call. The shape is selected once at startup by ``E2B_PRIV_HELPER_TRANSPORT=agent``
(``configure``), so a deployment either routes every file step to the agent or
keeps the pre-C3 shape -- never a mixture decided per call.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from gateway_common import create_trace

logger = logging.getLogger(__name__)

#: One deadline for a file operation, from the worker's side. It has to outlast
#: the control plane's own agent deadline (``E2B_C3_AGENT_FILE_OP_TIMEOUT_S``),
#: or this side would abandon a step that is still running -- and on this path
#: "abandoned" does not mean "stopped".
DEFAULT_TIMEOUT_S = 660.0

#: The connect phase gets its own, short deadline (C3 Task 4 review, N2). A
#: control plane that is not *answering* is entitled to the file-op deadline --
#: a big tree takes minutes -- but one that cannot be *reached* is known in
#: seconds, and in that case the caller is usually a request handler that must
#: not park the worker's event loop behind it.
DEFAULT_CONNECT_TIMEOUT_S = 5.0


class AgentFileOpsError(RuntimeError):
    """A named, fail-closed failure of one file operation."""


class AgentFileOpsUnknownSandbox(AgentFileOpsError):
    """The control plane has no record of this sandbox (HTTP 404).

    Its own type because the two failure modes ask for opposite reactions:
    an unreachable control plane is retryable, while a *definite* "no such
    sandbox" means nothing will ever authorize an operation on that id again.
    The worker's runtime record is its claim on a tree, and a claim the
    control plane does not recognize can only produce work that cannot
    succeed -- measured on the fleet 2026-10-01 as a 404 storm (every disk
    round walking a forgotten sandbox, ~5 requests/s, for hours) that started
    with exactly this answer being treated as a transient error.
    """


class AgentFileOps:
    """The worker's client for face B."""

    def __init__(
        self,
        *,
        control_plane_url: str,
        node_id: str,
        internal_key: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport=None,
    ) -> None:
        self._url = str(control_plane_url).rstrip("/")
        self._node_id = str(node_id)
        self._internal_key = internal_key or ""
        self._timeout_s = float(timeout_s)
        self._connect_timeout_s = min(DEFAULT_CONNECT_TIMEOUT_S, self._timeout_s)
        self._transport = transport
        #: The keep-alive client, built on the first op (see :meth:`_http`).
        self._client = None
        #: One worker runs creates, teardowns and disk-scan rounds on several
        #: threads at once, and they all come through here: the guard is what
        #: keeps the lazy build from happening twice (the loser's socket would
        #: simply be dropped, but nothing should build one it never uses).
        self._client_lock = threading.Lock()
        #: Every op this client will send. Named here so the whitelist is
        #: readable in one place (and so a typo is a refusal, not a 400 from the
        #: far side that reads like a bug).
        self.ops: frozenset[str] = frozenset(
            {
                "chown-workspace",
                "remove-workspace",
                "walk-workspace",
                "remove-runtime",
                "chown-checkpoint",
                "remove-checkpoint",
                "walk-checkpoint",
                "chown-volume-slice",
                "chown-volume-root",
                "remove-volume-slice",
                "chown-secret",
                "scope-slot-document",
            }
        )

    # ------------------------------------------------------------- the wire

    def _http(self):
        """The one client every op on this worker goes through.

        Built lazily and kept: this worker asks the control plane for a file
        operation on *every* create (the ownership hand-over), on every
        teardown, and on every disk-scan round, and the old shape built a
        fresh ``httpx.Client`` per call -- a new TCP connection (and a DNS
        lookup of the control plane's Service name) each time. Measured on the
        fleet 2026-10-01: 27 ms for a ``walk-workspace`` that did nothing,
        against 49 ms for the ``chown`` that actually walked the tree; the
        difference is connection setup, not work.

        The timeout is per request, so reusing the client cannot leak a
        longer budget into one op than the code below asks for.
        """
        import httpx

        client = self._client
        if client is None:
            with self._client_lock:
                client = self._client
                if client is None:
                    # ``httpx.Timeout`` and not the bare float: the read phase
                    # is the operation's budget, the connect phase is this
                    # service's reachability (see ``DEFAULT_CONNECT_TIMEOUT_S``).
                    client = httpx.Client(
                        timeout=httpx.Timeout(
                            self._timeout_s, connect=self._connect_timeout_s
                        ),
                        transport=self._transport,
                    )
                    self._client = client
        return client

    def close(self) -> None:
        """Drop the keep-alive connection (shutdown path; idempotent)."""
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing must never raise here
                pass

    def request(self, op: str, sandbox_id: str, **params: Any) -> dict[str, Any]:
        """One op, one round trip; every failure named (D18.1)."""
        if op not in self.ops:
            raise AgentFileOpsError(
                f"{op!r} is not a file operation this worker may ask for"
            )
        import httpx

        url = f"{self._url}/internal/nodes/{self._node_id}/file-op"
        body = {"op": op, "sandbox_id": sandbox_id, **params}
        started = time.monotonic()
        try:
            response = self._http().post(
                url, json=body, headers={"X-Internal-Key": self._internal_key}
            )
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise AgentFileOpsError(
                f"the control plane is unreachable for {op} on sandbox "
                f"{sandbox_id}: {detail}"
            ) from exc
        finally:
            create_trace.stage(f"fileop:{op}", sandbox_id, started)
        if response.status_code >= 300:
            detail = _error_detail(response)
            message = (
                f"the control plane refused {op} for sandbox {sandbox_id} "
                f"(HTTP {response.status_code}): {detail}"
            )
            if response.status_code == 404:
                # "No such sandbox" is not "the control plane is broken": the
                # caller that holds a *record* for it has to drop the record
                # rather than retry (see the class docstring).
                raise AgentFileOpsUnknownSandbox(message)
            raise AgentFileOpsError(message)
        try:
            answer = response.json()
        except ValueError as exc:
            raise AgentFileOpsError(
                f"the control plane answered {op} for sandbox {sandbox_id} "
                "with a non-JSON body"
            ) from exc
        if not isinstance(answer, dict):
            raise AgentFileOpsError(
                f"the control plane answered {op} for sandbox {sandbox_id} "
                f"with a {type(answer).__name__}, not an answer"
            )
        return answer

    # ------------------------------------------------------------ the verbs

    def chown_workspace(self, sandbox_id: str, *, recursive: bool = True) -> None:
        self.request("chown-workspace", sandbox_id, recursive=bool(recursive))

    def remove_workspace(self, sandbox_id: str) -> None:
        self.request("remove-workspace", sandbox_id)

    def walk_workspace(self, sandbox_id: str) -> list[str]:
        return self._walk("walk-workspace", sandbox_id)

    def workspace_bytes(self, sandbox_id: str) -> int:
        """Bytes under a sandbox's tree, from the agent's ``walk`` answer.

        The same quantity ``priv_helpers.dir_size`` computes -- every entry's
        size, directories included (N31's fix 2) -- using the same parser, so
        the two shapes of one deployment answer the same number.
        """
        return _sum_entries(self.walk_workspace(sandbox_id))

    def remove_runtime(self, sandbox_id: str) -> None:
        self.request("remove-runtime", sandbox_id)

    def chown_checkpoint(self, sandbox_id: str, *, recursive: bool = True) -> None:
        self.request("chown-checkpoint", sandbox_id, recursive=bool(recursive))

    def remove_checkpoint(self, sandbox_id: str) -> None:
        self.request("remove-checkpoint", sandbox_id)

    def walk_checkpoint(self, sandbox_id: str) -> list[str]:
        return self._walk("walk-checkpoint", sandbox_id)

    def checkpoint_bytes(self, sandbox_id: str) -> int:
        return _sum_entries(self.walk_checkpoint(sandbox_id))

    def chown_volume_slice(
        self, sandbox_id: str, volume: str, *, recursive: bool = True
    ) -> None:
        self.request(
            "chown-volume-slice",
            sandbox_id,
            volume=volume,
            recursive=bool(recursive),
        )

    def chown_volume_root(self, sandbox_id: str, volume: str) -> None:
        self.request("chown-volume-root", sandbox_id, volume=volume)

    def remove_volume_slice(self, sandbox_id: str, volume: str) -> None:
        self.request("remove-volume-slice", sandbox_id, volume=volume)

    def chown_secret(self, sandbox_id: str, name: str) -> None:
        self.request("chown-secret", sandbox_id, name=name)

    def scope_slot_document(self, sandbox_id: str, name: str) -> None:
        self.request("scope-slot-document", sandbox_id, name=name)

    def _walk(self, op: str, sandbox_id: str) -> list[str]:
        answer = self.request(op, sandbox_id)
        stdout = answer.get("stdout")
        if not isinstance(stdout, str):
            raise AgentFileOpsError(
                f"the control plane's {op} answer for sandbox {sandbox_id} "
                "carried no entry text"
            )
        return [line for line in stdout.splitlines() if line]


def _error_detail(response) -> str:
    """The refusal's own words: ``message`` (CP) or ``error`` (agent)."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(payload, dict):
        for key in ("message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text.strip()


def _sum_entries(lines: list[str]) -> int:
    """``maint walk``'s line shape, summed with the platform's own parser."""
    from envd_service.priv_helpers import WalkEntry

    return sum(WalkEntry.parse(line).size for line in lines)


# ------------------------------------------------------------- the singleton

_ACTIVE: list[AgentFileOps | None] = [None]

#: The transport value that selects this shape (``E2B_PRIV_HELPER_TRANSPORT``).
AGENT_TRANSPORT = "agent"


def transport_setting(settings) -> str:
    """``E2B_PRIV_HELPER_TRANSPORT`` -- the same switch the exec shape uses.

    Read from the environment, like ``priv_helpers._transport_setting`` (the two
    share ``TRANSPORT_ENV`` and the accepted values): the worker's ``Settings``
    has no field for it (it is a *shape* choice, made once at startup), and a
    second copy in a dataclass field could only drift from the value the
    resolver actually reads. An explicit attribute, when a test or an embedder
    sets one, wins -- that is the one way to make the shape observable without
    an environment variable.
    """
    from envd_service.priv_helpers import TRANSPORT_ENV

    value = getattr(settings, "priv_helper_transport", None) or os.environ.get(
        TRANSPORT_ENV, "auto"
    )
    return str(value or "auto").lower()


def enabled(settings) -> bool:
    """Whether this deployment routes its file operations to the agent."""
    return transport_setting(settings) == AGENT_TRANSPORT


def configure(
    settings,
    *,
    control_plane_url: str | None = None,
    node_id: str | None = None,
    transport=None,
) -> AgentFileOps | None:
    """Resolve and install the singleton this worker wires itself to.

    Returns ``None`` -- the honest answer -- when the shape is not the agent one
    or when the worker does not know its control plane's address / its own node
    id. ``enabled()`` and ``active()`` are then both false, and the call sites
    keep the pre-C3 behaviour; a deployment that *asks* for the agent shape but
    cannot name its control plane fails closed here instead of silently keeping
    a privileged path (D18.1).
    """
    if not enabled(settings):
        _ACTIVE[0] = None
        return None
    url = (
        control_plane_url
        if control_plane_url is not None
        else os.getenv("E2B_CONTROL_PLANE_URL", "")
    )
    node = node_id if node_id is not None else os.getenv("E2B_NODE_ID", "")
    if not url or not node:
        raise AgentFileOpsError(
            "E2B_PRIV_HELPER_TRANSPORT=agent needs E2B_CONTROL_PLANE_URL and "
            "E2B_NODE_ID: refusing to start without a control plane to ask"
        )
    client = AgentFileOps(
        control_plane_url=str(url),
        node_id=str(node),
        internal_key=_internal_key_for(settings),
        timeout_s=float(getattr(settings, "file_op_timeout_s", DEFAULT_TIMEOUT_S)),
        transport=transport,
    )
    _ACTIVE[0] = client
    return client


def active() -> AgentFileOps | None:
    return _ACTIVE[0]


def shutdown() -> None:
    """Close the active client's keep-alive connection (idempotent).

    Called from the worker's lifespan shutdown next to the other transport
    teardowns; a client left open would hold a socket until the process exits,
    which for a rolling restart is exactly the window that matters.

    ``_ACTIVE`` is "whatever shape is wired" -- an embedder (and this repo's
    tests) may put a stand-in there that is not an :class:`AgentFileOps` -- so
    the teardown asks for ``close`` rather than assuming the whole interface.
    """
    client = _ACTIVE[0]
    close = getattr(client, "close", None)
    if close is not None:
        close()
