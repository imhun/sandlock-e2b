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
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import httpx

from gateway_common.paths import validate_node_id
from gateway_common.worker_identity import validate_pid_namespace, validate_pod_uid

logger = logging.getLogger(__name__)

#: The agent's own port (``E2B_C3_AGENT_PORT`` in ``deploy/c3_agent/config.py``).
DEFAULT_AGENT_PORT = 49985


@dataclass(frozen=True)
class AgentTarget:
    """Where the agent is, and what the lookup learned about the worker.

    ``pod_uid`` is the **worker pod's** UID, not the agent pod's: it is the
    k8s lane's proof that a candidate process in the agent's ``/proc`` scan
    belongs to the worker whose report the control plane is forwarding.
    """

    url: str
    pod_uid: str | None = None


class AgentAddressResolver(Protocol):
    def resolve(self, node_id: str) -> AgentTarget | None:
        """The node's agent, or ``None`` when it cannot be determined."""
        ...


class StaticAgentAddressResolver:
    """A fixed node→agent table (tests, embedders, a hand-run lane)."""

    def __init__(self, targets: Mapping[str, AgentTarget]) -> None:
        self._targets = dict(targets)

    def resolve(self, node_id: str) -> AgentTarget | None:
        return self._targets.get(node_id)


class ComposeAgentAddressResolver:
    """Compose: the agent is the configured service name (``E2B_C3_AGENT_URL``).

    Unconfigured means *no agent*, not "try something else": a shape that has
    not named one must refuse the grant by name rather than dial a guess.
    """

    def __init__(self, url: str | None) -> None:
        self._url = (url or "").strip().rstrip("/")

    def resolve(self, node_id: str) -> AgentTarget | None:
        if not self._url:
            return None
        return AgentTarget(url=self._url, pod_uid=None)


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
        scheme: str = "http",
        client: httpx.Client | None = None,
    ) -> None:
        self._namespace = namespace
        self._label_selector = label_selector
        self._port = int(port)
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
            url=f"{self._scheme}://{candidates[0]}:{self._port}", pod_uid=pod_uid
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
        )
    if mode == "hostname":
        return ComposeAgentAddressResolver(getattr(settings, "c3_agent_url", None))
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
    """One instruction per slot start, fail-closed, with a concurrency bound."""

    def __init__(
        self,
        *,
        resolver: AgentAddressResolver,
        token: str,
        timeout_s: float,
        max_concurrency: int = 0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver = resolver
        self._token = token or ""
        self._timeout_s = float(timeout_s)
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
        """Instruct the node's agent to hand ``uid`` to the slot's child."""
        if not validate_pid_namespace(worker_pid_namespace):
            raise AgentClientError(
                f"node {node_id} has no usable pid namespace identity "
                f"({worker_pid_namespace!r}): refusing to instruct the agent",
                status_code=503,
            )
        target = self._resolver.resolve(node_id)
        if target is None:
            raise AgentClientError(
                f"cannot determine the agent address for node {node_id}: "
                "refusing to instruct an agent the control plane cannot locate",
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
        body = {
            "sandbox_id": sandbox_id,
            "pid": int(container_pid),
            "uid": int(uid),
            "worker": {
                "node_id": node_id,
                "pid_namespace": worker_pid_namespace,
                "pod_uid": target.pod_uid,
            },
        }
        if self._semaphore is not None:
            async with self._semaphore:
                return await self._post(target, node_id, body)
        return await self._post(target, node_id, body)

    async def _post(
        self, target: AgentTarget, node_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{target.url}/internal/nodes/{node_id}/agent/grant-slot"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                resp = await client.post(
                    url, json=body, headers={"X-Internal-Key": self._token}
                )
        except httpx.TimeoutException as exc:
            raise AgentClientError(
                f"the agent for node {node_id} did not answer within "
                f"{self._timeout_s}s: refusing (the slot identity grant is "
                "fail-closed)",
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
                f"the agent for node {node_id} refused the grant: {detail}",
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
