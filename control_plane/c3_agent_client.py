"""Finding a node's C3 agent, and instructing it (rulings D9.4 / D9.5).

The control plane is the only component that talks to the agent, and it has to
find it **from a trusted source**: never from a request body (the same rule
``control_plane/node_address.py`` follows for the worker's own address).

* ``k8s`` -- the node id is the worker's StatefulSet pod name. The worker pod's
  ``spec.nodeName`` says which host it landed on; the agent pod *on that host*
  is then looked up **by its label**, and its ``podIP`` is the address. The
  lookup reads the worker pod's UID on the way, and that UID travels with the
  instruction as the extra proof the agent's reverse lookup demands (D9.3).
* ``hostname`` -- the compose shapes address the agent by its configured
  service name (``E2B_C3_AGENT_URL``). There is no pod UID in this lane, so the
  instruction carries none; the worker's pid namespace identity is the proof
  (see ``deploy/c3_agent/lookup.py`` for why that is sound).

**Two identities, and D12 is why they are two.** The agent is a *per-node*
DaemonSet, so the identity the agent checks against itself -- and the one this
client puts in the URL path -- is the **host**: ``spec.nodeName`` in k8s, the
service name in compose. The identity of the *worker that reported the pid*
travels in the instruction body (``worker.node_id`` + pod UID + pid namespace):
with two worker pods on one node (three, in the multinode compose stack) a
worker-pod-name identity would be wrong or ambiguous, and the agent could not
tell which worker a candidate process belonged to.

A lookup that cannot answer -- pod missing, no agent pod on the node, two agent
pods, a k8s lane that somehow had no worker UID -- resolves to ``None``. The
caller fails closed and names the node; nothing here ever picks a candidate "on
a hunch".

The client itself is deliberately small and typed: one deadline per instruction
(a stuck agent must never read as "the sandbox create hangs", D9.5), the agent's
own named refusal forwarded verbatim when it refuses, and a **concurrency
limit** -- the knob the task's dispatch asks to expose now, so slice B can size
it from the real concurrent-create arm.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx

from gateway_common.paths import validate_node_id
from gateway_common.worker_identity import validate_pid_namespace, validate_pod_uid

logger = logging.getLogger(__name__)

#: The agent's own port (``E2B_C3_AGENT_PORT`` in ``deploy/c3_agent/config.py``).
DEFAULT_AGENT_PORT = 49985
#: Face B's port (ruling D22). The agent is one pod with two containers, so
#: "the same node's face B" is the same address with this second port; compose
#: names the face-B service outright instead (``E2B_C3_AGENT_MAINT_URL``).
DEFAULT_AGENT_MAINT_PORT = 49986


def _host_source_ips(host: str) -> tuple[str, ...]:
    """The IPs a host name resolves to, for the source-IP second factor.

    Reuses the node-address resolver's own ``hostname`` lane (the same
    ``getaddrinfo`` call the worker-facing endpoints use) instead of a second
    implementation, and answers ``()`` -- "cannot state one" -- rather than a
    guess when the name does not resolve.
    """
    from control_plane.node_address import HostnameAddressResolver

    endpoint = HostnameAddressResolver().resolve(host)
    return endpoint.source_ips if endpoint is not None else ()


@dataclass(frozen=True)
class AgentTarget:
    """Where the agent is, and what the lookup learned about the worker.

    ``node_identity`` is the agent's **own** identity -- the host it runs on
    (``spec.nodeName`` in k8s; the service name in compose). The instruction is
    addressed by it, and the agent compares it against the node it believes it
    is (D12).

    ``pod_uid`` is the **worker pod's** UID, not the agent pod's: it is the
    k8s lane's proof that a candidate process in the agent's ``/proc`` scan
    belongs to the worker whose report the control plane is forwarding.

    ``url`` is face A (``grant-slot``); ``maint_url`` is face B (``chown`` /
    ``rm`` / ``walk``). They are two processes by necessity -- face A is uid
    65534 (the ``uid_map`` owner rule) and face B is uid 0 (NFS AUTH_SYS
    ``chown``) -- so they are two listeners, and the ruling (D22) is that face
    B gets its own endpoint rather than sharing one. ``maint_url`` is ``None``
    only for a shape that named none (compose without
    ``E2B_C3_AGENT_MAINT_URL``), and a file operation then refuses by name.
    """

    node_identity: str
    url: str
    pod_uid: str | None = None
    maint_url: str | None = None
    #: The address(es) this agent must speak from -- the source-IP second
    #: factor (§11.1 item 9's layer 4). Empty means "this lane cannot state
    #: one", and a caller that needs the check then refuses by name rather
    #: than accepting a claim it cannot verify (C3 Task 6's report endpoint).
    source_ips: tuple[str, ...] = ()


class AgentAddressResolver(Protocol):
    def resolve(self, node_id: str) -> AgentTarget | None:
        """The node's agent, or ``None`` when it cannot be determined."""
        ...

    def resolve_host(self, node_identity: str) -> AgentTarget | None:
        """The agent **on that host**, keyed by the agent's own identity (D12).

        C3 Task 6 needs this direction: the agent reports on its own behalf, and
        the report is addressed by the host it runs on. ``resolve`` cannot
        answer that question when the *worker* pod is gone -- which is exactly
        the case the sweep exists for (a worker that crashed and never came
        back), so the lookup goes to the agent pods themselves.
        """
        ...


class StaticAgentAddressResolver:
    """A fixed node→agent table (tests, embedders, a hand-run lane)."""

    def __init__(self, targets: Mapping[str, AgentTarget]) -> None:
        self._targets = dict(targets)

    def resolve(self, node_id: str) -> AgentTarget | None:
        return self._targets.get(node_id)

    def resolve_host(self, node_identity: str) -> AgentTarget | None:
        target = self._targets.get(node_identity)
        if target is None or target.node_identity != node_identity:
            # A table keyed by something else must not answer for this host:
            # the identity the agent is addressed by is the agent's own.
            return None
        return target


class ComposeAgentAddressResolver:
    """Compose: the agent is the configured service name (``E2B_C3_AGENT_URL``).

    Unconfigured means *no agent*, not "try something else": a shape that has
    not named one must refuse the grant by name rather than dial a guess.

    The agent's self-identity is the **host of that URL** (``c3-agent``), and the
    compose manifests set the agent's ``E2B_C3_AGENT_NODE_ID`` to the same name
    -- the pair is pinned in ``tests/unit/test_c3_agent_manifest.py``. Deriving
    it from the URL is deliberate: there is exactly one name, the one the
    control plane actually dials, so the two halves cannot drift.

    Face B is a *second service* in this lane (compose has no pods, so the two
    faces are two containers with two names): ``E2B_C3_AGENT_MAINT_URL`` names
    it, and the path identity inside every instruction is still the face-A host
    -- the agent's self-check is against the name the control plane addresses
    it by, and both faces carry that same ``E2B_C3_AGENT_NODE_ID``.
    """

    def __init__(self, url: str | None, maint_url: str | None = None) -> None:
        self._url = (url or "").strip().rstrip("/")
        self._maint_url = (maint_url or "").strip().rstrip("/")

    def resolve(self, node_id: str) -> AgentTarget | None:
        if not self._url:
            return None
        identity = urlsplit(self._url).hostname or ""
        if not validate_node_id(identity):
            return None
        # A face-B URL the deployment named but that carries no usable host is
        # not an address: drop it here (the file operation then fails closed
        # naming the variable) rather than dialling a guess later.
        maint_host = urlsplit(self._maint_url).hostname if self._maint_url else ""
        maint_url = (
            self._maint_url
            if self._maint_url and maint_host and validate_node_id(maint_host)
            else None
        )
        return AgentTarget(
            node_identity=identity,
            url=self._url,
            pod_uid=None,
            maint_url=maint_url,
        )

    def resolve_host(self, node_identity: str) -> AgentTarget | None:
        """The configured agent, when the claim names *its* host.

        Compose has no per-node agents: the resolver answers for one name, so a
        claim for any other name is ``None`` (fail closed) rather than "probably
        that one". The expected source IPs come from the same name the control
        plane dials (``socket.getaddrinfo`` at request time, so a restarted
        container's new IP is followed -- §11.1 item 9's premise (b)).
        """
        target = self.resolve(node_identity)
        if target is None or target.node_identity != node_identity:
            return None
        return replace(target, source_ips=_host_source_ips(node_identity))


class K8sAgentAddressResolver:
    """k8s: worker pod → ``spec.nodeName`` → the agent pod on that host.

    The two lookups are the whole point: the agent is a DaemonSet, so "the agent
    for this node" is only well-defined through the host the worker actually
    landed on. Two agent pods on one host (a stale DaemonSet revision) are
    *ambiguous*, which is a refusal -- not an arbitrary pick.
    """

    def __init__(
        self,
        *,
        namespace: str,
        label_selector: str,
        port: int = DEFAULT_AGENT_PORT,
        maint_port: int = DEFAULT_AGENT_MAINT_PORT,
        scheme: str = "http",
        client: httpx.Client | None = None,
    ) -> None:
        self._namespace = namespace
        self._label_selector = label_selector
        self._port = int(port)
        self._maint_port = int(maint_port)
        self._scheme = scheme
        self._client = client or self._in_cluster_client()

    @staticmethod
    def _in_cluster_client() -> httpx.Client:
        """The same ServiceAccount client ``node_address``'s k8s mode uses."""
        from control_plane.node_address import K8sPodAddressResolver

        return K8sPodAddressResolver._in_cluster_client()

    def resolve(self, node_id: str) -> AgentTarget | None:
        if not validate_node_id(node_id):
            return None
        worker = self._get(f"/api/v1/namespaces/{self._namespace}/pods/{node_id}")
        if worker is None:
            return None
        pod_uid = _nested_str(worker, "metadata", "uid")
        node_name = _nested_str(worker, "spec", "nodeName")
        if not node_name or not pod_uid or not validate_pod_uid(pod_uid):
            # The k8s lane's proof is unavailable: refuse rather than instruct
            # the agent with a weaker one (D9.3's fail-closed rule).
            logger.warning(
                "c3 agent lookup: worker pod %s carries no usable uid/nodeName; "
                "refusing (fail closed)",
                node_id,
            )
            return None
        listing = self._get(
            f"/api/v1/namespaces/{self._namespace}/pods",
            params={
                "labelSelector": self._label_selector,
                "fieldSelector": f"spec.nodeName={node_name}",
            },
        )
        if listing is None:
            return None
        pods = listing.get("items") if isinstance(listing, dict) else None
        if not isinstance(pods, list):
            return None
        candidates = [
            pod_ip
            for pod in pods
            if isinstance(pod, dict) and (pod_ip := _nested_str(pod, "status", "podIP"))
        ]
        if len(candidates) != 1:
            logger.warning(
                "c3 agent lookup: node %s (host %s) has %d agent pods with an "
                "address; refusing (fail closed)",
                node_id,
                node_name,
                len(candidates),
            )
            return None
        return AgentTarget(
            # D12: the agent's identity is the *host*, not the worker pod that
            # happened to report. This is what the instruction is addressed by,
            # and it is the agent's own `E2B_C3_AGENT_NODE_ID` (spec.nodeName).
            node_identity=node_name,
            url=f"{self._scheme}://{candidates[0]}:{self._port}",
            pod_uid=pod_uid,
            # D22: face B is the *same* pod on the *same* node (the two
            # containers share the pod netns), so it is the same address on its
            # own port -- never a second lookup, and never anything the worker
            # said.
            maint_url=f"{self._scheme}://{candidates[0]}:{self._maint_port}",
        )

    def resolve_host(self, node_identity: str) -> AgentTarget | None:
        """The agent pod **on that node**, addressed by the host itself (D12).

        Unlike :meth:`resolve`, this path never reads a worker pod: the worker
        may be gone -- that is exactly the case the self-heal sweep exists for
        -- while the DaemonSet's face on the node is not. The lookup is the
        agent pods' own label plus ``spec.nodeName``, and two agent pods on one
        node (a stale DaemonSet revision) is a refusal, never an arbitrary pick.
        """
        if not validate_node_id(node_identity):
            return None
        listing = self._get(
            f"/api/v1/namespaces/{self._namespace}/pods",
            params={
                "labelSelector": self._label_selector,
                "fieldSelector": f"spec.nodeName={node_identity}",
            },
        )
        if listing is None:
            return None
        pods = listing.get("items") if isinstance(listing, dict) else None
        if not isinstance(pods, list):
            return None
        candidates = [
            pod_ip
            for pod in pods
            if isinstance(pod, dict) and (pod_ip := _nested_str(pod, "status", "podIP"))
        ]
        if len(candidates) != 1:
            logger.warning(
                "c3 agent lookup: host %s has %d agent pods with an address; "
                "refusing (fail closed)",
                node_identity,
                len(candidates),
            )
            return None
        return AgentTarget(
            node_identity=node_identity,
            url=f"{self._scheme}://{candidates[0]}:{self._port}",
            # No worker pod is involved, so there is no worker pod UID to carry:
            # ``grant-slot``'s reverse lookup is that proof's only consumer.
            pod_uid=None,
            maint_url=f"{self._scheme}://{candidates[0]}:{self._maint_port}",
            source_ips=(candidates[0],),
        )

    def _get(self, path: str, params: dict[str, str] | None = None):
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError:
            # Unreachable API is "cannot determine", never "allow".
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def _nested_str(payload: dict, *path: str) -> str:
    """``payload[a][b]`` when it is a non-empty string, else ``""``."""
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _worker_body(
    *,
    node_id: str,
    worker_uid: int,
    worker_gid: int,
    worker_pid_namespace: str | None,
) -> dict[str, Any]:
    """One instruction's ``worker`` block (face B's "who does this act as").

    ``uid``/``gid`` are the control plane's own record for the node -- the value
    it verified (k8s) or the claim the agent confirms (compose, D21 option 2).
    ``pid_namespace`` is the anchor that makes the compose shape's confirmation
    a *lookup*: it is present only when the shape defers to the kernel, and the
    worker's own name travels with it so the agent's refusal can name who it was
    asked about.

    The anchor is shape-checked here as well as where it was recorded ("two
    layers, neither replaces the other" -- the same rule the path discipline
    follows): a value that cannot be a namespace identity must never reach the
    agent's ``/proc`` walk, and this is the last hop that can say so.
    """
    body: dict[str, Any] = {"uid": int(worker_uid), "gid": int(worker_gid)}
    if worker_pid_namespace is None:
        return body
    if not validate_pid_namespace(worker_pid_namespace):
        raise AgentClientError(
            f"the pid namespace anchor carried for node {node_id} is not a pid "
            "namespace identity: refusing to instruct the agent",
            status_code=503,
        )
    body["node_id"] = node_id
    body["pid_namespace"] = worker_pid_namespace
    return body


def build_agent_address_resolver(settings) -> AgentAddressResolver:
    """The resolver for this deployment's shape.

    The mode is the *same* switch the node-address resolver uses
    (``E2B_NODE_ADDRESS_MODE``): a deployment that told the control plane how to
    find its workers has also told it which topology the agents live in, and a
    second knob could only drift from the first.
    """
    mode = (getattr(settings, "node_address_mode", "auto") or "auto").strip().lower()
    if mode == "auto":
        # ``auto`` follows the node resolver's own in-cluster detection; the
        # control plane passes the resolved mode down explicitly in the shipped
        # manifests, so this branch only matters for an embedder.
        from control_plane.node_address import _service_account_present

        mode = "k8s" if _service_account_present() else "hostname"
    if mode == "k8s":
        return K8sAgentAddressResolver(
            namespace=getattr(settings, "c3_agent_namespace", "sandlock"),
            label_selector=getattr(settings, "c3_agent_label", "app=c3-agent"),
            port=int(getattr(settings, "c3_agent_port", DEFAULT_AGENT_PORT)),
            maint_port=int(
                getattr(settings, "c3_agent_maint_port", DEFAULT_AGENT_MAINT_PORT)
            ),
        )
    if mode == "hostname":
        return ComposeAgentAddressResolver(
            getattr(settings, "c3_agent_url", None),
            getattr(settings, "c3_agent_maint_url", None),
        )
    raise ValueError(
        f"E2B_NODE_ADDRESS_MODE must be 'k8s', 'hostname' or 'auto' (got {mode!r})"
    )


class AgentClientError(RuntimeError):
    """A named, fail-closed refusal from the CP→agent hop.

    ``status_code`` is the control plane's own answer to the worker: every hop
    of this chain is typed, so a failure is never reported as a bare 500 (and a
    stuck agent is a 504, not a hung create).
    """

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class C3AgentClient:
    """One instruction per privileged step, fail-closed, with a concurrency bound.

    Face A (``grant-slot``) is one instruction per slot start; face B
    (``chown`` / ``rm`` / ``walk``) is one per file operation. They share the
    addressing, the token, the timeout and the concurrency knob -- and, above
    all, the refusal shape, so a hop that cannot be made reads the same way
    whichever verb asked for it (a stuck agent is a 504, never a hung create).
    """

    def __init__(
        self,
        *,
        resolver: AgentAddressResolver,
        token: str,
        timeout_s: float,
        file_op_timeout_s: float | None = None,
        max_concurrency: int = 0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver = resolver
        self._token = token or ""
        self._timeout_s = float(timeout_s)
        # Face B's deadline is its own number: a tree walk or a teardown is
        # bounded by the tree (``maint.c``'s own budgets are 300 s for chown/rm
        # and larger for a walk), while a slot grant is a single ``write(2)``.
        # Sharing one deadline would either hang a create for minutes or cut
        # off a legitimate teardown at five seconds.
        self._file_op_timeout_s = float(
            timeout_s if file_op_timeout_s is None else file_op_timeout_s
        )
        # ``0`` means "unbounded" (today's default); slice B sizes the real
        # number from the concurrent-create arm and the acceptance matrix's
        # negative arm (pool = 1 must reproduce queueing).
        self._max_concurrency = int(max_concurrency or 0)
        self._semaphore = (
            asyncio.Semaphore(self._max_concurrency)
            if self._max_concurrency > 0
            else None
        )
        self._transport = transport

    async def grant_slot(
        self,
        *,
        node_id: str,
        sandbox_id: str,
        container_pid: int,
        uid: int,
        worker_pid_namespace: str,
    ) -> dict[str, Any]:
        """Instruct the node's agent to hand ``uid`` to the slot's child.

        ``node_id`` is the **worker's** identity (StatefulSet pod name). The
        agent is addressed by the identity the resolver found for it -- the host
        it runs on (D12) -- and the worker's identity rides in the body, where
        the agent's reverse lookup needs it.
        """
        if not validate_pid_namespace(worker_pid_namespace):
            raise AgentClientError(
                f"node {node_id} has no usable pid namespace identity "
                f"({worker_pid_namespace!r}): refusing to instruct the agent",
                status_code=503,
            )
        target = self._target(node_id)
        body = {
            "sandbox_id": sandbox_id,
            "pid": int(container_pid),
            "uid": int(uid),
            "worker": {
                # The *worker's* identity (its node id == its pod name) travels
                # here; the URL carries the **agent's** identity (its host).
                "node_id": node_id,
                "pid_namespace": worker_pid_namespace,
                "pod_uid": target.pod_uid,
            },
        }
        return await self._instruct(
            target,
            node_id,
            "grant-slot",
            body,
            url=target.url,
            refusal="refused the grant",
            timeout_tail="the slot identity grant is fail-closed",
        )

    async def chown(
        self,
        *,
        node_id: str,
        sandbox_id: str,
        path: str,
        worker_uid: int,
        worker_gid: int,
        worker_pid_namespace: str | None = None,
        uid: int | None = None,
        gid: int | None = None,
        recursive: bool = False,
        worker_owned: bool = False,
    ) -> dict[str, Any]:
        """Instruct the agent to run ``e2b-maint chown`` on a CP-derived path.

        ``uid`` is the pooled uid the control plane's records named and
        ``worker_owned`` selects ``--worker`` (the owner stays the worker; only
        the group moves). The *worker's* own identity is carried beside them:
        it is what the agent must write into ``E2B_BROKER_WORKER_UID/GID`` so
        that ``--worker`` and the group gate keep the meaning they have behind
        the worker's broker (see ``deploy/c3_agent/fileops.py``).

        ``worker_pid_namespace`` is D21 option 2's anchor: present when this
        deployment's shape could not verify the identity itself (compose), so
        the agent reads it from the kernel and refuses a claim it does not
        confirm. A shape that verified it (k8s) passes ``None`` and the
        instruction is byte-for-byte what it always was.
        """
        body = {
            "sandbox_id": sandbox_id,
            "path": path,
            "recursive": bool(recursive),
            "worker_owned": bool(worker_owned),
            "worker": _worker_body(
                node_id=node_id,
                worker_uid=worker_uid,
                worker_gid=worker_gid,
                worker_pid_namespace=worker_pid_namespace,
            ),
        }
        if uid is not None:
            body["uid"] = int(uid)
        if gid is not None:
            body["gid"] = int(gid)
        return await self._file_op(node_id, "chown", body)

    async def rm(
        self,
        *,
        node_id: str,
        sandbox_id: str,
        path: str,
        worker_uid: int | None = None,
        worker_gid: int | None = None,
        worker_pid_namespace: str | None = None,
        target: AgentTarget | None = None,
    ) -> dict[str, Any]:
        """Instruct the agent to run ``e2b-maint rm`` on a CP-derived path.

        A worker-initiated removal carries the worker's identity (it is the
        node record's, and the agent writes it into ``--worker``'s environment).
        C3 Task 6's **self-heal** removal does not: it deletes a tree no record
        claims, as nobody, through the ``target`` the caller already
        authenticated the report against (``resolve_agent``).
        """
        body: dict[str, Any] = {"sandbox_id": sandbox_id, "path": path}
        if worker_uid is not None and worker_gid is not None:
            body["worker"] = _worker_body(
                node_id=node_id,
                worker_uid=worker_uid,
                worker_gid=worker_gid,
                worker_pid_namespace=worker_pid_namespace,
            )
        return await self._file_op(node_id, "rm", body, target=target)

    async def walk(
        self,
        *,
        node_id: str,
        sandbox_id: str,
        path: str,
        worker_uid: int,
        worker_gid: int,
        worker_pid_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Instruct the agent to run ``e2b-maint walk`` on a CP-derived path."""
        body = {
            "sandbox_id": sandbox_id,
            "path": path,
            "worker": _worker_body(
                node_id=node_id,
                worker_uid=worker_uid,
                worker_gid=worker_gid,
                worker_pid_namespace=worker_pid_namespace,
            ),
        }
        return await self._file_op(node_id, "walk", body)

    async def _file_op(
        self,
        node_id: str,
        verb: str,
        body: dict[str, Any],
        *,
        target: AgentTarget | None = None,
    ) -> dict[str, Any]:
        target = self._target(node_id) if target is None else self._prepare_target(
            target, node_id
        )
        # D22: the file verbs go to face B's own endpoint. A shape that named
        # none (compose without `E2B_C3_AGENT_MAINT_URL`) refuses by name here
        # rather than dialling face A -- where every chown on NFS would come
        # back EPERM, i.e. a failure that looks like a permission bug.
        if not target.maint_url:
            raise AgentClientError(
                f"cannot determine the file-operation agent address for node "
                f"{node_id} (E2B_C3_AGENT_MAINT_URL / E2B_C3_AGENT_MAINT_PORT): "
                "refusing to instruct an agent the control plane cannot locate",
                status_code=503,
            )
        return await self._instruct(
            target,
            node_id,
            verb,
            body,
            url=target.maint_url,
            refusal=f"refused the {verb}",
            timeout_tail=f"the {verb} instruction is fail-closed",
        )

    def _target(self, node_id: str) -> AgentTarget:
        """The fail-closed preconditions every instruction shares."""
        target = self._resolver.resolve(node_id)
        if target is None:
            raise AgentClientError(
                f"cannot determine the agent address for node {node_id}: "
                "refusing to instruct an agent the control plane cannot locate",
                status_code=503,
            )
        return self._prepare_target(target, node_id)

    def resolve_agent(self, node_identity: str) -> AgentTarget:
        """The agent **on that host**, by the host's own name (C3 Task 6).

        The lookup is keyed by the agent's identity rather than a worker's, so
        a node whose worker is gone can still be addressed (the sweep's whole
        reason to exist). Every way it can fail is a named 503 -- there is no
        "cannot check, so accept" branch.
        """
        target = self._resolver.resolve_host(node_identity)
        if target is None:
            raise AgentClientError(
                f"cannot determine the address of the agent for node "
                f"{node_identity}: refusing (fail closed)",
                status_code=503,
            )
        if target.node_identity != node_identity:
            raise AgentClientError(
                f"the agent lookup for node {node_identity} answered for "
                f"{target.node_identity}: refusing",
                status_code=503,
            )
        return self._prepare_target(target, node_identity)

    def _prepare_target(self, target: AgentTarget, node_id: str) -> AgentTarget:
        """The preconditions that hold for a resolved target, whoever resolved it."""
        if not validate_node_id(target.node_identity):
            raise AgentClientError(
                f"the agent address for node {node_id} carries no usable agent "
                f"identity ({target.node_identity!r}): refusing",
                status_code=503,
            )
        if target.pod_uid is not None and not validate_pod_uid(target.pod_uid):
            raise AgentClientError(
                f"the pod uid carried for node {node_id} is not a pod uid: "
                "refusing",
                status_code=503,
            )
        if not self._token:
            raise AgentClientError(
                "E2B_C3_AGENT_TOKEN is not configured: refusing to instruct an agent",
                status_code=503,
            )
        return target

    def _deadline_for(self, op: str) -> float:
        """One deadline per instruction kind (see ``__init__``)."""
        return self._timeout_s if op == "grant-slot" else self._file_op_timeout_s

    async def _instruct(
        self,
        target: AgentTarget,
        node_id: str,
        op: str,
        body: dict[str, Any],
        *,
        url: str,
        refusal: str,
        timeout_tail: str,
    ) -> dict[str, Any]:
        if self._semaphore is not None:
            async with self._semaphore:
                return await self._post(
                    target,
                    node_id,
                    op,
                    body,
                    url=url,
                    refusal=refusal,
                    timeout_tail=timeout_tail,
                )
        return await self._post(
            target,
            node_id,
            op,
            body,
            url=url,
            refusal=refusal,
            timeout_tail=timeout_tail,
        )

    async def _post(
        self,
        target: AgentTarget,
        node_id: str,
        op: str,
        body: dict[str, Any],
        *,
        url: str,
        refusal: str,
        timeout_tail: str,
    ) -> dict[str, Any]:
        # The path identity is the agent's own (the host, D12) on *both* faces:
        # face B is a different address (D22) for the same node, and its
        # `E2B_C3_AGENT_NODE_ID` is the same name face A carries.
        url = f"{url}/internal/nodes/{target.node_identity}/agent/{op}"
        deadline = self._deadline_for(op)
        try:
            async with httpx.AsyncClient(
                timeout=deadline, transport=self._transport
            ) as client:
                resp = await client.post(
                    url, json=body, headers={"X-Internal-Key": self._token}
                )
        except httpx.TimeoutException as exc:
            raise AgentClientError(
                f"the agent for node {node_id} did not answer within "
                f"{self._deadline_for(op)}s: refusing ({timeout_tail})",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise AgentClientError(
                f"the agent for node {node_id} is unreachable: {detail}",
                status_code=502,
            ) from exc
        if resp.status_code >= 300:
            error: Any = None
            with suppress(ValueError):
                error = resp.json().get("error")
            detail = error if isinstance(error, str) and error else resp.text
            raise AgentClientError(
                f"the agent for node {node_id} {refusal}: {detail}",
                status_code=502,
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AgentClientError(
                f"the agent for node {node_id} answered with a non-JSON body",
                status_code=502,
            ) from exc
        if not isinstance(payload, dict):
            # A 2xx that is not an instruction answer is not a grant: the caller
            # reads fields out of it, and "wrapped something else" would let a
            # broken or substituted answer pass for one the agent never gave.
            raise AgentClientError(
                f"the agent for node {node_id} answered with a "
                f"{type(payload).__name__}, not an instruction answer",
                status_code=502,
            )
        return payload
