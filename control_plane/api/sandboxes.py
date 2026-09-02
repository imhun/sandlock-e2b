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
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from control_plane.api.errors import OfficialError
from control_plane.auth import (
    _require_owned,
    _require_related,
    require_api_key,
    tenant_of,
    tenant_scope,
)
from control_plane.registry.manager import (
    PRIORITY_DEFAULT,
    PRIORITY_MAX,
    PRIORITY_MIN,
    ResourceUnavailableError,
    SandboxRecord,
    SandboxStateConflictError,
    SandboxRegistry,
    UnknownSandboxError,
)
from control_plane.registry.secrets import SecretTenantMismatchError
from control_plane.registry.snapshots import UnknownSnapshotError
from control_plane.registry.templates import UnknownTemplateBuildError
from gateway_common.network import (
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
from gateway_common.paths import validate_sandbox_id

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
    try:
        import sandlock  # noqa: F401

        return sandlock.landlock_abi_version() >= 6
    except Exception:
        return False


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
            # retry paths) can treat a full node and a full fleet alike.
            raise OfficialError(503, "No resources available")
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
    log_path = Path(workspace) / "command-logs.jsonl"
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
    ``recent_failures.record()`` exactly like before.
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
                await _destroy_evicted(request, result.record)
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
        raise _CapacityExhausted("No resources available")

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
    # chowned to it. Only a root worker maps arbitrary host uids (S1.2);
    # otherwise the fixed-uid + Landlock model applies.
    host_uid = None
    pool = getattr(request.app.state.runtime_registry, "uid_pool", None)
    if pool is not None and os.geteuid() == 0:
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
                via_agent=False,
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
async def kill_sandbox(sandbox_id: str, request: Request) -> Response:
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        registry.delete(sandbox_id)
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is not None and node.address != "local://":
        await _destroy_remote(request, record, node)
    else:
        _destroy_local(request.app.state, record)
    return Response(status_code=204)


async def _destroy_remote(
    request,
    record,
    node,
    keep_files: bool = False,
    keep_volume_slices: bool = False,
) -> None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{node.address}/agent/sandboxes/{record.sandbox_id}"
            params = []
            if keep_files:
                params.append("keepFiles=true")
            if keep_volume_slices:
                params.append("keepVolumeSlices=true")
            if params:
                url += "?" + "&".join(params)
            await client.delete(
                url,
                headers={
                    "X-Internal-Key": request.app.state.settings.internal_api_key
                },
            )
    except httpx.HTTPError:
        pass


def _destroy_local(
    state, record, keep_files: bool = False, keep_volume_slices: bool = False
) -> None:
    """Stop and clean a local sandbox runtime, including volume slices.

    ``keep_files=True`` keeps the workspace and volume slices (migration
    source stop / shared-workspace teardown). ``keep_volume_slices=True``
    keeps only the per-sandbox volume slices while still removing the
    workspace — used by migration success and rollback, where the shared
    volume slice is already in use by the target sandbox and must survive.
    """
    runtime_registry = getattr(state, "runtime_registry", None)
    runtime = (
        runtime_registry.get(record.sandbox_id)
        if runtime_registry is not None
        else None
    )
    if not keep_files:
        if (
            not keep_volume_slices
            and runtime is not None
            and runtime.volume_projects
        ):
            try:
                from envd_service.volumes import cleanup_volume_projects
            except ImportError:  # pragma: no cover - separated control plane
                pass
            else:
                cleanup_volume_projects(
                    volume_projects=runtime.volume_projects,
                    fallback_mount_point=state.workspace_base,
                    via_agent=False,
                )
        shutil.rmtree(
            state.workspace_base / record.sandbox_id, ignore_errors=True
        )
    if runtime_registry is not None:
        runtime_registry.unregister(record.sandbox_id)


async def _destroy_on_node(
    request,
    record,
    node,
    keep_files: bool = False,
    keep_volume_slices: bool = False,
) -> None:
    if node.address == "local://":
        _destroy_local(
            request.app.state,
            record,
            keep_files=keep_files,
            keep_volume_slices=keep_volume_slices,
        )
        return
    await _destroy_remote(
        request,
        record,
        node,
        keep_files=keep_files,
        keep_volume_slices=keep_volume_slices,
    )


async def _destroy_evicted(request, record) -> None:
    """Tear down the runtime of an eviction-killed sandbox (E9.3).

    The registry already removed the record (and released its admission/node
    quota through the normal delete chain); this mirrors ``kill_sandbox``'s
    teardown so the worker actually stops the runtime and drops its files.
    """
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is not None and node.address != "local://":
        await _destroy_remote(request, record, node)
    else:
        _destroy_local(request.app.state, record)


async def _stop_source_runtime(request, record, node) -> bool:
    """Stop the sandbox runtime on the source node, keeping its files.

    Unregistering the runtime kills the process tree, closing the dual-active
    window: once the gateway route switches, no command can still be served
    by the old node. Returns ``False`` when the node did not acknowledge the
    stop, in which case the caller must abort the migration.
    """
    if node.address == "local://":
        _destroy_local(request.app.state, record, keep_files=True)
        return True
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
                record.node_id = target.node_id
                registry.save(record)
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
        if (
            record is not None
            and old_node_id is not None
            and record.node_id != old_node_id
        ):
            record.node_id = old_node_id
            registry.save(record)
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
            request.app.state.runtime_registry.set_state(sandbox_id, "running")
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


async def _push_network_config(request, record) -> None:
    """Apply a persisted network update on the node hosting the sandbox."""
    node = request.app.state.nodes.get(record.node_id or "local")
    if node is None:
        logger.warning(
            "node %s not found; network update for sandbox %s not pushed",
            record.node_id,
            record.sandbox_id,
        )
        return
    allow_public_traffic = bool(
        (record.network or {}).get("allowPublicTraffic", False)
    )
    if node.address == "local://":
        runtime = request.app.state.runtime_registry.get(record.sandbox_id)
        if runtime is None:
            return
        runtime.network = dict(record.network) if record.network else None
        runtime.allow_internet_access = record.allow_internet_access
        runtime.allow_public_traffic = allow_public_traffic
        # If this process also hosts a live runtime context, update it; the
        # envd side additionally applies drift when records are shared.
        ctx = getattr(request.app.state, "runtimes", {}).get(record.sandbox_id)
        if ctx is not None and hasattr(ctx, "update_network"):
            ctx.update_network(record.network)
        return
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
                json={
                    "network": record.network,
                    "allowInternetAccess": record.allow_internet_access,
                    "allowPublicTraffic": allow_public_traffic,
                },
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
        return
    if resp.status_code >= 300:
        logger.warning(
            "node %s rejected network update for sandbox %s: %s",
            node.node_id,
            record.sandbox_id,
            resp.text,
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
    node hosting the sandbox; the next command uses the new policy.
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

    # Atomic replace: overlay the update on the current config; fields the
    # update omits are cleared. ``allow_public_traffic`` is not updatable
    # through this endpoint (official API keeps it create-only).
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
    if "allowInternetAccess" in update:
        record.allow_internet_access = update["allowInternetAccess"]
    record.network = network or None
    record.touch()  # E9.1: a user mutation, persisted by the save below
    registry.save(record)
    await _push_network_config(request, record)
    return Response(status_code=204)


@router.post(
    "/sandboxes/{sandbox_id}/pause",
    status_code=204,
    dependencies=[Depends(require_api_key)],
)
async def pause_sandbox(sandbox_id: str, request: Request) -> Response:
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
    request.app.state.runtime_registry.set_state(sandbox_id, "paused")
    return Response(status_code=204)


@router.post(
    "/sandboxes/{sandbox_id}/resume",
    status_code=204,
    dependencies=[Depends(require_api_key)],
)
async def resume_sandbox(sandbox_id: str, request: Request) -> Response:
    registry = _registry(request)
    try:
        record = registry.get(sandbox_id)
        _require_owned(request, record, resource_id=sandbox_id, label="Sandbox")
        record.touch()  # E9.1: a user action
        _resume_with_capacity(request, registry, record)  # E9.2
    except UnknownSandboxError:
        raise OfficialError(404, f"Sandbox {sandbox_id} not found")
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
    logs.sort(key=_log_ts)
    if start is not None:
        logs = [log for log in logs if _log_ts(log) >= start]
    return logs[-limit:]


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
    logs.sort(key=_log_ts)
    return logs[-limit:]
