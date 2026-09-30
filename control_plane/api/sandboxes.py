"""Sandbox lifecycle endpoints mirroring the official Sandbox OpenAPI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, Query, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import (
    _require_owned,
    _require_related,
    require_api_key,
    tenant_of,
    tenant_scope,
)
from control_plane.config import local_node_quota_via_agent
from control_plane.queue import QueueOutcome
from control_plane.registry.manager import (
    PRIORITY_DEFAULT,
    PRIORITY_MAX,
    PRIORITY_MIN,
    ResourceUnavailableError,
    SandboxRecord,
    SandboxStateConflictError,
    SandboxRegistry,
    UnknownSandboxError,
    workspace_disk_refusal,
)
from control_plane.registry.secrets import SecretTenantMismatchError
from control_plane.registry.snapshots import UnknownSnapshotError
from control_plane.registry.templates import UnknownTemplateBuildError
from envd_service.executors.factory import (
    sandlock_failure_detail,
    sandlock_not_installed,
    sandlock_unusable_error,
)
from gateway_common.network import (
    NetworkUpdateConflictError,
    NetworkConfigError,
    normalize_network_config,
    normalize_network_update,
)
from gateway_common.upload import (
    UploadTooLargeError,
    json_size,
    read_json_body,
)
from gateway_common import GATEWAY_ROUTE_INVALIDATE_CHANNEL
from gateway_common.timeutil import to_iso_z
from gateway_common.paths import (
    is_reserved_platform_namespace,
    sandbox_command_log_path,
    sandbox_runtime_dir,
    validate_sandbox_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Unsupported fields that must produce an explicit 400 (no fake success).
UNSUPPORTED_FIELDS = ("image", "lifecycle")
UNSUPPORTED_ENDPOINTS = ()

# Idempotent-create (X-Sandbox-Id) slow-path tuning.
_PENDING_TTL_S = 300
_PENDING_WAIT_S = 10.0


class _CapacityExhausted(Exception):
    """Internal control-flow marker: admission found no room (E9.3).

    Raised by one create attempt at the two capacity failure points (no node,
    or global/tenant quota refused) after the attempt rolled back any partial
    state it held; the outer ``create_sandbox`` dispatcher turns it into an
    eviction round + retry or the original 503.
    """


def _default_dims(settings) -> tuple[int, int, int, int]:
    return (
        settings.default_memory_mb,
        settings.default_cpu_percent,
        settings.default_disk_mb,
        settings.default_max_processes,
    )


def _executor_needs_images(mode: str) -> bool:
    """Whether the local executor resolves image rootfs at all."""
    if mode == "local":
        return False
    if mode == "sandlock":
        return True
    # auto: same judgment as the worker's probe (envd_service/agent.py): a
    # *missing* package falls back to the local executor, an unusable one does
    # not (the factory fails closed on it), so it must not read as "no images
    # needed" here either (B1 fix round 2, shared wording).
    try:
        import sandlock  # noqa: F401

        return sandlock.landlock_abi_version() >= 6
    except ModuleNotFoundError as exc:
        # Only a missing *top-level* package is the fallback case: a
        # half-upgraded tree ("No module named 'sandlock.exceptions'") is
        # installed-but-broken and must fail closed (B1 fix round 3).
        if sandlock_not_installed(exc):
            return False
        raise sandlock_unusable_error(
            "auto", sandlock_failure_detail(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001 - classified right here
        raise sandlock_unusable_error("auto", sandlock_failure_detail(exc)) from exc


def _check_metadata_envvars_size(settings, metadata: dict, env_vars: dict) -> None:
    """E5.3: keep sandbox.json bounded (metadata/envVars serialized bytes)."""
    if settings.max_metadata_bytes > 0:
        meta_bytes = json_size(metadata)
        if meta_bytes > settings.max_metadata_bytes:
            raise OfficialError(
                413,
                f"metadata exceeds {settings.max_metadata_bytes}-byte limit",
            )
    if settings.max_envvars_bytes > 0:
        env_bytes = json_size(env_vars)
        if env_bytes > settings.max_envvars_bytes:
            raise OfficialError(
                413,
                f"envVars exceeds {settings.max_envvars_bytes}-byte limit",
            )


def _release_node_quota(request, node, dims: tuple[int, int, int, int]) -> None:
    memory_mb, cpu, disk_mb, processes = dims
    request.app.state.nodes.release_quota(
        node.node_id,
        memory_mb=memory_mb,
        cpu_percent=cpu,
        disk_mb=disk_mb,
        processes=processes,
    )


def _record_quota_dims(record) -> tuple[int, int, int, int]:
    return (
        record.memory_mb,
        record.cpu_count * 100,
        record.disk_size_mb,
        record.max_processes,
    )


def _node_refusal_message(request, dims) -> str:
    """Explain a node-level refusal in the fleet's own terms.

    A full workspace used to be indistinguishable from a full memory pool
    here, exactly as it was in the fleet ledger. This gate is the one that
    fires *first* on single-node shapes (and in the test harness, where the
    in-process node carries the fleet's own budget), so it has to speak the
    same language as ``SandboxRegistry._quota_denied_message``.
    """
    refused = request.app.state.nodes.refusal(
        memory_mb=dims[0],
        cpu_percent=dims[1],
        disk_mb=dims[2],
        processes=dims[3],
    )
    if refused is None or refused["dimension"] != "disk":
        return "No resources available"
    return workspace_disk_refusal(
        int(refused["disk_reserved_mb"]), int(refused["disk_limit_mb"])
    )


def _park_capacity(request, record) -> None:
    """Give back the node reservation of a sandbox that just paused (E9.2).

    The global/tenant ledger is handled by ``SandboxRegistry.pause``; the node
    pool lives in the node registry, so it is released here. A node the
    registry no longer knows about simply has nothing to release (the same
    tolerance the delete path has).
    """
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        return
    _release_node_quota(request, node, _record_quota_dims(record))


def _resume_with_capacity(request, registry, record, *, timeout: int | None = None):
    """Re-book capacity for a paused sandbox, then resume it (E9.2).

    Node admission happens first (the workspace pins the sandbox to its node,
    so a different node is not an option), then the global/tenant ledger. Any
    reservation taken before a later step fails is rolled back: a refused
    resume must never consume capacity, and a successful one must never run
    unaccounted.

    Raises ``OfficialError`` 503 (no room; the sandbox stays paused) or 409
    (already running).
    """
    nodes = request.app.state.nodes
    node = nodes.get(record.node_id or "local")
    dims = _record_quota_dims(record)
    reserved_on: object | None = None
    if node is not None:
        reserved_on = nodes.reserve_node(
            node.node_id,
            memory_mb=dims[0],
            cpu_percent=dims[1],
            disk_mb=dims[2],
            processes=dims[3],
        )
        if reserved_on is None:
            # Same message as the global pool: callers (and the E9.3/E9.4
            # retry paths) can treat a full node and a full fleet alike -- the
            # workspace budget excepted, which names itself on both gates.
            # The sandbox is pinned to this node, so the answer is about this
            # node's slice, not about what the rest of the fleet could take.
            blocked = node.blocking_dimension(*dims)
            if blocked != "disk":
                raise OfficialError(503, "No resources available")
            raise OfficialError(
                503,
                workspace_disk_refusal(node.reserved_disk_mb, node.total_disk_mb),
            )
    try:
        resumed = registry.resume(record, timeout)
    except ResourceUnavailableError as e:
        if reserved_on is not None:
            _release_node_quota(request, reserved_on, dims)
        raise OfficialError(503, str(e)) from e
    except SandboxStateConflictError as e:
        if reserved_on is not None:
            _release_node_quota(request, reserved_on, dims)
        raise OfficialError(409, str(e)) from e
    return resumed


async def _push_pause_state(
    request: Request, record, *, paused: bool, reason: str | None = None
) -> bool:
    """Push a pause/resume decision to the hosting worker agent (G1a).

    ``local://`` nodes need no HTTP push: control plane and envd service
    share one runtime registry whose state callback freezes/thaws the
    in-process context. For a remote node the status mapping is:

    * 204 -> delivered;
    * 404 -> no live runtime on the worker (nothing to freeze/thaw); the
      caller keeps its own state bookkeeping and returns success;
    * any other explicit 4xx/5xx -> raise ``OfficialError`` 502 so the
      caller rolls back its local state change;
    * transport error/timeout -> log WARNING and return (documented
      best-effort caveat, same as the network push): the sandbox is
      unreachable, so its runtime cannot be acting on commands anyway.

    ``record.node_id`` missing from the node registry is also best-effort
    (WARNING): there is no address to push to. Returns ``True`` when the
    hosting node is remote (delivery completed, 404-treated-as-success, or
    transport-loss best-effort) and ``False`` when the caller must apply the
    shared-registry state callback instead (``local://`` node, or a missing
    node that was WARNINGed).
    """
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        logger.warning(
            "node %s not found; %s for sandbox %s not pushed",
            record.node_id,
            "pause" if paused else "resume",
            record.sandbox_id,
        )
        return False
    if node.address == "local://":
        return False
    verb = "pause" if paused else "resume"
    import httpx

    logger.info(
        "pushing %s for sandbox %s to node %s",
        verb,
        record.sandbox_id,
        node.node_id,
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{node.address}/agent/sandboxes/{record.sandbox_id}/{verb}",
                headers={
                    "X-Internal-Key": request.app.state.settings.internal_api_key
                },
                # N28/D: only a platform-initiated pause has a reason, and the
                # worker needs it to answer "why can I not write?" with more
                # than "you are paused". Omitted entirely otherwise, so an
                # ordinary pause keeps its old body.
                json={"reason": reason} if reason else None,
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "failed to push %s for sandbox %s to node %s: %s",
            verb,
            record.sandbox_id,
            node.node_id,
            exc,
        )
        return True
    if resp.status_code == 204:
        return True
    if resp.status_code == 404:
        logger.info(
            "node %s has no live runtime for sandbox %s; %s delivery skipped",
            node.node_id,
            record.sandbox_id,
            verb,
        )
        return True
    logger.warning(
        "node %s rejected %s for sandbox %s: %s",
        node.node_id,
        verb,
        record.sandbox_id,
        resp.text,
    )
    raise OfficialError(
        502,
        f"Node {node.node_id} failed to {verb} sandbox "
        f"{record.sandbox_id}: {resp.text}",
    )


def _rollback_pause(request: Request, registry, sandbox_id: str) -> str:
    """Undo a pause whose worker push failed with an explicit error (G1a).

    The push-await window can overlap a concurrent delete/resume: by the
    time an explicit worker error lands, the record may be gone or already
    flipped. Re-fetch the record first and roll back only when it still
    exists AND is still paused (the state this request set); rolling back a
    stale object would ``registry.save()`` a resurrected sandbox and leak
    its reservation. Returns ``"rolled_back"`` (record running again),
    ``"skipped"`` (record deleted or no longer paused; WARNING logged with
    the observed state), or ``"failed"`` (still paused but admission has no
    room: the record stays paused and the caller still surfaces the 502).
    """
    try:
        record = registry.get(sandbox_id)
    except UnknownSandboxError:
        logger.warning(
            "pause rollback skipped for sandbox %s: deleted while pause "
            "push was in flight",
            sandbox_id,
        )
        return "skipped"
    if record.state != "paused":
        logger.warning(
            "pause rollback skipped for sandbox %s: state is %s, not paused",
            sandbox_id,
            record.state,
        )
        return "skipped"
    try:
        _resume_with_capacity(request, registry, record)
    except OfficialError as rollback_error:
        logger.error(
            "pause rollback failed for sandbox %s (%s); record stays paused",
            sandbox_id,
            rollback_error.message,
        )
        return "failed"
    request.app.state.runtime_registry.set_state(sandbox_id, "running")
    return "rolled_back"


def _rollback_resume(request: Request, registry, sandbox_id: str) -> str:
    """Undo a resume whose worker push failed with an explicit error (G1a).

    The push-await window can overlap a concurrent delete/pause: re-fetch the
    record first and roll back only when it still exists AND is still running
    (the state this request set). Returns ``"rolled_back"`` (record paused
    again), ``"skipped"`` (record deleted or no longer running; WARNING
    logged with the observed state), or ``"failed"`` (defensive; the record
    could not be parked).
    """
    try:
        record = registry.get(sandbox_id)
    except UnknownSandboxError:
        logger.warning(
            "resume rollback skipped for sandbox %s: deleted while resume "
            "push was in flight",
            sandbox_id,
        )
        return "skipped"
    if record.state != "running":
        logger.warning(
            "resume rollback skipped for sandbox %s: state is %s, "
            "not running",
            sandbox_id,
            record.state,
        )
        return "skipped"
    try:
        registry.pause(record)
    except SandboxStateConflictError:  # pragma: no cover - defensive
        logger.warning(
            "resume rollback failed for sandbox %s: state is %s",
            sandbox_id,
            record.state,
        )
        return "failed"
    _park_capacity(request, record)
    request.app.state.runtime_registry.set_state(sandbox_id, "paused")
    return "rolled_back"


async def _image_warm(request, node, base_image, settings) -> bool:
    """Peek whether ``base_image`` is already extracted on ``node``."""
    if not base_image:
        return True
    if node.address == "local://":
        if not _executor_needs_images(settings.executor):
            return True
        from envd_service.runtime.image_resolver import peek_image_warm

        state = await asyncio.to_thread(
            peek_image_warm,
            base_image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
            credential_host=settings.image_registry_host,
        )
        return bool(state.get("cached"))
    import httpx
    from urllib.parse import quote

    url = (
        f"{node.address}/agent/images/{quote(base_image, safe='/:')}"
        "/warm"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url, headers={"X-Internal-Key": settings.internal_api_key}
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("required") is False:
                    return True
                return bool(body.get("cached"))
    except httpx.HTTPError:
        pass
    # Unknown/older worker: fall back to the fast path; provisioning will
    # fail loudly if the image is actually missing.
    return True


async def _warm_node(request, node, base_image, settings) -> None:
    """Ensure the base image rootfs is extracted on ``node`` (may be slow)."""
    if not base_image:
        return
    if node.address == "local://":
        from envd_service.runtime.image_resolver import resolve_image_rootfs

        await asyncio.to_thread(
            resolve_image_rootfs,
            base_image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
            credential_host=settings.image_registry_host,
        )
        return
    import httpx
    from urllib.parse import quote

    url = (
        f"{node.address}/agent/images/{quote(base_image, safe='/:')}"
        "/warm"
    )
    async with httpx.AsyncClient(timeout=settings.warm_timeout_s) as client:
        resp = await client.post(
            url, headers={"X-Internal-Key": settings.internal_api_key}
        )
        if resp.status_code != 200:
            raise RuntimeError(f"warm failed: {resp.status_code} {resp.text[:200]}")


async def _wait_pending(
    registry, sandbox_id: str, budget_s: float
) -> Any | None:
    """Wait up to ``budget_s`` for a pending create to resolve into a record."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        try:
            record = registry.get(sandbox_id)
        except UnknownSandboxError:
            record = None
        if record is not None:
            return record
        if registry.get_pending(sandbox_id) is None:
            return None
        await asyncio.sleep(0.5)
    return None


def _registry(request: Request) -> SandboxRegistry:
    return request.app.state.registry


def _mark_active(request: Request, record) -> None:
    """Record control-plane-side activity for idle detection (E9.1).

    Only lifecycle/mutating endpoints call this (connect, timeout, pause,
    resume, network update): the worker reports in-sandbox traffic
    separately through its heartbeat, and read-only polling (info, metrics,
    logs) plus internal endpoints (reconcile, node sandbox listing) must not
    keep an unused sandbox out of reach of the eviction selector.
    """
    _registry(request).mark_active(record)


def _unsupported_field_error(field: str) -> OfficialError:
    return OfficialError(400, f"Unsupported field: {field}")


def _unsupported_endpoint_error(feature: str) -> OfficialError:
    return OfficialError(501, f"Unsupported: {feature}")


def _normalize_iam(body: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Validate the SDK workload-identity config (``iam``).

    Wire shape: ``{"tokens": {"<name>": {"audience": str, "tokenType": str}}}``
    (the SDK serializes the client model with camelCase; ``token_type`` is
    accepted too).
    Token names must be placeholder-safe (no ``{``/``}``/control chars) because
    they are interpolated into ``${e2b.identity.tokens.<name>}`` header values.
    """
    iam = body.get("iam")
    if iam is None:
        return {}
    if not isinstance(iam, dict):
        raise OfficialError(400, "iam must be an object")
    tokens = iam.get("tokens")
    if tokens is None:
        return {}
    if not isinstance(tokens, dict):
        raise OfficialError(400, "iam.tokens must be an object")
    out: dict[str, dict[str, str]] = {}
    for name, token in tokens.items():
        if (
            not isinstance(name, str)
            or not name
            or any(c in name for c in "{}")
            or any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in name)
        ):
            raise OfficialError(
                400,
                f"iam token name {name!r} is not usable: must be a non-empty "
                "string without '{', '}' or control characters",
            )
        token_type = token.get("token_type", token.get("tokenType"))
        if (
            not isinstance(token, dict)
            or not isinstance(token.get("audience"), str)
            or not isinstance(token_type, str)
        ):
            raise OfficialError(
                400,
                f"iam token {name!r} must be an object with string "
                "'audience' and 'token_type' values",
            )
        out[name] = {
            "audience": token["audience"],
            "token_type": token_type,
        }
    return out


for _feature in UNSUPPORTED_ENDPOINTS:

    @router.api_route(
        f"/sandboxes/{{sandbox_id}}/{_feature}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def _unsupported_sandbox_endpoint(
        sandbox_id: str, request: Request, _feature: str = _feature
    ) -> None:
        raise _unsupported_endpoint_error(_feature)

    @router.api_route(
        f"/v2/sandboxes/{{sandbox_id}}/{_feature}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def _unsupported_v2_endpoint(
        sandbox_id: str, request: Request, _feature: str = _feature
    ) -> None:
        raise _unsupported_endpoint_error(_feature)


@router.api_route(
    "/templates/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    include_in_schema=False,
)
async def unsupported_templates(path: str, request: Request) -> None:
    raise _unsupported_endpoint_error("templates")


def _parse_metadata(metadata: str | None) -> dict[str, str]:
    """Parse URL-encoded ``key=value&key2=value2`` metadata filters."""
    if not metadata:
        return {}
    from urllib.parse import parse_qsl

    return dict(parse_qsl(metadata))


def _parse_cursor(next_token: str | None) -> int:
    """Parse a cursor token into an item offset."""
    if not next_token:
        return 0
    try:
        return max(0, int(next_token))
    except ValueError:
        return 0


def _log_ts(log: dict[str, str]) -> int:
    try:
        return int(
            datetime.fromisoformat(
                log["timestamp"].replace("Z", "+00:00")
            ).timestamp()
        )
    except (ValueError, KeyError):
        return 0


def _platform_pause_entry(record) -> dict[str, str] | None:
    """The platform's own pause, as the log line the SDK and operators read.

    The record's log *history* is in-memory by design (the record store keeps
    durable state only), so a line appended by ``SandboxRecord.pause`` is gone
    by the next ``get`` -- and with Redis in front, *every* read is the next
    get. The reason is durable (``pause_reason``), so this derives the line from
    it instead, and stays out of the way when the in-memory history already
    carries one.
    """
    if not record.pause_reason:
        return None
    line = f"sandbox paused: {record.pause_reason}"
    if any(entry.get("line") == line for entry in record.logs):
        return None
    return {
        "timestamp": to_iso_z(record.paused_at or record.last_active_at),
        "line": line,
    }


async def _command_logs(request, record) -> list[dict[str, str]]:
    """Command stdout/stderr lines recorded on the sandbox node."""
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is not None and node.address != "local://":
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{node.address}/agent/sandboxes/{record.sandbox_id}/logs",
                    headers={
                        "X-Internal-Key": request.app.state.settings.internal_api_key
                    },
                )
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, list):
                    return [
                        dict(e)
                        for e in payload
                        if isinstance(e, dict)
                        and "line" in e
                        and "timestamp" in e
                    ]
        except (httpx.HTTPError, ValueError):
            pass
        return []
    workspace = record.workspace_dir or (
        request.app.state.workspace_base / record.sandbox_id
    )
    # Platform file, so it lives beside the tree (``_runtime/<id>/``) rather
    # than inside it; the in-tree path is the pre-split location and stays
    # readable during a rolling upgrade. The *platform's* base is the app's,
    # not the one derived from the record above: with N27 the tree base and the
    # state base are different directories, and deriving one from the other is
    # how a reader ends up looking in a directory no writer ever wrote to.
    base = Path(workspace).parent
    log_path = sandbox_command_log_path(
        base, record.sandbox_id, state_base=request.app.state.state_base
    )
    if not log_path.is_file():
        # The pre-split location is inside the sandbox's own tree, so this one
        # is a workspace-base question (the state base plays no part in it).
        log_path = sandbox_command_log_path(base, record.sandbox_id, legacy=True)
    entries: list[dict[str, str]] = []
    if log_path.is_file():
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and "line" in entry and "timestamp" in entry:
                    entries.append(dict(entry))
        except OSError:
            pass
    return entries
    try:
        return max(0, int(next_token))
    except ValueError:
        return 0


@router.post("/sandboxes", status_code=201, dependencies=[Depends(require_api_key)])
async def create_sandbox(request: Request) -> dict[str, Any]:
    """Create a sandbox, retrying once after a bounded eviction round (E9.3).

    When admission fails for *capacity* (no node, or global/tenant quota) and
    ``E2B_EVICTION_ENABLED`` is on, the dispatcher evicts at most
    ``E2B_EVICTION_MAX_PER_CREATE`` idle victims (own tenant only unless admin
    or ``E2B_EVICTION_CROSS_TENANT``) and retries. Prefer-pause victims that
    still did not make room are killed before one final retry. Anything that is
    not a capacity failure propagates untouched, and the final 503 keeps
    ``recent_failures.record()`` exactly like before. With E9.4 queueing on
    (``E2B_CREATE_QUEUE_TIMEOUT_S``), a request that survived eviction but
    still has no room waits for a capacity release instead of failing right
    away; only the eventual timeout answers the original 503.
    """
    settings = request.app.state.settings
    tenant, is_admin = tenant_of(request)
    registry = _registry(request)
    sandbox_id_hdr = request.headers.get("X-Sandbox-Id")
    paused_victims: list[SandboxRecord] = []

    def _pause_hook(victim: SandboxRecord) -> None:
        """Pause hook: return the victim's node reservation and freeze the
        runtime (mirrors the manual pause endpoint, E9.2)."""
        _park_capacity(request, victim)
        request.app.state.runtime_registry.set_state(victim.sandbox_id, "paused")
        paused_victims.append(victim)

    async def _push_evicted_pause(victim: SandboxRecord) -> None:
        """Deliver a prefer-pause eviction freeze to a remote worker (G1a).

        Local victims already froze through the shared runtime-registry state
        callback in ``_pause_hook``; a remote victim needs the agent pause
        push. Transport loss stays best-effort (WARNING): the record keeps
        its paused bookkeeping and a later kill pass cleans the worker
        runtime. An explicit non-404 worker error attempts a guarded rollback
        (record re-fetched; only a record that still exists and is still
        paused is rolled back). When the rollback succeeds, or was skipped
        because the record vanished or its state moved off ``paused``, the
        victim leaves the kill-pass list; only a ``paused`` record the
        rollback could not re-admit stays in the list so the kill pass still
        cleans it up.
        """
        try:
            await _push_pause_state(request, victim, paused=True)
        except OfficialError as push_error:
            logger.warning(
                "eviction pause push failed for sandbox %s: %s",
                victim.sandbox_id,
                push_error.message,
            )
            outcome = _rollback_pause(request, registry, victim.sandbox_id)
            if outcome != "failed":
                try:
                    paused_victims.remove(victim)
                except ValueError:  # pragma: no cover - defensive
                    pass

    async def _evict_one() -> bool:
        """Evict one idle victim; False when disabled/throttled/no candidate."""
        results = registry.evict_for_capacity(
            tenant_id=tenant,
            is_admin=is_admin,
            exclude_ids=(sandbox_id_hdr,) if sandbox_id_hdr else (),
            max_victims=1,
            prefer_pause=settings.eviction_prefer_pause,
            pause_action=_pause_hook,
        )
        for result in results:
            if result.action == "killed":
                # kill 受害者复用 contract 的销毁路径：记录已删除，这里只
                # 销毁节点上的运行时（远程 agent DELETE / 本地清理）。
                # 失败不再是静默的成功：_destroy_evicted 记 WARNING，树留给
                # orphan-tree GC 回收（控制面记录已无该沙箱）。
                await _destroy_evicted(request, result.record)
            elif result.action == "paused":
                await _push_evicted_pause(result.record)
        return bool(results)

    async def _final_503(message: str) -> None:
        request.app.state.recent_failures.record()
        raise OfficialError(503, message)

    max_victims = settings.eviction_max_per_create if settings.eviction_enabled else 0
    evicted = 0
    message = "No resources available"
    while True:
        try:
            return await _create_sandbox_attempt(request, rate_limited=evicted == 0)
        except _CapacityExhausted as exc:
            message = str(exc) or "No resources available"
        if evicted >= max_victims or not await _evict_one():
            break
        evicted += 1
    if paused_victims:
        # prefer_pause 一轮仍拿不到配额：pause 保留了现场但没有腾出足够的
        # 容量，kill 掉这些候选（含现场清理）后再做最后一次尝试。
        for victim in paused_victims:
            registry.record_eviction(
                victim.sandbox_id,
                tenant_id=victim.tenant_id,
            )
            registry.delete(victim.sandbox_id)
            await _destroy_evicted(request, victim)
        paused_victims.clear()
        try:
            return await _create_sandbox_attempt(request, rate_limited=False)
        except _CapacityExhausted as exc:
            message = str(exc) or "No resources available"
    # E9.4：驱逐已无力回天——直接 503 之前先给“等容量释放”一个窗口。
    # 排队发生在 attempt 内部回滚（先还节点配额、再放 pending 标记）之后，
    # 因此等待中的请求既不占节点配额也不占 pending marker：同 id 的客户端
    # 重试不会被自己的 marker 卡住；每次被唤醒仍走完整准入（不超卖）。
    create_queue = request.app.state.create_queue
    if create_queue is not None and settings.create_queue_timeout_s > 0:
        admitted: dict[str, Any] = {}

        async def _queued_admission() -> bool:
            """One queued retry: True only when admission actually succeeded
            (probe consumed capacity atomically), False keeps waiting."""
            try:
                admitted["result"] = await _create_sandbox_attempt(
                    request, rate_limited=False
                )
                return True
            except _CapacityExhausted:
                return False

        outcome = await create_queue.wait_for_capacity(
            _queued_admission,
            timeout_s=settings.create_queue_timeout_s,
            max_waiters=settings.create_queue_max,
        )
        if outcome is QueueOutcome.ADMITTED:
            return admitted["result"]
        if outcome is QueueOutcome.FULL:
            # 并发排队数已达上限：不占配额、不重试，429 让客户端 1s 后重试
            # （OfficialError.headers 由 E9.3 引入）。
            #
            # 仍然计入 recent_failures：走到这里说明池子既满又没人肯腾容量，
            # 正是 autoscaler 需要的扩缩容信号（只是客户端拿到的是 load-shedding
            # 的 429，而不是 503）。
            request.app.state.recent_failures.record()
            raise OfficialError(
                429,
                "Sandbox create queue is full",
                headers={"retry-after": "1"},
            )
        # TIMEOUT / DISABLED：等待窗口耗尽（或排队未启用），保留原 503。
    await _final_503(message)


async def _create_sandbox_attempt(
    request: Request, *, rate_limited: bool = True
) -> dict[str, Any]:
    """One admission + provision attempt for ``POST /sandboxes`` (E9.3).

    Capacity failures roll back every partial reservation first (node quota,
    pending marker — in that order) and then raise ``_CapacityExhausted`` so
    the dispatcher can evict and retry without ever interleaving eviction into
    a half-finished create.

    ``rate_limited`` is ``False`` for the dispatcher's retries: one client
    request must spend exactly one create-rate-limit token, whichever attempt
    inside it wins admission.
    """
    settings = request.app.state.settings
    tenant, is_admin = tenant_of(request)
    if rate_limited:
        limiter = request.app.state.create_limiter
        key = (
            request.headers.get("X-API-Key") or request.headers.get("X-API-KEY", "")
        )
        if not limiter.allow(key):
            raise OfficialError(429, "Sandbox create rate limit exceeded")
        # Per-tenant create rate limit (E2B_TENANT_RATE_LIMITS; falls back to
        # the global per-minute budget). Admin keys and compatible mode skip it.
        if settings.tenants_enabled and tenant is not None and not is_admin:
            tenant_limiter = request.app.state.tenant_limiters.get(
                tenant
            ) or request.app.state.tenant_create_limiter
            if not tenant_limiter.allow(tenant):
                raise OfficialError(429, "Sandbox create rate limit exceeded")
    try:
        body = await read_json_body(request, settings.max_json_body_bytes)
    except UploadTooLargeError:
        raise OfficialError(413, "Request body exceeds maximum size")
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")

    for field in UNSUPPORTED_FIELDS:
        if field in body and body[field] is not None:
            raise _unsupported_field_error(field)

    template_id = body.get("templateID")
    if not isinstance(template_id, str) or not template_id:
        raise OfficialError(400, "templateID is required")

    base_image = settings.resolve_template_image(template_id)
    snapshot = None
    template_record = None
    if base_image is None and (
        template_id != "base"
        and template_id != "mcp-gateway"
        and template_id not in settings.template_images
    ):
        try:
            snapshot = request.app.state.snapshots.get(template_id)
        except UnknownSnapshotError:
            snapshot = None
        if snapshot is None:
            try:
                template_record = request.app.state.templates.get(template_id)
            except UnknownTemplateBuildError:
                try:
                    template_record = request.app.state.templates.get_by_name(
                        template_id
                    )
                except UnknownTemplateBuildError:
                    template_record = None
        if snapshot is None and template_record is None:
            raise OfficialError(400, f"Template {template_id} not found")
    if snapshot is not None:
        _require_related(
            request, snapshot, resource_id=snapshot.snapshot_id, label="Snapshot"
        )
    if template_record is not None:
        _require_related(
            request,
            template_record,
            resource_id=template_record.template_id,
            label="Template",
        )

    timeout = body.get("timeout", settings.default_timeout)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise OfficialError(400, "timeout must be a positive integer")

    # Eviction priority (E9.1/E9.3): optional 0-10, default 5, low = first
    # to be evicted when the fleet is out of capacity.
    priority = body.get("priority", PRIORITY_DEFAULT)
    if (
        not isinstance(priority, int)
        or isinstance(priority, bool)
        or not PRIORITY_MIN <= priority <= PRIORITY_MAX
    ):
        raise OfficialError(
            400,
            f"priority must be an integer between {PRIORITY_MIN} and {PRIORITY_MAX}",
        )

    metadata = body.get("metadata") or (snapshot.metadata if snapshot else {})
    env_vars = body.get("envVars") or (snapshot.env_vars if snapshot else {})
    if not isinstance(metadata, dict) or not isinstance(env_vars, dict):
        raise OfficialError(400, "metadata and envVars must be objects")
    _check_metadata_envvars_size(settings, metadata, env_vars)

    # MCP: only local stdio base servers are supported.
    mcp = body.get("mcp")
    if mcp is not None:
        if not isinstance(mcp, dict) or not isinstance(mcp.get("command"), str):
            raise OfficialError(
                400,
                "Unsupported MCP server: only base servers with a command are supported",
            )
        if "github" in mcp.get("name", "").lower():
            raise OfficialError(
                400, "Unsupported MCP server: GitHub servers are not supported"
            )

    # Volume mounts: [{name: <volumeID>, path: <mount path>}]
    volume_mounts: list[dict[str, str]] = []
    raw_mounts = body.get("volumeMounts")
    if raw_mounts is None and snapshot is not None:
        raw_mounts = snapshot.volume_mounts
    if raw_mounts:
        if not isinstance(raw_mounts, list):
            raise OfficialError(400, "volumeMounts must be a list")
        volume_registry = request.app.state.volumes
        for mount in raw_mounts:
            if not isinstance(mount, dict):
                raise OfficialError(400, "volumeMounts entries must be objects")
            name = mount.get("name")
            path = mount.get("path")
            if not isinstance(name, str) or not isinstance(path, str):
                raise OfficialError(400, "volumeMounts entries need name and path")
            try:
                volume = volume_registry.get(name)
            except Exception:
                raise OfficialError(404, f"Volume {name} not found")
            _require_related(request, volume, resource_id=name, label="Volume")
            volume_mounts.append({"name": name, "path": path})

    secure = body.get("secure", True)
    allow_internet_access = body.get("allow_internet_access", False)
    if not isinstance(secure, bool) or not isinstance(allow_internet_access, bool):
        raise OfficialError(400, "secure and allow_internet_access must be booleans")

    try:
        network = normalize_network_config(body.get("network"))
    except NetworkConfigError as e:
        raise OfficialError(400, str(e))
    iam_tokens = _normalize_iam(body)

    dims = _default_dims(settings)
    registry = _registry(request)

    # Idempotent create: a client-supplied sandbox ID short-circuits an
    # existing record (retry after timeout -> immediate 201).
    sandbox_id_hdr = request.headers.get("X-Sandbox-Id")
    if sandbox_id_hdr is not None:
        if not validate_sandbox_id(sandbox_id_hdr):
            raise OfficialError(400, "X-Sandbox-Id must be a valid sandbox id")
        # A sandbox tree lands at ``<base>/<id>``, so an id naming one of the
        # platform's own top-level namespaces would put a sandbox on top of
        # platform state -- ``_runtime`` now holds the sandbox's own record, so
        # this is the difference between a sandbox and the entry that records
        # it. Client ids are otherwise free-form; the create path is the one
        # place that can keep the two namespaces apart (interior scans must
        # keep accepting a reserved-looking name that carries a record: M1).
        if is_reserved_platform_namespace(sandbox_id_hdr):
            raise OfficialError(
                400,
                f"X-Sandbox-Id {sandbox_id_hdr!r} is a reserved platform "
                f"namespace",
            )
        try:
            existing = registry.get(sandbox_id_hdr)
        except UnknownSandboxError:
            existing = None
        if existing is not None:
            _require_owned(
                request, existing, resource_id=existing.sandbox_id, label="Sandbox"
            )
            return existing.as_sandbox()
        if registry.get_pending(sandbox_id_hdr) is not None:
            resolved = await _wait_pending(
                registry, sandbox_id_hdr, _PENDING_WAIT_S
            )
            if resolved is not None:
                _require_owned(
                    request,
                    resolved,
                    resource_id=resolved.sandbox_id,
                    label="Sandbox",
                )
                return resolved.as_sandbox()
            if registry.get_pending(sandbox_id_hdr) is not None:
                raise OfficialError(503, "Sandbox create still in progress")

    # Select a compute node and reserve its quota (volume/snapshot affinity).
    volume_node_id: str | None = None
    if snapshot is not None:
        volume_node_id = snapshot.node_id
    elif volume_mounts:
        volume_records = [
            request.app.state.volumes.get(m["name"]) for m in volume_mounts
        ]
        shared_root = settings.shared_volume_root
        shared = bool(
            shared_root
            and all(
                r.path is not None and r.path.is_relative_to(Path(shared_root).resolve())
                for r in volume_records
            )
        )
        if not shared:
            node_ids = {r.node_id for r in volume_records}
            if len(node_ids) > 1:
                raise OfficialError(400, "all volume mounts must be on the same node")
            volume_node_id = next(iter(node_ids)) if node_ids else None
    node = request.app.state.select_node(
        base_image=base_image,
        volume_node_id=volume_node_id,
        memory_mb=dims[0],
        cpu_percent=dims[1],
        disk_mb=dims[2],
        processes=dims[3],
    )
    if node is None:
        # 节点层无容量：还没有任何状态可回滚，直接交给调度器决定是否驱逐。
        raise _CapacityExhausted(_node_refusal_message(request, dims))

    # Adaptive warm: image cached -> fast path (server-side ID, SDK no-op);
    # image cold -> slow path requiring X-Sandbox-Id, warming before any
    # sandbox record exists (no orphans, idempotent retries).
    if sandbox_id_hdr is not None:
        if not registry.claim_pending(
            sandbox_id_hdr,
            {"node": node.node_id, "status": "warming"},
            ttl=_PENDING_TTL_S,
        ):
            resolved = await _wait_pending(
                registry, sandbox_id_hdr, _PENDING_WAIT_S
            )
            _release_node_quota(request, node, dims)
            if resolved is not None:
                return resolved.as_sandbox()
            if registry.get_pending(sandbox_id_hdr) is not None:
                raise OfficialError(503, "Sandbox create still in progress")
            # Marker expired (owner crashed): take ownership and continue.
            registry.claim_pending(
                sandbox_id_hdr,
                {"node": node.node_id, "status": "warming"},
                ttl=_PENDING_TTL_S,
            )

    slow_path = not await _image_warm(request, node, base_image, settings)
    if slow_path and sandbox_id_hdr is None:
        _release_node_quota(request, node, dims)
        request.app.state.recent_failures.record()
        raise OfficialError(
            428,
            "warm_required: base image not cached on the target node; "
            "retry with the X-Sandbox-Id header to enable idempotent create",
        )
    if slow_path:
        try:
            await _warm_node(request, node, base_image, settings)
        except Exception as e:
            registry.release_pending(sandbox_id_hdr)
            _release_node_quota(request, node, dims)
            request.app.state.recent_failures.record()
            raise OfficialError(503, f"Image warm failed: {e}") from e
        try:
            existing = registry.get(sandbox_id_hdr)
        except UnknownSandboxError:
            existing = None
        if existing is not None:
            # A concurrent attempt finished while we warmed.
            registry.release_pending(sandbox_id_hdr)
            _release_node_quota(request, node, dims)
            _require_owned(
                request, existing, resource_id=existing.sandbox_id, label="Sandbox"
            )
            return existing.as_sandbox()

    secrets = request.app.state.secrets
    try:
        raw_env_vars = {str(k): str(v) for k, v in env_vars.items()}
        resolved_env = secrets.resolve_env_refs(
            raw_env_vars,
            tenant_id=tenant,
            is_admin=is_admin,
        )
        # Secret expansion replaces a short ``${name}`` ref with the secret
        # value, which can be much larger than the raw envVars. Re-check the
        # resolved envVars so sandbox.json stays bounded.
        _check_metadata_envvars_size(settings, metadata, resolved_env)
    except SecretTenantMismatchError as e:
        _release_node_quota(request, node, dims)
        registry.release_pending(sandbox_id_hdr)
        raise OfficialError(403, str(e)) from e
    try:
        record = registry.create(
            template_id=template_id,
            sandbox_id=sandbox_id_hdr,
            timeout=timeout,
            metadata={str(k): str(v) for k, v in metadata.items()},
            env_vars=resolved_env,
            secure=secure,
            allow_internet_access=(
                allow_internet_access
                if snapshot is None
                else snapshot.allow_internet_access
            ),
            base_image=(
                base_image
                or (snapshot.base_image if snapshot else None)
                or (template_record.image if template_record else None)
            ),
            volume_mounts=volume_mounts,
            mcp=dict(mcp) if mcp is not None else None,
            network=network,
            iam_tokens=iam_tokens,
            tenant_id=tenant,
            is_admin=is_admin,
            priority=priority,
        )
    except ResourceUnavailableError as e:
        # 保持既有回滚顺序：先还节点配额，再释放 pending 标记。驱逐重试由
        # 外层调度器接管，绝不插进这个半成品状态里。
        _release_node_quota(request, node, dims)
        registry.release_pending(sandbox_id_hdr)
        raise _CapacityExhausted(str(e)) from e
    except ValueError as e:
        _release_node_quota(request, node, dims)
        registry.release_pending(sandbox_id_hdr)
        raise OfficialError(400, str(e)) from e
    record.node_id = node.node_id
    registry.save(record)
    registry.release_pending(sandbox_id_hdr)

    # OBS-9: the fleet-wide host uid is allocated here, in the registry's
    # shared store, and travels down with the provision call -- the worker must
    # not derive it from the sandbox tree, because that tree is writable by any
    # root on any mounting node. The record carries it, so migration and
    # re-provisioning reuse the same uid, and record removal (including every
    # rollback below) returns it to the pool.
    host_uid = None
    if settings.per_sandbox_uid:
        host_uid = registry.allocate_host_uid(record.sandbox_id)
        if host_uid is None:
            registry.delete(record.sandbox_id)
            raise OfficialError(
                503,
                "per-sandbox uid pool exhausted: every host uid in "
                f"[{settings.uid_pool_start}, "
                f"{settings.uid_pool_start + settings.uid_pool_size}) is taken",
            )
        record.host_uid = host_uid
        registry.save(record)

    try:
        if node.address == "local://":
            workspace_dir = _provision_local(
                request, record, snapshot, volume_mounts, settings
            )
        else:
            await _provision_remote(
                request,
                record,
                node,
                settings,
                snapshot,
                volume_mounts,
                snapshot_id=snapshot.snapshot_id if snapshot else None,
            )
        record.append_log("sandbox created")
        registry.save(record)
    except OfficialError:
        registry.delete(record.sandbox_id)
        raise
    except Exception as e:
        registry.delete(record.sandbox_id)
        raise OfficialError(500, f"Failed to provision sandbox runtime: {e}") from e

    logger.info(
        "created sandbox %s (template=%s image=%s node=%s)",
        record.sandbox_id,
        template_id,
        base_image,
        node.node_id,
    )
    return record.as_sandbox()


def _provision_local(request, record, snapshot, volume_mounts, settings) -> None:
    """Provision the sandbox on the in-process (local) worker."""
    workspace_dir = request.app.state.workspace_base / record.sandbox_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if snapshot is not None:
        request.app.state.snapshots.expand_to(snapshot, workspace_dir)
    else:
        (workspace_dir / "workspace").mkdir(parents=True, exist_ok=True)
    record.workspace_dir = workspace_dir
    existing = request.app.state.runtime_registry.get(record.sandbox_id)
    # E3.2: allocate the sandbox's host uid through the shared worker uid
    # pool before materializing volumes so per-sandbox volume slices are
    # chowned to it. Only a root worker -- or a non-root worker with C3's
    # per-node agent wired, which performs the chown there -- can put a sandbox
    # under its own host uid; otherwise the fixed-uid + Landlock model applies.
    host_uid = None
    pool = getattr(request.app.state.runtime_registry, "uid_pool", None)
    from envd_service import priv_helpers

    if pool is not None and (
        os.geteuid() == 0 or priv_helpers.file_steps_available()
    ):
        # The registry allocated this uid fleet-wide before we got here
        # (OBS-9); the pool's own allocator is only the fallback for records
        # that predate the change.
        if record.host_uid is not None:
            host_uid = pool.claim(record.sandbox_id, record.host_uid)
        else:
            host_uid = pool.acquire(
                record.sandbox_id,
                preferred=existing.host_uid if existing is not None else None,
            )
    try:
        try:
            from envd_service.volumes import build_volume_mounts
        except ImportError:  # pragma: no cover - separated control plane
            raise OfficialError(500, "local node requires the envd service")
        mount_inputs = []
        for mount in volume_mounts:
            volume = request.app.state.volumes.get(mount["name"])
            mount_inputs.append(
                {
                    "name": mount["name"],
                    "path": mount["path"].lstrip("/"),
                    "hostPath": str(volume.path),
                    "perSandboxQuotaMb": volume.per_sandbox_quota_mb,
                }
            )
        try:
            mount_paths, volume_projects = build_volume_mounts(
                sandbox_id=record.sandbox_id,
                volume_mounts=mount_inputs,
                shared_volume_root=settings.shared_volume_root,
                workspace_dir=workspace_dir,
                fallback_mount_point=settings.workspace_base,
                # The combined node's volume quota follows the same switch as
                # the envd service it runs in-process (``E2B_QUOTA_AGENT_URL``
                # / ``E2B_QUOTA_VIA_AGENT``): a non-root merged image has no
                # ``xfs_quota`` and no ``CAP_SYS_ADMIN``, and an NFS volume
                # has no local project quota at all.
                via_agent=local_node_quota_via_agent(),
                existing_volume_projects=(
                    existing.volume_projects if existing is not None else []
                ),
                host_uid=host_uid,
            )
        except ValueError as e:
            raise OfficialError(400, str(e))
        if host_uid is not None:
            from envd_service.uid_pool import apply_sandbox_ownership

            apply_sandbox_ownership(workspace_dir, host_uid)
        elif pool is None:
            # FUP #6: local (combined) worker in the legacy shared-uid shape
            # — no uid pool means no per-sandbox host uid, so a root worker
            # must chown the root-created workspace to the shared RunAs uid
            # 1000 (same identity sandlock maps; the pure no-chroot sandbox
            # shell writes it directly). Non-root workers no-op inside.
            from envd_service.uid_pool import align_shared_uid_workspace

            align_shared_uid_workspace(workspace_dir)
        request.app.state.runtime_registry.register(
            sandbox_id=record.sandbox_id,
            access_token=record.envd_access_token,
            workspace_dir=str(workspace_dir),
            env_vars=record.env_vars,
            base_image=record.base_image,
            host_uid=host_uid,
            memory_mb=record.memory_mb,
            cpu_percent=record.cpu_count * 100,
            disk_mb=record.disk_size_mb,
            max_processes=record.max_processes,
            allow_internet_access=record.allow_internet_access,
            max_command_timeout=settings.max_command_timeout,
            volume_mounts=mount_paths,
            mcp=record.mcp,
            network=record.network,
            iam_tokens=record.iam_tokens,
            allow_public_traffic=bool(
                (record.network or {}).get("allowPublicTraffic", False)
            ),
            volume_projects=volume_projects,
        )
    except BaseException:
        # I3: a provisioning failure after acquire (invalid mount config,
        # ownership/quota errors, register failure) must return the reserved
        # uid to the pool instead of leaking a slot.
        if host_uid is not None and pool is not None:
            pool.release(record.sandbox_id)
        raise
    # I1: the record is durable — drop the cross-process reservation marker.
    if host_uid is not None and pool is not None:
        pool.commit(record.sandbox_id)


async def _provision_remote(
    request, record, node, settings, snapshot, volume_mounts, snapshot_id=None
) -> None:
    """Provision the sandbox on a remote worker through its agent API."""
    import httpx

    payload = {
        "sandboxID": record.sandbox_id,
        "accessToken": record.envd_access_token,
        "envVars": record.env_vars,
        "baseImage": record.base_image,
        "memoryMB": record.memory_mb,
        "cpuPercent": record.cpu_count * 100,
        "diskMB": record.disk_size_mb,
        "maxProcesses": record.max_processes,
        # OBS-9: the fleet-wide host uid, allocated by the registry. Absent
        # only for records older than the change, where the worker falls back
        # to its own pool.
        "hostUID": record.host_uid,
        "allowInternetAccess": record.allow_internet_access,
        "allowPublicTraffic": bool(
            (record.network or {}).get("allowPublicTraffic", False)
        ),
        "network": record.network,
        "maxCommandTimeout": settings.max_command_timeout,
        "volumeMounts": [
            {
                "path": m["path"].lstrip("/"),
                "name": m["name"],
                "hostPath": str(
                    request.app.state.volumes.get(m["name"]).path
                ),
                "perSandboxQuotaMb": request.app.state.volumes.get(
                    m["name"]
                ).per_sandbox_quota_mb,
            }
            for m in volume_mounts
        ],
        "mcp": record.mcp,
        "iamTokens": record.iam_tokens,
        "snapshotTar": None,
        "snapshotID": snapshot_id,
    }
    internal_key = settings.internal_api_key
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{node.address}/agent/sandboxes",
                json=payload,
                headers={"X-Internal-Key": internal_key},
            )
    except httpx.HTTPError as e:
        raise OfficialError(502, f"Node {node.node_id} unavailable: {e}") from e
    if resp.status_code >= 300:
        raise OfficialError(502, f"Node {node.node_id} failed to provision: {resp.text}")
    record.workspace_dir = None


@router.get("/sandboxes", dependencies=[Depends(require_api_key)])
async def list_sandboxes_legacy(request: Request) -> list[dict[str, Any]]:
    registry = _registry(request)
    records = registry.list(limit=None, tenant_id=tenant_scope(request))
    return [r.as_listed() for r in records]


@router.get("/v2/sandboxes", dependencies=[Depends(require_api_key)])
async def list_sandboxes(
    request: Request,
    response: Response,
    metadata: str | None = Query(default=None),
    state: str | None = Query(default=None),
    order: str = Query(default="desc"),
    startedAfter: datetime | None = Query(default=None, alias="startedAfter"),
    template: str | None = Query(default=None),
    nextToken: str | None = Query(default=None, alias="nextToken"),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[dict[str, Any]]:
    if order not in ("asc", "desc"):
        raise OfficialError(400, "order must be asc or desc")
    state_filter = None
    if state is not None:
        state_filter = [s for s in state.split(",") if s]
    registry = _registry(request)
    tenant = tenant_scope(request)
    total = len(
        registry.list(
            metadata_filter=_parse_metadata(metadata),
            state_filter=state_filter,
            order=order,
            started_after=startedAfter,
            template=template,
            limit=None,
            tenant_id=tenant,
        )
    )
    offset = _parse_cursor(nextToken)
    page = registry.list(
        metadata_filter=_parse_metadata(metadata),
        state_filter=state_filter,
        order=order,
        started_after=startedAfter,
        template=template,
        limit=limit,
        offset=offset,
        tenant_id=tenant,
    )
    # Cursor pagination: the next token encodes the absolute offset of the
    # following page.
    if offset + len(page) < total:
        response.headers["X-Next-Token"] = str(offset + len(page))
    return [r.as_listed() for r in page]


@router.get("/sandboxes/{sandbox_id}", dependencies=[Depends(require_api_key)])
async def get_sandbox_info(sandbox_id: str, request: Request) -> dict[str, Any]:
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        return record.as_detail()
    except UnknownSandboxError:
        notice = registry.eviction_notice(sandbox_id)
        if notice is not None:
            # 驱逐不静默消失（E9.3）：带原因的 404 + 响应头。跨租户请求仍
            # 保持普通 404，避免拿驱逐提示当存在性探测。
            tenant, is_admin = tenant_of(request)
            notice_tenant = notice.get("tenant_id")
            if is_admin or tenant is None or tenant == notice_tenant:
                reason = notice.get("reason", "evicted-idle")
                raise OfficialError(
                    404,
                    f"Sandbox {sandbox_id} not found (evicted: {reason})",
                    headers={"x-e2b-eviction-reason": reason},
                )
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")


@router.delete("/sandboxes/{sandbox_id}", status_code=204, dependencies=[Depends(require_api_key)])
async def kill_sandbox(
    sandbox_id: str, request: Request, force: bool = Query(default=False)
) -> Response:
    """Kill one sandbox, and only report success once the teardown happened.

    The record is deleted *after* the hosting node acknowledges the teardown
    (review W7 / C1-2). It used to be deleted first and the remote answer was
    never looked at, so a node that refused the teardown (a rewritten
    ``sandbox.json`` -> 409) still answered the SDK with 204: the control
    plane had forgotten the sandbox while its tree, its quota row and its
    process tree were all still there. A failure now keeps the record (so the
    sandbox stays visible and retryable) and answers 502.

    A node that cannot be reached at all is the one exception, and it is the
    documented E6.1 case: nothing can be confirmed, the worker's next
    reconcile reclaims the tree and its row, and the kill still completes --
    with a WARNING naming the sandbox, never silently (see
    :class:`_TeardownOutcome`).

    ``force=true`` is the operator's bounded exit for a record that
    contradicts the disk: the teardown then runs on the convention path and
    the project ids the disk reports, never on the record's claims. It is
    restricted to keys that are not tenant-scoped (admin or single-tenant
    mode), because it is not something a tenant's SDK call should turn on.
    """
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    if force:
        tenant, is_admin = tenant_of(request)
        if not (is_admin or tenant is None):
            raise OfficialError(403, "force teardown requires an admin API key")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None and (record.node_id or "local") != "local":
        # The node this sandbox was scheduled on is gone from the registry:
        # no address can confirm the teardown, so the record must not be
        # dropped on the strength of a local no-op.
        logger.warning(
            "sandbox %s: node %s is not in the registry; teardown cannot be "
            "confirmed",
            sandbox_id,
            record.node_id,
        )
        outcome = _TeardownOutcome(acknowledged=False)
    elif node is not None and node.address != "local://":
        outcome = await _destroy_remote(request, record, node, force=force)
    else:
        outcome = _destroy_local(request.app.state, record, force=force)
    if not outcome.acknowledged and not outcome.deferred:
        # Keep the record: the sandbox is still there (its runtime was stopped
        # where that was possible, its files are kept), so a 204 here would be
        # the "control plane forgot it" state with the tree still running.
        record.state = "orphaned"
        record.append_log(
            "delete: the node did not confirm the teardown; runtime stopped, "
            "files kept"
        )
        registry.save(record)
        logger.warning(
            "sandbox %s: teardown not confirmed; record kept as orphaned",
            sandbox_id,
        )
        raise OfficialError(
            502,
            f"Sandbox {sandbox_id} teardown failed on node "
            f"{record.node_id}; its runtime was stopped and its files are kept",
        )
    if outcome.deferred:
        logger.warning(
            "sandbox %s: teardown deferred to the worker's next reconcile "
            "(node %s unreachable); the record is released and the worker "
            "reclaims the tree and its quota row",
            sandbox_id,
            record.node_id,
        )
    registry.delete(sandbox_id)
    return Response(status_code=204)


class _TeardownOutcome(NamedTuple):
    """What one teardown attempt achieved (review W7 / C1-1).

    ``acknowledged`` is True only when the sandbox really is gone from its
    node: the agent answered 204, or the local teardown ran. Everything else
    is a failure the caller has to handle, and ``deferred`` separates the two
    kinds of failure, because they have different exits:

    * the node *answered* and refused or failed (a non-2xx, e.g. the agent's
      409 for a ``sandbox.json`` that contradicts the disk): nothing was torn
      down and nothing else will reclaim it, so the record has to stay and
      the caller has to fail;
    * the node could not be reached at all: the E6.1 reconcile on the worker's
      next start reclaims the tree and its quota row (a deliberate contract:
      ``test_kill_while_the_hosting_worker_is_down_is_reclaimed_on_the_next
      _start``), so the record may still go -- but never silently.
    """

    acknowledged: bool
    deferred: bool = False


async def _destroy_remote(
    request,
    record,
    node,
    keep_files: bool = False,
    keep_volume_slices: bool = False,
    force: bool = False,
) -> _TeardownOutcome:
    """Tear a sandbox down through its node's agent, and report the answer.

    The answer is what the agent said (204 = torn down, 409 = refused, ...)
    and never "we asked and hoped": treating a refusal as success is what let
    the control plane forget a sandbox whose tree, quota row and process tree
    were all still there (review W7 / C1-1).
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{node.address}/agent/sandboxes/{record.sandbox_id}"
            params = []
            if keep_files:
                params.append("keepFiles=true")
            if keep_volume_slices:
                params.append("keepVolumeSlices=true")
            if force:
                params.append("force=true")
            if params:
                url += "?" + "&".join(params)
            resp = await client.delete(
                url,
                headers={
                    "X-Internal-Key": request.app.state.settings.internal_api_key
                },
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "sandbox %s: node %s did not answer its teardown request: %s; "
            "the tree and its runtime are left to that worker's next "
            "reconcile",
            record.sandbox_id,
            getattr(node, "node_id", node.address),
            exc,
        )
        return _TeardownOutcome(acknowledged=False, deferred=True)
    if resp.status_code != 204:
        logger.warning(
            "sandbox %s: node %s refused the teardown (HTTP %s): %s; its "
            "runtime was stopped and its files are kept",
            record.sandbox_id,
            getattr(node, "node_id", node.address),
            resp.status_code,
            (resp.text or "")[:300],
        )
        return _TeardownOutcome(acknowledged=False)
    return _TeardownOutcome(acknowledged=True)


def _destroy_local(
    state,
    record,
    keep_files: bool = False,
    keep_volume_slices: bool = False,
    force: bool = False,
) -> _TeardownOutcome:
    """Stop and clean a local sandbox runtime, including volume slices.

    ``keep_files=True`` keeps the workspace and volume slices (migration
    source stop / shared-workspace teardown). ``keep_volume_slices=True``
    keeps only the per-sandbox volume slices while still removing the
    workspace — used by migration success and rollback, where the shared
    volume slice is already in use by the target sandbox and must survive.

    The volume entries come from ``sandbox.json`` inside the sandbox's own
    tree — input the sandbox can rewrite — so they go through the same
    verified target set the worker's delete endpoint uses (review W7 / C2):
    a slice has to be named after this sandbox and live under the configured
    volume root, and the project id released is the one the disk reports,
    never the record's claim. Reports a refused teardown (and keeps the files)
    when the record contradicts the tree it was found in; the runtime is
    stopped in that case too, so a refusal never leaves a process tree behind
    a sandbox the control plane has already forgotten.

    A successful answer means the tree is *gone* from the disk (review W7 /
    W7-2): the removal runs through the worker's helper — in-process first,
    then the ``e2b-maint`` broker — and is confirmed by looking at the disk. A
    tree that survives it, a refused record, or a refusal that armed the
    just-unregistered marker (review W7 / W7-1) all report
    ``acknowledged=False`` so the caller keeps the record and answers 502.
    """
    sandbox_id = record.sandbox_id
    runtime_registry = getattr(state, "runtime_registry", None)
    runtime = (
        runtime_registry.get(sandbox_id)
        if runtime_registry is not None
        else None
    )
    if not keep_files:
        volume_projects, refused = _verified_local_volume_projects(
            state, sandbox_id, runtime, force=force
        )
        if refused is not None:
            logger.warning(
                "local delete: refusing to tear down %s: %s",
                sandbox_id,
                refused,
            )
            if runtime_registry is not None:
                runtime_registry.unregister(sandbox_id)
                # The just-unregistered marker covers a teardown (race B);
                # this was a refusal, and the record file stays on disk on
                # purpose. Left armed it blinds the *next* delete's record
                # read for UNREGISTER_TOMBSTONE_S seconds, and "no record" is
                # read as "nothing to verify", which deleted the files the
                # refusal promised to keep (review W7 / W7-1).
                release = getattr(runtime_registry, "release_tombstone", None)
                if release is not None:
                    release(sandbox_id)
            return _TeardownOutcome(acknowledged=False)
        if not keep_volume_slices and volume_projects:
            try:
                from envd_service.volumes import cleanup_volume_projects
            except ImportError:  # pragma: no cover - separated control plane
                pass
            else:
                cleanup_volume_projects(
                    volume_projects=volume_projects,
                    fallback_mount_point=state.workspace_base,
                    # Release (and the GC fallback behind it) takes the same
                    # switch as provisioning, or a released project would be
                    # asked of the wrong side.
                    via_agent=local_node_quota_via_agent(),
                )
        if not _remove_local_tree_confirming(state, sandbox_id):
            # The tree survived (a sealed directory the worker cannot remove,
            # the worker's own DAC, a mount it may not walk): that is a failed
            # teardown, not a successful one. Reporting success here is what
            # left "record gone, tree still on disk" on the combined node --
            # and the leftovers carry no readable record, so the orphan-tree
            # GC can never reclaim them (review W7 / W7-2). The caller keeps
            # the record (orphaned) and the SDK sees 502.
            if runtime_registry is not None:
                runtime_registry.unregister(sandbox_id)
                release = getattr(runtime_registry, "release_tombstone", None)
                if release is not None:
                    release(sandbox_id)
            return _TeardownOutcome(acknowledged=False)
    if runtime_registry is not None:
        runtime_registry.unregister(sandbox_id)
    return _TeardownOutcome(acknowledged=True)


def _remove_local_tree_confirming(state, sandbox_id: str) -> bool:
    """Remove ``<base>/<id>`` and confirm it is really gone (review W7 / W7-2).

    The removal goes through the worker's own helper (in-process first, then
    the ``e2b-maint`` broker) instead of ``shutil.rmtree(ignore_errors=True)``,
    whose silent partial failure was indistinguishable from success. The
    answer is the disk's: the tree has to be gone when this returns.

    ``False`` means the tree is still there and the caller must report a
    failure (the record stays, the SDK sees 502).
    """
    tree = state.workspace_base / sandbox_id
    runtime_dir = sandbox_runtime_dir(
        state.workspace_base, sandbox_id, state_base=state.state_base
    )
    try:
        from envd_service import priv_helpers
    except ImportError:  # pragma: no cover - separated control plane
        priv_helpers = None
    try:
        if priv_helpers is not None:
            # ``on_error="raise"``: no broker (or a broker that refuses) has
            # to surface as a failure, not as a silent partial delete.
            priv_helpers.remove_tree(tree, on_error="raise")
        else:
            shutil.rmtree(tree)
    except FileNotFoundError:
        # The tree is already gone; the platform's paired directory is not, and
        # it is removed by the same confirming path below (A5).
        return _remove_local_runtime_confirming(
            state, sandbox_id, runtime_dir=runtime_dir, priv_helpers=priv_helpers
        )
    except Exception as exc:
        logger.warning(
            "local delete: %s could not be removed in-process or through the "
            "broker: %s",
            sandbox_id,
            exc,
        )
        return False
    if tree.exists() or tree.is_symlink():
        logger.warning(
            "local delete: %s survived its removal; the teardown did not "
            "happen",
            sandbox_id,
        )
        return False
    # Paired收尾 (N12/N24): the platform's files live beside the tree now, so
    # they go with it -- and only with it, since the record is what the next
    # delete verifies against.
    return _remove_local_runtime_confirming(
        state, sandbox_id, runtime_dir=runtime_dir, priv_helpers=priv_helpers
    )


def _remove_local_runtime_confirming(
    state, sandbox_id: str, *, runtime_dir: Path, priv_helpers
) -> bool:
    """Remove ``<state base>/_runtime/<id>`` and confirm it is really gone (A5).

    This half used to be ``shutil.rmtree(..., ignore_errors=True)`` followed by
    an unconditional ``return True`` -- the exact "silent half-delete reads as
    success" shape review W7 removed from the tree half. It matters because the
    directory is created ``0700`` owned by the *worker* (``_ensure_runtime_dir``
    chowns it to ``geteuid``), so a control plane that is neither root nor that
    uid -- which Task 5 makes it -- cannot delete it and used to say nothing
    (reproduced on the cluster by
    ``deploy/scripts/acceptance/probe_c3_a5_silent_rmtree.py``; docs §13.7).

    ``False`` means the directory is still there, and the caller must report a
    failure rather than a teardown that did not happen.
    """
    try:
        if priv_helpers is not None:
            priv_helpers.remove_tree(runtime_dir, on_error="raise")
        else:
            shutil.rmtree(runtime_dir)
    except FileNotFoundError:
        return True
    except Exception as exc:
        logger.warning(
            "local delete: the platform state of %s could not be removed "
            "in-process or through the broker: %s",
            sandbox_id,
            exc,
        )
        return False
    if runtime_dir.exists() or runtime_dir.is_symlink():
        logger.warning(
            "local delete: the platform state of %s survived its removal; the "
            "teardown did not happen",
            sandbox_id,
        )
        return False
    return True


def _verified_local_volume_projects(
    state, sandbox_id: str, runtime, *, force: bool
) -> tuple[list[dict[str, Any]], str | None]:
    """Verified volume targets for a combined-node teardown (review W7 / C2).

    Returns ``(volume_projects, None)`` when the record describes its own tree
    (the entries are the ones whose slice name, volume root and disk project id
    all check out), and ``(<the record's claims untouched>, reason)`` when the
    record contradicts the disk, which the caller refuses.

    The rule is the worker's rule, not a second implementation of it: the
    targets come from :func:`envd_service.agent._verified_teardown_plan`, the
    one place that reads them off the disk.
    """
    settings = getattr(state, "settings", None)
    try:
        from envd_service.agent import _verified_teardown_plan
    except ImportError:  # pragma: no cover - separated control plane
        # No envd service in this image: there is no verified way to pick a
        # slice, so none is touched (the old record-driven selection would
        # delete whatever a rewritten ``sandbox.json`` named).
        return [], None
    plan, reason = _verified_teardown_plan(
        state.workspace_base,
        sandbox_id,
        runtime,
        shared_volume_root=getattr(settings, "shared_volume_root", None),
        context="local delete",
        force=force,
        # The tree's own workspace project belongs to the envd half of a
        # combined node (``_provision_local`` does not create one), so this
        # teardown releases slices only -- the same accounting it did before.
        verify_tree_project=False,
    )
    if plan is None:
        return [], reason
    return list(plan.volume_projects), None


async def _destroy_on_node(
    request,
    record,
    node,
    keep_files: bool = False,
    keep_volume_slices: bool = False,
) -> _TeardownOutcome:
    if node.address == "local://":
        return _destroy_local(
            request.app.state,
            record,
            keep_files=keep_files,
            keep_volume_slices=keep_volume_slices,
        )
    return await _destroy_remote(
        request,
        record,
        node,
        keep_files=keep_files,
        keep_volume_slices=keep_volume_slices,
    )


async def _destroy_evicted(request, record) -> bool:
    """Tear down the runtime of an eviction-killed sandbox (E9.3).

    The registry already removed the record (and released its admission/node
    quota through the normal delete chain); this mirrors ``kill_sandbox``'s
    teardown so the worker actually stops the runtime and drops its files.

    Returns whether the node acknowledged the teardown. There is no record
    left to keep, so a failure is not a silent 204 either: it is logged (the
    ``_destroy_*`` helpers name the sandbox and the reason) and the tree is
    left on the disk as an orphan, which the orphan-tree GC reclaims on its
    next round — the sandbox is in no control-plane record any more, so the
    sweep is exactly the retry this shape needs.
    """
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is not None and node.address != "local://":
        outcome = await _destroy_remote(request, record, node)
    else:
        outcome = _destroy_local(request.app.state, record)
    if not outcome.acknowledged:
        logger.warning(
            "eviction: the runtime of %s was not torn down (%s); its tree is "
            "left to the orphan-tree GC",
            record.sandbox_id,
            "node unreachable" if outcome.deferred else "node refused",
        )
    return outcome.acknowledged


async def _stop_source_runtime(request, record, node) -> bool:
    """Stop the sandbox runtime on the source node, keeping its files.

    Unregistering the runtime kills the process tree, closing the dual-active
    window: once the gateway route switches, no command can still be served
    by the old node. Returns ``False`` when the node did not acknowledge the
    stop, in which case the caller must abort the migration.
    """
    if node.address == "local://":
        return _destroy_local(request.app.state, record, keep_files=True).acknowledged
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{node.address}/agent/sandboxes/{record.sandbox_id}"
                "?keepFiles=true",
                headers={
                    "X-Internal-Key": request.app.state.settings.internal_api_key
                },
            )
    except httpx.HTTPError:
        return False
    return resp.status_code == 204


def _migration_volume_node_id(request, record) -> str | None:
    """Return the node pinning non-shared volumes, else ``None``."""
    if not record.volume_mounts:
        return None
    volume_registry = request.app.state.volumes
    settings = request.app.state.settings
    shared_root = settings.shared_volume_root
    volume_records = [volume_registry.get(m["name"]) for m in record.volume_mounts]
    shared = bool(
        shared_root
        and all(
            r.path is not None
            and r.path.is_relative_to(Path(shared_root).resolve())
            for r in volume_records
        )
    )
    if shared:
        return None
    node_ids = {r.node_id for r in volume_records}
    if len(node_ids) > 1:
        raise OfficialError(400, "all volume mounts must be on the same node")
    return next(iter(node_ids)) if node_ids else None


def _extract_sandbox_archive(archive_path: Path, dest: Path) -> None:
    """Extract a sandbox tar.gz, skipping absolute symlink members.

    Volume mounts are archived as symlinks to host paths that only exist on
    the source node; provisioning re-creates them on the target.
    """
    with tarfile.open(archive_path) as tar:
        members = []
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f"archive member escapes workspace: {member.name}")
            if member.issym() and os.path.isabs(member.linkname):
                continue
            members.append(member)
        try:
            tar.extractall(dest, members=members, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12
            tar.extractall(dest, members=members)


async def _export_sandbox_archive(request, record, node) -> Path:
    """Return a local tar.gz path containing the sandbox workspace."""
    migrate_dir = request.app.state.workspace_base / "_migrate"
    migrate_dir.mkdir(parents=True, exist_ok=True)
    tar_path = migrate_dir / f"{record.sandbox_id}.tar.gz"
    if node.address == "local://":
        workspace = request.app.state.workspace_base / record.sandbox_id
        if not workspace.is_dir():
            raise OfficialError(
                404, f"Sandbox workspace not found on node {node.node_id}"
            )
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(workspace, arcname=".", recursive=True)
        return tar_path
    import httpx

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(
                f"{node.address}/agent/sandboxes/{record.sandbox_id}/export",
                headers={
                    "X-Internal-Key": request.app.state.settings.internal_api_key
                },
            )
    except httpx.HTTPError as e:
        raise OfficialError(502, f"Node {node.node_id} unavailable: {e}") from e
    if resp.status_code != 200:
        raise OfficialError(
            502, f"Node {node.node_id} failed to export: {resp.text}"
        )
    tar_path.write_bytes(resp.content)
    return tar_path


async def _import_sandbox_archive(request, record, node, tar_path) -> None:
    """Restore the sandbox workspace on the target node from a tar.gz."""
    if node.address == "local://":
        workspace = request.app.state.workspace_base / record.sandbox_id
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            _extract_sandbox_archive(tar_path, workspace)
        except (tarfile.TarError, OSError, ValueError) as e:
            raise OfficialError(400, f"Invalid sandbox archive: {e}") from e
        return
    import httpx

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{node.address}/agent/sandboxes/{record.sandbox_id}/import",
                content=tar_path.read_bytes(),
                headers={
                    "X-Internal-Key": request.app.state.settings.internal_api_key
                },
            )
    except httpx.HTTPError as e:
        raise OfficialError(502, f"Node {node.node_id} unavailable: {e}") from e
    if resp.status_code != 204:
        raise OfficialError(
            502, f"Node {node.node_id} failed to import: {resp.text}"
        )


async def _invalidate_gateway_route(request, sandbox_id) -> None:
    gateway_url = request.app.state.settings.gateway_url
    if gateway_url:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{gateway_url.rstrip('/')}/internal/routes/{sandbox_id}/invalidate",
                    headers={
                        "X-Internal-Key": request.app.state.settings.internal_api_key
                    },
                )
        except httpx.HTTPError:
            pass
    # Broadcast invalidation to EVERY gateway replica so stale route caches
    # are dropped immediately (multi-replica support). Best-effort: if Redis
    # is down the route TTL still bounds staleness.
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is not None:
        try:
            redis_client.publish(GATEWAY_ROUTE_INVALIDATE_CHANNEL, sandbox_id)
        except Exception:
            pass


@router.post(
    "/sandboxes/{sandbox_id}/migrate",
    status_code=200,
    dependencies=[Depends(require_api_key)],
)
async def migrate_sandbox(sandbox_id: str, request: Request) -> dict[str, Any]:
    """Filesystem-level migration to another healthy node.

    Without a shared workspace the sandbox directory is exported from the
    source node, imported on the target, and the record/routes/quota are
    moved. With ``E2B_SHARED_WORKSPACE_ROOT`` the directory already lives on
    shared storage visible to every node, so migration only re-provisions the
    target (runtime + volume mounts), switches the record and releases the
    source quota -- no archive transfer, and the source directory is kept.
    Running processes are not migrated: the sandbox cold-starts on the target.

    A per-sandbox migration lock (Redis ``SETNX`` marker with TTL, or an
    in-process equivalent) guarantees that concurrent ``migrate`` requests --
    even across control-plane replicas -- cannot both run: the second request
    fails with 409. The source runtime is stopped before the target is
    provisioned so no command can be served by the old node after the route
    switches, and a failed migration re-provisions the source node.
    """
    registry = _registry(request)
    token = registry.try_acquire_migration(sandbox_id)
    if token is None:
        raise OfficialError(409, f"Sandbox {sandbox_id} is already being migrated")
    settings = request.app.state.settings
    shared = bool(settings.shared_workspace_root)
    nodes = request.app.state.nodes
    record = None
    source = None
    target = None
    old_node_id = None
    source_stopped = False
    tar_path: Path | None = None
    try:
        try:
            record = registry.get(sandbox_id)
            _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        except UnknownSandboxError:
            raise OfficialError(404, f"Sandbox {sandbox_id} not found")
        source = nodes.get(record.node_id or "local")
        if source is None:
            raise OfficialError(502, f"Node {record.node_id} not found")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        target_node_id = body.get("nodeID") if isinstance(body, dict) else None
        if target_node_id == source.node_id:
            raise OfficialError(400, "Sandbox is already on this node")

        dims = {
            "memory_mb": record.memory_mb,
            "cpu_percent": record.cpu_count * 100,
            "disk_mb": record.disk_size_mb,
            "processes": record.max_processes,
        }
        if target_node_id:
            target = nodes.reserve_node(target_node_id, **dims)
            if target is None:
                raise OfficialError(
                    503,
                    f"Node {target_node_id} has no capacity or is unavailable",
                )
        else:
            volume_node_id = _migration_volume_node_id(request, record)
            if volume_node_id == source.node_id:
                raise OfficialError(
                    409, "Volume pins the sandbox to the source node"
                )
            target = nodes.select_and_reserve(
                base_image=record.base_image,
                volume_node_id=volume_node_id,
                exclude_node_id=source.node_id,
                **dims,
            )
            if target is None:
                raise OfficialError(503, "No resources available for migration")

        old_node_id = record.node_id

        # Close the dual-active window: stop the source runtime (keeping its
        # files so the workspace can still be exported) before the target is
        # touched and the route switches. On failure the source is
        # re-provisioned below so the sandbox keeps serving from its node.
        if not await _stop_source_runtime(request, record, source):
            raise OfficialError(
                502, f"Node {source.node_id} failed to stop sandbox runtime"
            )
        source_stopped = True
        try:
            if not shared:
                tar_path = await _export_sandbox_archive(request, record, source)
            # F1: re-point the record at the destination *before* the target
            # provisions the tree. The target's provision runs the sandbox's
            # ownership step through face B (worker create ->
            # ``apply_sandbox_ownership`` -> ``POST .../file-op``), and that
            # file-op is scoped by the record's node. With the source still on
            # the record, the control plane refused the very operation it had
            # just ordered (403 "belongs to node <source>, not <target>") and
            # every cross-node migration failed.
            #
            # This is the control plane's own deliberate step -- the record is
            # its own, only it may move it -- so the ordinary create/delete
            # scoping stays exactly as strict as before (a worker still cannot
            # touch a peer's sandbox: the check below is unchanged). It happens
            # after the source is stopped (its own stop/reset file ops are still
            # scoped to the source) and the rollback restores the old node
            # *before* it re-provisions the source, so that hop is scoped
            # correctly too.
            record.node_id = target.node_id
            registry.save(record)
            try:
                if not shared:
                    await _import_sandbox_archive(request, record, target, tar_path)
                if target.address == "local://":
                    _provision_local(
                        request, record, None, record.volume_mounts, settings
                    )
                else:
                    await _provision_remote(
                        request,
                        record,
                        target,
                        settings,
                        None,
                        record.volume_mounts,
                        snapshot_id=None,
                    )
                nodes.release_quota(old_node_id, **dims)
                # The source workspace is released (non-shared) or kept
                # (shared), but per-sandbox volume slices under a shared
                # volume root are still mounted by the target sandbox:
                # migration must never delete them (C1 E2.5 review).
                await _destroy_on_node(
                    request,
                    record,
                    source,
                    keep_files=shared,
                    keep_volume_slices=True,
                )
                note = f"migrated to node {target.node_id}"
                if shared:
                    note += " (shared workspace)"
                record.append_log(note)
                registry.save(record)
            except Exception:
                # Roll back the target reservation and any partial target
                # files; the source workspace itself is untouched. With a
                # shared workspace the target directory is the shared one, so
                # never delete it -- only drop a partial runtime registration.
                nodes.release_quota(target.node_id, **dims)
                if target.address == "local://":
                    _destroy_local(
                        request.app.state,
                        record,
                        keep_files=shared,
                        keep_volume_slices=True,
                    )
                else:
                    await _destroy_remote(
                        request,
                        record,
                        target,
                        keep_files=shared,
                        keep_volume_slices=True,
                    )
                raise
        finally:
            if tar_path is not None:
                tar_path.unlink(missing_ok=True)
    except Exception:
        # Migration failed: restore the source runtime stopped above so the
        # sandbox keeps serving from its original node, and undo any record
        # switch that was already persisted.
        #
        # F1: undo the record switch *first*. The re-point above moved the
        # record to the destination before provisioning, so a failed migration
        # whose recovery re-provisions the source must put the source back on
        # the record first -- otherwise the source's own ownership step would
        # be refused by the (unchanged) file-op scoping, exactly like the
        # target's was before this change.
        if (
            record is not None
            and old_node_id is not None
            and record.node_id != old_node_id
        ):
            record.node_id = old_node_id
            registry.save(record)
        if source_stopped and record is not None and source is not None:
            try:
                if source.address == "local://":
                    _provision_local(
                        request, record, None, record.volume_mounts, settings
                    )
                else:
                    await _provision_remote(
                        request,
                        record,
                        source,
                        settings,
                        None,
                        record.volume_mounts,
                        snapshot_id=None,
                    )
            except Exception:
                # Never mask the migration error itself; the source runtime
                # re-provision is best-effort recovery.
                logger.exception(
                    "failed to restore runtime for sandbox %s on node %s "
                    "after migration error",
                    sandbox_id,
                    source.node_id,
                )
        raise
    finally:
        registry.release_migration(sandbox_id, token)
    await _invalidate_gateway_route(request, sandbox_id)
    logger.info(
        "migrated sandbox %s from %s to %s",
        sandbox_id,
        source.node_id,
        target.node_id,
    )
    return {
        "sandboxID": record.sandbox_id,
        "nodeID": target.node_id,
        "state": record.state,
    }


@router.post("/sandboxes/{sandbox_id}/connect", dependencies=[Depends(require_api_key)])
async def connect_sandbox(sandbox_id: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    timeout = body.get("timeout") if isinstance(body, dict) else None
    if timeout is None:
        timeout = request.app.state.settings.default_timeout
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise OfficialError(400, "timeout must be a positive integer")
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        if record.state == "paused":
            # E9.2: a paused sandbox holds no reservation, so the SDK's
            # auto-resume has to buy capacity back before the sandbox runs
            # again (503 when the fleet is full; it stays paused).
            record.touch()
            _resume_with_capacity(request, registry, record, timeout=timeout)
            # G1a: the SDK's only public resume surface is
            # ``Sandbox.connect``, so the auto-resume must thaw the remote
            # worker too. An explicit worker error attempts a guarded
            # rollback (sandbox stays paused, client can retry) and surfaces
            # 502; transport loss and a missing node are best-effort
            # (documented caveat; the missing node is WARNINGed by the
            # helper).
            try:
                pushed = await _push_pause_state(request, record, paused=False)
            except OfficialError:
                logger.warning(
                    "worker resume push failed for sandbox %s during "
                    "connect; rolling back to paused",
                    sandbox_id,
                )
                _rollback_resume(request, registry, sandbox_id)
                raise
            if not pushed:
                request.app.state.runtime_registry.set_state(
                    sandbox_id, "running"
                )
        connected = registry.connect(sandbox_id, timeout)
        _mark_active(request, connected)
        return connected.as_sandbox()
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")


@router.post("/sandboxes/{sandbox_id}/timeout", status_code=204, dependencies=[Depends(require_api_key)])
async def set_timeout(sandbox_id: str, request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    timeout = body.get("timeout") if isinstance(body, dict) else None
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise OfficialError(400, "timeout must be a positive integer")
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        updated = registry.set_timeout(sandbox_id, timeout)
        _mark_active(request, updated)
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    except ValueError as e:
        raise OfficialError(400, str(e))
    return Response(status_code=204)


def _network_rejection_message(resp, fallback: str) -> str:
    """Stable machine-readable rejection text from the worker agent."""
    try:
        body = resp.json()
    except Exception:
        return fallback
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])
    return fallback


async def _remote_network_decision(
    request, node, record, payload
) -> tuple[bool | None, str | None, int | None]:
    """Ask the hosting node to validate + apply before the record is saved.

    Returns ``(True, None, None)`` when the node applied the update,
    ``(False, message, worker_status)`` when it explicitly rejected it with
    an HTTP status >= 400 (FUP #7): a worker 409 maps to HTTP 409 and any
    other explicit status maps to HTTP 502 at the endpoint, both raised
    **before** the record is persisted (fail closed). ``(None, None, None)``
    means no semantic decision is available (missing/unreachable node or an
    unexpected non-decision status such as a redirect) -- the caller keeps
    the pre-existing best-effort persist-and-warn behavior for those
    transport-level failures (documented caveat).
    """
    if node is None:
        logger.warning(
            "node %s not found; network update for sandbox %s not pushed",
            record.node_id,
            record.sandbox_id,
        )
        return None, None, None
    import httpx

    logger.info(
        "pushing network update for sandbox %s to node %s",
        record.sandbox_id,
        node.node_id,
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{node.address}/agent/sandboxes/{record.sandbox_id}/network",
                json=payload,
                headers={
                    "X-Internal-Key": request.app.state.settings.internal_api_key
                },
            )
    except httpx.HTTPError as e:
        logger.warning(
            "failed to push network update for sandbox %s to node %s: %s",
            record.sandbox_id,
            node.node_id,
            e,
        )
        return None, None, None
    if resp.status_code >= 400:
        logger.warning(
            "node %s rejected network update for sandbox %s: %s",
            node.node_id,
            record.sandbox_id,
            resp.text,
        )
        if resp.status_code == 409:
            return (
                False,
                _network_rejection_message(
                    resp,
                    "network update conflicts with the launched sandbox",
                ),
                resp.status_code,
            )
        return (
            False,
            _network_rejection_message(
                resp,
                f"node {node.node_id} returned HTTP {resp.status_code} "
                "for the network update",
            ),
            resp.status_code,
        )
    if resp.status_code >= 300:
        logger.warning(
            "node %s returned unexpected status %s for network update for "
            "sandbox %s: %s",
            node.node_id,
            resp.status_code,
            record.sandbox_id,
            resp.text,
        )
        return None, None, None
    return True, None, None


def _apply_local_network_update(request, record, network) -> None:
    """Atomically validate + apply a ``local://`` update on a live context.

    Runs **before** the control-plane record is saved (M4 D4 review
    Important-1): a live context's ``update_network`` performs the executor
    validate+apply in one critical section and persists the worker runtime
    state only on success. A rejection raises
    :class:`NetworkUpdateConflictError`, which the caller maps to HTTP 409 --
    no 204 and no persisted record. With no live context there is no launched
    instance and the update is applicable (it becomes the future static
    policy); the runtime-registry copy is synced after the record save.
    """
    ctx = getattr(request.app.state, "runtimes", {}).get(record.sandbox_id)
    if ctx is None:
        return
    updater = getattr(ctx, "update_network", None)
    if updater is None:
        return
    updater(network)


def _sync_local_runtime_copy(request, record) -> None:
    """Mirror the persisted record into the control-plane runtime registry."""
    runtime = request.app.state.runtime_registry.get(record.sandbox_id)
    if runtime is None:
        return
    runtime.network = dict(record.network) if record.network else None
    runtime.allow_internet_access = record.allow_internet_access
    runtime.allow_public_traffic = bool(
        (record.network or {}).get("allowPublicTraffic", False)
    )


@router.put(
    "/sandboxes/{sandbox_id}/network",
    status_code=204,
    dependencies=[Depends(require_api_key)],
)
async def update_sandbox_network(sandbox_id: str, request: Request) -> Response:
    """Replace the sandbox network egress configuration atomically.

    Mirrors the official ``Sandbox.update_network``: omitted fields are
    cleared. The change is persisted on the control plane and pushed to the
    node hosting the sandbox; the next command uses the new policy. D4=A:
    on an already-launched instance only expressible monotone narrowings are
    accepted -- a worker conflict is an HTTP 409 and any other explicit
    worker rejection (404/5xx) is an HTTP 502, both raised **before** the
    registry record is persisted (the worker runtime copy is validated
    before it is mutated), so an explicit rejection changes neither side.
    Only transport loss or a missing node keeps the pre-existing
    best-effort persist-and-warn behavior (documented caveat).
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise OfficialError(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise OfficialError(400, "Request body must be a JSON object")
    try:
        update = normalize_network_update(body)
    except NetworkConfigError as e:
        raise OfficialError(400, str(e))
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")

    # Atomic replace (pure, computed before any record mutation): overlay the
    # update on the current config; fields the update omits are cleared.
    # ``allow_public_traffic`` is not updatable through this endpoint
    # (official API keeps it create-only).
    network = dict(record.network or {})
    for field in (
        "allowOut",
        "denyOut",
        "rules",
        "allowInternetAccess",
        "egressProxy",
    ):
        if field in update:
            network[field] = update[field]
        else:
            network.pop(field, None)
    proposed_internet = record.allow_internet_access
    if "allowInternetAccess" in update:
        proposed_internet = update["allowInternetAccess"]
    allow_public_traffic = bool(network.get("allowPublicTraffic", False))
    node = request.app.state.nodes.get(record.node_id or "local")
    payload = {
        "network": network or None,
        "allowInternetAccess": proposed_internet,
        "allowPublicTraffic": allow_public_traffic,
    }

    if node is not None and node.address != "local://":
        accepted, rejection, worker_status = await _remote_network_decision(
            request, node, record, payload
        )
        if accepted is False:
            # FUP #7: every explicit worker rejection is a decision and must
            # fail closed before the record is persisted. A worker 409 is
            # the client's conflict to fix (HTTP 409); any other explicit
            # status means the node cannot take the update (HTTP 502).
            if worker_status == 409:
                raise OfficialError(
                    409, rejection or "network update conflicts"
                )
            raise OfficialError(
                502,
                rejection
                or f"Node {node.node_id} rejected network update for sandbox "
                f"{record.sandbox_id}",
            )
    elif node is not None:
        try:
            _apply_local_network_update(request, record, network or None)
        except NetworkUpdateConflictError as exc:
            raise OfficialError(409, str(exc)) from exc
    else:
        logger.warning(
            "node %s not found; network update for sandbox %s not pushed",
            record.node_id,
            record.sandbox_id,
        )

    # Only expressible/no-instance updates reach the persistence below.
    if "allowInternetAccess" in update:
        record.allow_internet_access = update["allowInternetAccess"]
    record.network = network or None
    record.touch()  # E9.1: a user mutation, persisted by the save below
    registry.save(record)
    if node is not None and node.address == "local://":
        _sync_local_runtime_copy(request, record)
    return Response(status_code=204)


@router.post(
    "/sandboxes/{sandbox_id}/pause",
    status_code=204,
    dependencies=[Depends(require_api_key)],
)
async def pause_sandbox(sandbox_id: str, request: Request) -> Response:
    """Pause a sandbox: release its reservation and freeze its runtime.

    E9.2 keeps the ledger semantics unchanged; G1a adds worker delivery.
    A ``local://`` sandbox freezes through the shared runtime-registry state
    callback; a remote sandbox is paused by pushing to its hosting agent.
    The push's 404 (no live runtime to freeze) is a success for state
    bookkeeping, transport loss is best-effort (WARNING, documented caveat),
    and any other explicit worker error rolls back the local state change
    and surfaces HTTP 502.

    No new-command gating: pause freezes the currently running command
    groups; it does not gate future execs (parity with the combined
    deployment).
    """
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        record.touch()  # E9.1: a user action
        registry.pause(record)  # E9.2: releases the global/tenant reservation
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    except SandboxStateConflictError:
        raise OfficialError(409, "Sandbox is already paused")
    _park_capacity(request, record)
    try:
        pushed = await _push_pause_state(request, record, paused=True)
    except OfficialError:
        logger.warning(
            "worker pause push failed for sandbox %s; rolling back "
            "local state",
            sandbox_id,
        )
        _rollback_pause(request, registry, sandbox_id)
        raise
    if not pushed:
        # Combined deployment (or a node the registry no longer knows, which
        # the helper WARNINGed): the shared runtime-registry state callback
        # freezes the in-process context; a missing remote node has no agent
        # to reach.
        request.app.state.runtime_registry.set_state(sandbox_id, "paused")
    return Response(status_code=204)


@router.post(
    "/sandboxes/{sandbox_id}/resume",
    status_code=204,
    dependencies=[Depends(require_api_key)],
)
async def resume_sandbox(sandbox_id: str, request: Request) -> Response:
    """Resume a paused sandbox: re-book capacity and thaw its runtime.

    E9.2 keeps the admission semantics unchanged; G1a adds worker delivery.
    A ``local://`` sandbox thaws through the shared runtime-registry state
    callback; a remote sandbox is resumed by pushing to its hosting agent.
    The push's 404 (no live runtime to thaw) is a success, transport loss is
    best-effort (WARNING, documented caveat), and any other explicit worker
    error rolls the reservation/state back (sandbox stays paused) and
    surfaces HTTP 502 so the client can retry.
    """
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        record.touch()  # E9.1: a user action
        _resume_with_capacity(request, registry, record)  # E9.2
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    try:
        pushed = await _push_pause_state(request, record, paused=False)
    except OfficialError:
        logger.warning(
            "worker resume push failed for sandbox %s; rolling back "
            "to paused",
            sandbox_id,
        )
        _rollback_resume(request, registry, sandbox_id)
        raise
    if not pushed:
        # Combined deployment (or a node the registry no longer knows, which
        # the helper WARNINGed): the shared runtime-registry state callback
        # thaws the in-process context; a missing remote node has no agent
        # to reach.
        request.app.state.runtime_registry.set_state(sandbox_id, "running")
    return Response(status_code=204)


@router.get(
    "/sandboxes/{sandbox_id}/metrics",
    dependencies=[Depends(require_api_key)],
)
async def get_sandbox_metrics(
    sandbox_id: str,
    request: Request,
    start: int | None = Query(default=None),
    end: int | None = Query(default=None),
) -> list[dict[str, Any]]:
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    sample = record.sample_metric()
    now = int(__import__("time").time())
    if start is not None and sample["timestampUnix"] < start:
        return []
    if end is not None and sample["timestampUnix"] > end:
        return []
    return [sample]


@router.get(
    "/sandboxes/{sandbox_id}/logs",
    dependencies=[Depends(require_api_key)],
)
async def get_sandbox_logs(
    sandbox_id: str,
    request: Request,
    start: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, str]]:
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    logs = record.logs + await _command_logs(request, record)
    platform_pause = _platform_pause_entry(record)
    if platform_pause is not None:
        logs.append(platform_pause)
    logs.sort(key=_log_ts)
    if start is not None:
        logs = [log for log in logs if _log_ts(log) >= start]
    return logs[-limit:]


@router.get(
    "/sandboxes/{sandbox_id}/checkpoint",
    dependencies=[Depends(require_api_key)],
)
async def get_sandbox_checkpoint(
    sandbox_id: str, request: Request
) -> dict[str, Any]:
    """只读：这个沙箱的 checkpoint 图与最近一次恢复（E3）。

    The image and the last restore are facts about what a *sandbox node* did --
    the worker's own directory holds them (see
    :mod:`envd_service.runtime.checkpoint_store`), and this endpoint asks that
    node. The alternative (a ``checkpoint`` field on the sandbox record) was
    refused: the record would then be a second source of truth about a process
    the control plane never saw, and every field there has to survive the
    storage round trip. Deliberately **read-only**: ``pause``/``resume`` keep
    answering 204 and nothing here writes anything.

    Answers 404 for a sandbox this control plane does not know (or that belongs
    to another tenant, same as every other read). For a known sandbox it always
    answers 200: a node that cannot be reached adds ``unreachable: true`` to the
    "no image" shape rather than turning a diagnostic question into a new kind
    of failure, and a node whose worker answers something else is treated the
    same way.
    """
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is not None and node.address != "local://":
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{node.address}/agent/sandboxes/{sandbox_id}/checkpoint",
                    headers={
                        "X-Internal-Key": request.app.state.settings.internal_api_key
                    },
                )
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and "hasImage" in payload:
                    return payload
        except (httpx.HTTPError, ValueError):
            pass
        return {
            "sandboxID": sandbox_id,
            "hasImage": False,
            "imageMB": 0,
            "capturedAt": None,
            "lastRestore": None,
            "unreachable": True,
        }
    # ``local://`` (or a node this control plane has no agent for): the shared
    # worker is in this process, so the answer is read where the worker would
    # have read it -- from the platform's own base, never from the record's tree
    # (compare ``_command_logs``).
    from envd_service.runtime.checkpoint_store import checkpoint_status

    return checkpoint_status(
        request.app.state.workspace_base,
        sandbox_id,
        state_base=request.app.state.state_base,
    )


@router.get(
    "/v2/sandboxes/{sandbox_id}/logs",
    dependencies=[Depends(require_api_key)],
)
async def get_sandbox_logs_v2(
    sandbox_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, str]]:
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    logs = record.logs + await _command_logs(request, record)
    platform_pause = _platform_pause_entry(record)
    if platform_pause is not None:
        logs.append(platform_pause)
    logs.sort(key=_log_ts)
    return logs[-limit:]
