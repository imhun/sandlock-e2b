"""Worker agent: node registration, heartbeat and sandbox lifecycle API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from envd_service.config import Settings
from envd_service.runtime.image_resolver import (
    peek_image_warm,
    resolve_image_rootfs,
)
from envd_service.uid_pool import apply_sandbox_ownership
from envd_service.xfs_quota import (
    ProjectQuotaError,
    provision_project,
    release_project,
    xfs_project_supported,
)
from envd_service.volumes import build_volume_mounts, cleanup_volume_projects

logger = logging.getLogger(__name__)

router = APIRouter()


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


def _require_internal_key(request: Request, settings: Settings) -> None:
    key = request.headers.get("X-Internal-Key")
    # E3.6: accept any key in the rotation window list (falls back to the
    # single E2B_INTERNAL_API_KEY when the list is empty).
    if key is None or not any(
        secrets.compare_digest(key, candidate)
        for candidate in settings.all_internal_api_keys
    ):
        raise PermissionError("Unauthorized")


def _node_resources(settings: Settings) -> dict[str, int]:
    """Report node capacity: explicit env overrides, else host probing."""
    memory_mb = int(os.getenv("E2B_NODE_MEMORY_MB", "0"))
    if memory_mb <= 0:
        try:
            memory_mb = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
        except (ValueError, OSError):
            memory_mb = settings.default_memory_mb * 100
    cpu = int(os.getenv("E2B_NODE_CPU_PERCENT", "0"))
    if cpu <= 0:
        cpu = os.cpu_count() * 100 or 100
    disk = int(os.getenv("E2B_NODE_DISK_MB", "0"))
    if disk <= 0:
        try:
            disk = shutil.disk_usage(settings.workspace_base).total // (1024 * 1024)
        except OSError:
            disk = settings.default_disk_mb * 100
    processes = int(os.getenv("E2B_NODE_PROCESSES", "0"))
    if processes <= 0:
        processes = settings.default_max_processes * 100
    return {
        "totalMemoryMB": memory_mb,
        "totalCPUPercent": cpu,
        "totalDiskMB": disk,
        "totalProcesses": processes,
    }


def _node_type() -> str:
    if os.path.exists("/.dockerenv"):
        return "container"
    return "physical"


def _register_payload(settings: Settings) -> dict[str, Any]:
    return {
        "nodeID": os.getenv("E2B_NODE_ID"),
        "address": os.getenv("E2B_NODE_ADDRESS"),
        "images": [i for i in (settings.base_image,) if i],
        "labels": {
            "node-type": os.getenv("E2B_NODE_TYPE") or _node_type(),
        },
        **_node_resources(settings),
    }


def _heartbeat_usage_payload(
    settings: Settings,
    metrics_provider: Callable[[], dict[str, Any]] | None = None,
    activity_provider: Callable[[], dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Disk usage + quota alert snapshot carried by each worker heartbeat."""
    payload: dict[str, Any] = {}
    try:
        usage = shutil.disk_usage(settings.workspace_base)
        payload["diskUsedMB"] = usage.used // (1024 * 1024)
        payload["diskTotalMB"] = usage.total // (1024 * 1024)
    except OSError:
        pass
    if activity_provider is not None:
        try:
            # E9.1: per-sandbox last-activity timestamps; the control plane
            # turns them into ``last_active_at`` for idle detection.
            activity = activity_provider()
        except Exception:
            logger.warning("sandbox activity provider failed", exc_info=True)
            activity = None
        if isinstance(activity, dict) and activity:
            payload["sandboxActivity"] = activity
    if metrics_provider is not None:
        try:
            metrics = metrics_provider()
        except Exception:
            logger.warning("quota metrics provider failed", exc_info=True)
            return payload
        if isinstance(metrics, dict):
            for key in (
                "quotaOverLimit",
                "quotaNearLimit",
                "quotaOverLimitCount",
                "quotaNearLimitCount",
                "diskWarnCount",
                "diskErrorCount",
            ):
                if key in metrics:
                    payload[key] = metrics[key]
    return payload


def _delete_sandbox_runtime(
    settings: Settings,
    runtime_registry,
    sandbox_id: str,
    *,
    keep_files: bool = False,
    keep_volume_slices: bool = False,
) -> None:
    """Full local teardown for one sandbox runtime (shared by the delete
    endpoint and E6.1 orphan reconciliation)."""
    record = runtime_registry.get(sandbox_id)
    project_id = record.project_id if record is not None else None
    volume_projects = record.volume_projects if record is not None else []
    # Derive the project dir from the record so release and rmtree always
    # target the directory the sandbox was registered with; fall back to
    # the workspace_base/id convention for unregistered sandboxes.
    workspace_dir = (
        Path(record.workspace_dir)
        if record is not None
        else settings.workspace_base / sandbox_id
    )
    runtime_registry.unregister(sandbox_id)
    # Shared-workspace deployments keep the directory (keep_files=true): the
    # same storage hosts the sandbox on every node, so removing it would
    # destroy the live sandbox's files, and its project id must stay until
    # the sandbox is really deleted.
    # keep_volume_slices=true is the migration counterpart: the workspace may
    # be removed (non-shared workspace export finished), but per-sandbox
    # volume slices under a shared volume root are still in use by the
    # target node and must never be deleted by a migration stop/rollback.
    if keep_files:
        return
    if project_id is not None:
        try:
            release_project(
                project_dir=workspace_dir,
                mount_point=settings.workspace_base,
                projid=project_id,
                via_agent=settings.quota_via_agent,
            )
        except ProjectQuotaError as exc:
            logger.warning(
                "XFS project quota cleanup failed for %s: %s",
                sandbox_id,
                exc,
            )
    if not keep_volume_slices:
        cleanup_volume_projects(
            volume_projects=volume_projects,
            fallback_mount_point=settings.workspace_base,
            via_agent=settings.quota_via_agent,
        )
    shutil.rmtree(workspace_dir, ignore_errors=True)


class NodeAgent:
    """Periodically registers with the control plane and sends heartbeats."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_registry,
        control_plane_url: str | None,
        node_address: str | None,
        metrics_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._settings = settings
        self._runtime_registry = runtime_registry
        self._control_url = (control_plane_url or "").rstrip("/")
        self._node_address = node_address or ""
        self._metrics_provider = metrics_provider
        #: E9.1: per-sandbox activity to ship with each heartbeat (registries
        #: without activity tracking simply report nothing).
        self._activity_provider = getattr(
            runtime_registry, "activity_snapshot", None
        )
        self._node_id: str | None = None
        self._task: asyncio.Task | None = None
        # E6.1: set when the control plane may have missed this worker (first
        # start, heartbeat failures). The next successful heartbeat after a
        # registration runs a local-runtime reconciliation.
        self._reconcile_pending = True

    def start(self) -> None:
        if not self._control_url or not self._node_address:
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        headers = {"X-Internal-Key": self._settings.internal_api_key}
        while True:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    payload = _register_payload(self._settings)
                    payload["address"] = self._node_address
                    if self._node_id is None:
                        resp = await client.post(
                            f"{self._control_url}/internal/nodes/register",
                            json=payload,
                            headers=headers,
                        )
                        if resp.status_code == 200:
                            self._node_id = resp.json().get("nodeID")
                            logger.info(
                                "registered node %s at %s", self._node_id, self._node_address
                            )
                            await self._reconcile_with_control_plane(client, headers)
                    else:
                        resp = await client.post(
                            f"{self._control_url}/internal/nodes/{self._node_id}/heartbeat",
                            json=_heartbeat_usage_payload(
                                self._settings,
                                self._metrics_provider,
                                self._activity_provider,
                            ),
                            headers=headers,
                        )
                        if resp.status_code == 404:
                            # The control plane lost us (e.g. it restarted);
                            # re-register on the next cycle.
                            self._node_id = None
                        elif self._reconcile_pending:
                            # A previous heartbeat/registration failed (e.g.
                            # a network partition): the control plane may
                            # have orphaned our sandboxes, so reconcile now
                            # that we can reach it again.
                            self._reconcile_pending = False
                            await self._reconcile_with_control_plane(client, headers)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._reconcile_pending = True
                logger.warning("node agent heartbeat failed", exc_info=True)
            await asyncio.sleep(5)

    async def _reconcile_with_control_plane(self, client, headers) -> None:
        """Reconcile this worker's local runtimes against the control plane
        after (re)registration (E6.1 recovery path).

        During a partition the control plane marks this node's sandbox
        records ``orphaned`` instead of deleting their workspaces. When the
        worker reconnects:

        * local runtimes the control plane no longer knows are torn down
          here (their records were deleted while we were unreachable) — but
          only runtimes that already existed when the control-plane snapshot
          was requested. A runtime registered *during* the reconcile window
          is a concurrent create that raced the snapshot; it is kept and
          reported back so the control plane never deletes its record;
        * the remaining local ids are reported back together with the
          snapshot ids, so the control plane un-orphans the records it
          still has, removes records for sandboxes we no longer run (only
          ones that were in the snapshot), and leaves records created after
          the snapshot untouched.
        """
        if not self._control_url or not self._node_id:
            return
        # Wall-clock boundary captured *before* the snapshot request. Any
        # local runtime registered after this point cannot have been in the
        # control-plane snapshot, so it must be a concurrent create.
        reconcile_started_at = time.time()
        try:
            resp = await client.get(
                f"{self._control_url}/internal/nodes/{self._node_id}/sandboxes",
                headers=headers,
            )
            resp.raise_for_status()
            known = set(resp.json().get("sandboxIDs") or [])
        except (httpx.HTTPError, ValueError):
            logger.warning(
                "reconcile: cannot fetch control-plane sandbox list",
                exc_info=True,
            )
            return
        local = {r.sandbox_id: r for r in self._runtime_registry.list()}
        concurrent_creates = {
            sandbox_id
            for sandbox_id, record in local.items()
            if record.created_at > reconcile_started_at
        }
        orphaned = set(local) - known - concurrent_creates
        for sandbox_id in sorted(orphaned):
            logger.warning(
                "reconcile: removing orphan runtime %s (not in control plane)",
                sandbox_id,
            )
            _delete_sandbox_runtime(self._settings, self._runtime_registry, sandbox_id)
        remaining = (set(local) & known) | concurrent_creates
        try:
            resp = await client.post(
                f"{self._control_url}/internal/nodes/{self._node_id}/reconcile",
                json={
                    "sandboxIDs": sorted(remaining),
                    "snapshotIDs": sorted(known),
                },
                headers=headers,
            )
            resp.raise_for_status()
            result = resp.json()
            if concurrent_creates:
                logger.info(
                    "reconcile: kept %d concurrent create(s) started during "
                    "reconcile window: %s",
                    len(concurrent_creates),
                    ",".join(sorted(concurrent_creates)),
                )
            if result.get("recovered"):
                logger.info(
                    "reconcile: restored sandboxes %s",
                    ",".join(result["recovered"]),
                )
            if result.get("removed"):
                logger.info(
                    "reconcile: removed stale records %s",
                    ",".join(result["removed"]),
                )
        except (httpx.HTTPError, ValueError):
            logger.warning(
                "reconcile: control-plane record update failed",
                exc_info=True,
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def _agent_create_sandbox(request: Request, settings: Settings, payload: dict) -> None:
    runtime_registry = request.app.state.runtime_registry
    workspace_base = settings.workspace_base
    sandbox_id = payload.get("sandboxID")
    if not sandbox_id:
        raise ValueError("sandboxID is required")
    workspace_dir = workspace_base / sandbox_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    snapshot_id = payload.get("snapshotID")
    if snapshot_id:
        snapshot_fs = workspace_base / "_snapshots" / snapshot_id / "fs"
        if not snapshot_fs.is_dir():
            raise ValueError(f"Snapshot {snapshot_id} not found on this node")
        shutil.copytree(snapshot_fs, workspace_dir, dirs_exist_ok=True, symlinks=True)
    else:
        (workspace_dir / "workspace").mkdir(parents=True, exist_ok=True)
    volume_mounts = payload.get("volumeMounts") or []
    existing = runtime_registry.get(sandbox_id)
    # E3.2: allocate the sandbox's host uid before materializing volumes so
    # per-sandbox volume slices can be chowned to it. Only a root worker can
    # map arbitrary host uids (S1.2); non-root workers keep the fixed-uid +
    # Landlock model and never allocate.
    host_uid = None
    pool = getattr(runtime_registry, "uid_pool", None)
    if settings.per_sandbox_uid and os.geteuid() == 0 and pool is not None:
        host_uid = pool.acquire(
            sandbox_id,
            preferred=existing.host_uid if existing is not None else None,
        )
    try:
        mount_paths, volume_projects = build_volume_mounts(
            sandbox_id=sandbox_id,
            volume_mounts=volume_mounts,
            shared_volume_root=settings.shared_volume_root,
            workspace_dir=workspace_dir,
            fallback_mount_point=settings.workspace_base,
            via_agent=settings.quota_via_agent,
            existing_volume_projects=(
                existing.volume_projects if existing is not None else []
            ),
            host_uid=host_uid,
        )
        disk_mb = int(payload.get("diskMB", settings.default_disk_mb))
        project_id = None
        if existing is not None:
            project_id = existing.project_id
        if xfs_project_supported(
            workspace_base, via_agent=settings.quota_via_agent
        )[0]:
            try:
                project_id = provision_project(
                    sandbox_id=sandbox_id,
                    project_dir=workspace_dir,
                    mount_point=workspace_base,
                    disk_mb=disk_mb,
                    via_agent=settings.quota_via_agent,
                    project_id=project_id,
                )
            except ProjectQuotaError as exc:
                logger.warning(
                    "XFS project quota setup failed for %s: %s",
                    sandbox_id,
                    exc,
                )
                # A failed provision may have cleared the project state
                # (project -C after a half-created setup), so the sandbox
                # must not keep a stale projid: persisting it would claim
                # quota is active while the files no longer belong to any
                # project, and delete would only leave an orphan quota table
                # entry.
                project_id = None
        if host_uid is not None:
            apply_sandbox_ownership(workspace_dir, host_uid)
        runtime_registry.register(
            sandbox_id=sandbox_id,
            access_token=payload.get("accessToken", ""),
            workspace_dir=str(workspace_dir),
            env_vars=dict(payload.get("envVars") or {}),
            base_image=payload.get("baseImage"),
            host_uid=host_uid,
            memory_mb=int(
                payload.get("memoryMB", settings.default_memory_mb)
            ),
            cpu_percent=int(
                payload.get("cpuPercent", settings.default_cpu_percent)
            ),
            disk_mb=disk_mb,
            project_id=project_id,
            max_processes=int(
                payload.get("maxProcesses", settings.default_max_processes)
            ),
            allow_internet_access=bool(
                payload.get("allowInternetAccess", False)
            ),
            max_command_timeout=int(
                payload.get("maxCommandTimeout", 3600)
            ),
            mcp=payload.get("mcp"),
            network=payload.get("network"),
            allow_public_traffic=bool(
                payload.get("allowPublicTraffic", False)
            ),
            volume_mounts=mount_paths,
            volume_projects=volume_projects,
            iam_tokens=payload.get("iamTokens"),
        )
    except BaseException:
        # I3: any failure between acquire and register (invalid volume
        # mounts -> 400, quota/ownership errors -> 500) must return the
        # reserved uid to the pool instead of leaking a slot.
        if host_uid is not None and pool is not None:
            pool.release(sandbox_id)
        raise
    # I1: the record is durable — drop the cross-process reservation marker
    # so other workers can reuse the free set without seeing a stale hold.
    if host_uid is not None and pool is not None:
        pool.commit(sandbox_id)


@router.post("/agent/sandboxes", status_code=201)
async def agent_create_sandbox(request: Request) -> Response:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
        payload = await request.json()
        _agent_create_sandbox(request, settings, payload)
    except PermissionError:
        return Response(status_code=401)
    except (ValueError, json.JSONDecodeError) as e:
        return Response(status_code=400, content=str(e))
    except Exception:
        logger.exception("agent create sandbox failed")
        return Response(status_code=500)
    return Response(status_code=201)


@router.delete("/agent/sandboxes/{sandbox_id}", status_code=204)
async def agent_delete_sandbox(
    sandbox_id: str,
    request: Request,
    keepFiles: bool = Query(default=False),
    keepVolumeSlices: bool = Query(default=False),
) -> Response:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    runtime_registry = request.app.state.runtime_registry
    _delete_sandbox_runtime(
        settings,
        runtime_registry,
        sandbox_id,
        keep_files=keepFiles,
        keep_volume_slices=keepVolumeSlices,
    )
    return Response(status_code=204)


@router.get("/agent/images/{image:path}/warm")
async def agent_image_warm_peek(image: str, request: Request) -> Response:
    """Return ``{cached, digest}`` for a base image without extracting it.

    Used by the control plane to decide fast path (image cached -> create
    directly) vs slow path (image cold -> require ``X-Sandbox-Id`` and warm
    before creating the sandbox record).
    """
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    if not _executor_needs_images(settings.executor):
        return JSONResponse({"cached": True, "required": False, "digest": None})
    return JSONResponse(
        peek_image_warm(
            image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
        )
    )


@router.post("/agent/images/{image:path}/warm")
async def agent_image_warm_now(image: str, request: Request) -> Response:
    """Ensure a base image rootfs is extracted (idempotent, may take long)."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    if not _executor_needs_images(settings.executor):
        return JSONResponse({"cached": True, "required": False, "digest": None})
    try:
        rootfs = await asyncio.to_thread(
            resolve_image_rootfs,
            image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
        )
    except Exception as e:
        logger.warning("agent warm failed for %s: %s", image, e)
        return Response(status_code=500, content=str(e)[:500])
    return JSONResponse({"cached": True, "rootfs": str(rootfs)})


def _executor_needs_images(mode: str) -> bool:
    """Whether this worker's executor resolves image rootfs at all."""
    if mode == "local":
        return False
    if mode == "sandlock":
        return True
    try:
        import sandlock  # noqa: F401

        # auto: images only when the sandlock executor will actually run
        # (factory falls back to local when Landlock ABI < 6).
        return sandlock.landlock_abi_version() >= 6
    except Exception:
        return False


@router.post("/agent/sandboxes/{sandbox_id}/network", status_code=204)
async def agent_update_sandbox_network(
    sandbox_id: str,
    request: Request,
) -> Response:
    """Apply a control-plane network update to a live sandbox runtime."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
        payload = await request.json()
    except PermissionError:
        return Response(status_code=401)
    except json.JSONDecodeError:
        return Response(status_code=400, content="Invalid JSON body")
    runtime = request.app.state.runtime_registry.get(sandbox_id)
    if runtime is None:
        return Response(status_code=404)
    if not isinstance(payload, dict):
        return Response(status_code=400, content="Request body must be an object")
    network = payload.get("network")
    if network is not None and not isinstance(network, dict):
        return Response(status_code=400, content="network must be an object")
    runtime.network = dict(network) if network else None
    allow_internet = payload.get("allowInternetAccess")
    if isinstance(allow_internet, bool):
        runtime.allow_internet_access = allow_internet
    allow_public = payload.get("allowPublicTraffic")
    if isinstance(allow_public, bool):
        runtime.allow_public_traffic = allow_public
    ctx = request.app.state.runtimes.get(sandbox_id)
    if ctx is not None and hasattr(ctx, "update_network"):
        ctx.update_network(runtime.network)
    logger.info(
        "agent network update for sandbox %s: %s",
        sandbox_id,
        runtime.network,
    )
    return Response(status_code=204)


@router.get("/agent/sandboxes/{sandbox_id}/export")
async def agent_export_sandbox(sandbox_id: str, request: Request) -> Response:
    """Stream a tar.gz of the sandbox workspace for filesystem migration."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    workspace = settings.workspace_base / sandbox_id
    if not workspace.is_dir():
        return Response(status_code=404)
    migrate_dir = settings.workspace_base / "_migrate"
    migrate_dir.mkdir(parents=True, exist_ok=True)
    tar_path = migrate_dir / f"{sandbox_id}.tar.gz"
    try:
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(workspace, arcname=".", recursive=True)
    except OSError:
        return Response(status_code=500)

    def _stream():
        try:
            with open(tar_path, "rb") as f:
                while chunk := f.read(64 * 1024):
                    yield chunk
        finally:
            tar_path.unlink(missing_ok=True)

    return StreamingResponse(_stream(), media_type="application/gzip")


@router.post("/agent/sandboxes/{sandbox_id}/import", status_code=204)
async def agent_import_sandbox(sandbox_id: str, request: Request) -> Response:
    """Restore a sandbox workspace from a raw tar.gz body."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    body = await request.body()
    if not body:
        return Response(status_code=400, content="Upload body is empty")
    workspace = settings.workspace_base / sandbox_id
    if workspace.exists():
        # Retry-friendly: a previous failed migration may have left partial
        # files; the incoming archive is the full source of truth.
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    migrate_dir = settings.workspace_base / "_migrate"
    migrate_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = migrate_dir / f"{sandbox_id}.tar.gz"
    try:
        tmp_path.write_bytes(body)
        logger.info(
            "import %s: received %d bytes",
            sandbox_id,
            len(body),
        )
        _extract_sandbox_archive(tmp_path, workspace)
    except (OSError, tarfile.TarError) as e:
        logger.warning("import %s failed: %s", sandbox_id, e, exc_info=True)
        return Response(status_code=400, content="Invalid tar archive")
    finally:
        tmp_path.unlink(missing_ok=True)
    return Response(status_code=204)


@router.get("/agent/sandboxes/{sandbox_id}/logs")
async def agent_sandbox_logs(sandbox_id: str, request: Request) -> Response:
    """Return the sandbox's command output log (JSON list)."""
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    log_path = settings.workspace_base / sandbox_id / "command-logs.jsonl"
    if not log_path.is_file():
        return JSONResponse(content=[])
    entries: list[dict[str, Any]] = []
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return Response(status_code=500)
    return JSONResponse(content=entries)


@router.get("/agent/health")
async def agent_health(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    payload = _register_payload(settings)
    payload["nodeID"] = os.getenv("E2B_NODE_ID")
    return payload


@router.post("/agent/snapshots", status_code=201)
async def agent_create_snapshot(request: Request) -> Response:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
        body = await request.json()
        snapshot_id = body.get("snapshotID")
        sandbox_id = body.get("sandboxID")
        if not snapshot_id or not sandbox_id:
            return Response(status_code=400, content="snapshotID and sandboxID required")
        src = settings.workspace_base / sandbox_id
        dst = settings.workspace_base / "_snapshots" / snapshot_id / "fs"
        if not src.is_dir():
            return Response(status_code=404, content=f"Sandbox {sandbox_id} not found")
        if dst.exists():
            return Response(status_code=409, content="snapshot already exists")
        shutil.copytree(src, dst, symlinks=True)
    except PermissionError:
        return Response(status_code=401)
    except Exception:
        logger.exception("agent create snapshot failed")
        return Response(status_code=500)
    return Response(status_code=201)


@router.delete("/agent/snapshots/{snapshot_id}", status_code=204)
async def agent_delete_snapshot(snapshot_id: str, request: Request) -> Response:
    settings = request.app.state.settings
    try:
        _require_internal_key(request, settings)
    except PermissionError:
        return Response(status_code=401)
    shutil.rmtree(
        settings.workspace_base / "_snapshots" / snapshot_id, ignore_errors=True
    )
    return Response(status_code=204)
