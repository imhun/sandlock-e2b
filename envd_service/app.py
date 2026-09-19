"""Envd service FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from envd_service.config import (
    Settings,
    check_net_isolation_pairing,
    check_seccomp_filter,
)
from envd_service.agent import (
    NodeAgent,
    _executor_needs_images,
    router as agent_router,
)
from envd_service.http.auth import HttpAuthError, http_error_response
from envd_service.http.files import router as files_router
from envd_service.http.health import router as health_router
from envd_service.http.mcp import router as mcp_router
from envd_service.quota_agent import wait_for_startup_readiness
from envd_service.quota_maintenance import QuotaMonitor
from envd_service.rpc import register_rpc
from envd_service.runtime.context import SandboxRuntimeContext
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.uid_pool import CAP_SYS_PTRACE, UidPool, has_effective_cap
from envd_service.xfs_quota import (
    NONROOT_DIRECT_QUOTA_REASON,
    ProjectQuotaError,
    direct_quota_unprivileged_reason,
    reconcile_orphan_projects,
)

logger = logging.getLogger(__name__)

#: E3.2 is on by default, and a non-root worker cannot honour it (no uid map,
#: no chown). Said out loud on purpose: the thing being lost is per-tenant
#: host-uid isolation, which shared volumes and route B both depend on.
PER_UID_NONROOT_WARNING = (
    "E2B_PER_SANDBOX_UID is enabled but the worker is not running "
    "as root; per-sandbox host uids are disabled (non-root workers "
    "use the fixed identity + Landlock model, E5.1)"
)

#: The other half of the same story: a root worker that dropped CAP_SYS_PTRACE
#: can allocate host uids but cannot map them for the in-process mediator.
PER_UID_NO_PTRACE_WARNING = (
    "E2B_PER_SANDBOX_UID is enabled on a root worker without CAP_SYS_PTRACE: "
    "the in-process RunAs path cannot write the sandbox's uid_map, so "
    "sandboxes that are not routed through a supervise slot will fail to "
    "start (add CAP_SYS_PTRACE, or keep chroot sandboxes on route B -- "
    "E2B_ROUTE_B=auto/on -- whose slot self-maps and needs no ptrace)"
)


async def _warm_base_image(settings: Settings) -> None:
    """Pre-extract the configured base image + template images at startup."""
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    images = [settings.base_image]
    images.extend(v for v in settings.template_images.values() if v)
    for image in dict.fromkeys(images):  # dedupe, keep order
        if not image:
            continue
        try:
            await asyncio.to_thread(
                resolve_image_rootfs,
                image,
                settings.image_cache_dir,
                registry_username=settings.image_registry_username,
                registry_password=settings.image_registry_password,
            )
            logger.info("worker image warmed: %s", image)
        except Exception:
            logger.warning("worker image warm failed: %s", image, exc_info=True)


async def _startup_reconcile(settings: Settings) -> None:
    """Reconcile quota table vs sandbox.json records once at worker startup."""
    try:
        result = await asyncio.to_thread(_startup_reconcile_once, settings)
    except ProjectQuotaError as exc:
        logger.warning("startup quota reconciliation skipped: %s", exc)
        return
    logger.info(
        "startup quota reconciliation: cleaned=%s skipped=%s",
        result.get("cleaned"),
        result.get("skipped"),
    )


def _startup_reconcile_once(settings: Settings) -> dict:
    """One blocking startup reconcile pass (the thread target).

    W6: the bounded quota-agent readiness wait runs first and on the same
    worker thread, so a worker that started before the agent did does not
    record the race as a degraded reconciliation — and the wait can never
    block the event loop or the heartbeat loop.
    """
    wait_for_startup_readiness(settings.workspace_base)
    return reconcile_orphan_projects(
        workspace_base=settings.workspace_base,
        mount_point=settings.workspace_base,
        via_agent=settings.quota_via_agent,
    )


async def _startup_uid_reconcile(pool: UidPool) -> None:
    """Reclaim orphan host uids once at worker startup (E3.2)."""
    result = await asyncio.to_thread(pool.reconcile)
    logger.info(
        "startup uid reconciliation: referenced=%s reclaimed=%s cleaned=%s "
        "skipped=%s",
        result.get("referenced"),
        result.get("reclaimed"),
        result.get("cleaned"),
        result.get("skipped"),
    )


def _shared_volume_traversal_gaps(root: Path, uid: int) -> list[tuple[Path, int]]:
    """Directories between ``/`` and ``root`` that ``uid`` cannot traverse.

    Mode bits, not ``os.access``: the worker (and the gate) runs as root, and
    root walks a 0700/0770 directory finer than the sandbox uid ever could, so an
    access(2) probe would report the tenant's view as fine.
    """
    gaps: list[tuple[Path, int]] = []
    for candidate in [root, *root.resolve().parents]:
        if candidate == Path("/"):
            break
        try:
            st = candidate.stat()
        except OSError:
            # A path that does not exist yet cannot block the sandbox either;
            # it is created (with traversal) when the volume is provisioned.
            continue
        mode = stat.S_IMODE(st.st_mode)
        executable = mode & 0o100 if st.st_uid == uid else mode & 0o001
        if not executable:
            gaps.append((candidate, mode))
    return gaps


def _disclose_shared_volume_traversal(
    settings: Settings, runtime_registry: RuntimeRegistry
) -> None:
    """Startup self-check (A5): can the first pool uid walk to the volume root?

    The mediator opens the volume host path as the mounting sandbox's own uid,
    so every level from ``/`` down to the volume view needs o+x. Without it the
    volume is unreachable by *any* path -- absolute ones included -- and the
    failure surfaces as an unexplained EACCES inside the sandbox. Say so once,
    naming the offending directory, its actual mode, and the fix.
    """
    if not settings.per_sandbox_uid or not settings.shared_volume_root:
        return
    pool = runtime_registry.uid_pool
    if pool is None:
        # Non-root worker: no uid pool, sandboxes keep the worker's own
        # identity, so the traversal requirement does not apply (E5.1).
        return
    root = Path(settings.shared_volume_root)
    gaps = _shared_volume_traversal_gaps(root, pool.start)
    if not gaps:
        return
    logger.warning(
        "shared volume root %s is not traversable for tenant uids (first pool "
        "uid %s): %s. The mediator opens volume host paths as the sandbox's "
        "own uid, so every level from / down to the volume view needs o+x; "
        "without it volume mounts fail with EACCES even on an absolute path. "
        "Fix: chmod 0711 (or 0755 where listing is acceptable) on each of "
        "those directories -- %s and its ancestors.",
        root,
        pool.start,
        "; ".join(f"{path} is mode {mode:04o}" for path, mode in gaps),
        root,
    )


def _disclose_nonroot_direct_quota(settings: Settings) -> None:
    """Startup disclosure (E5.1 review): a non-root worker without effective
    CAP_SYS_ADMIN cannot run ``xfs_quota -x`` directly (every call fails
    with EPERM), so with the agent form off (no ``E2B_QUOTA_AGENT_URL``) the
    per-sandbox disk hard limit silently degrades. Surface the required
    configuration once at startup. The effective-capability check (not euid
    alone) keeps a worker that holds CAP_SYS_ADMIN working without a spurious
    warning, and non-XFS hosts keep their real detection reason
    (Important-2 / Minor-13; A6 rewrote the remedy to the agent form).
    """
    if settings.quota_via_agent:
        return
    if direct_quota_unprivileged_reason(settings.workspace_base) is None:
        return
    logger.warning(
        "%s (direct xfs_quota requires root/CAP_SYS_ADMIN; per-sandbox "
        "disk hard limits are disabled while the agent form is off; set "
        "E2B_QUOTA_AGENT_URL to the quota-agent, or run the worker as root)",
        NONROOT_DIRECT_QUOTA_REASON,
    )


def create_app(
    *,
    settings: Settings | None = None,
    runtime_registry: RuntimeRegistry | None = None,
    workspace_base=None,
    control_plane_url: str | None = None,
    node_address: str | None = None,
) -> FastAPI:
    settings = settings or Settings()
    # E7.2 pairing guard (2026-09-16): refuse the shape whose only symptom is a
    # timeout in user code -- `net_isolation` without `fd_inject_connect` makes
    # every sandbox loopback-only. Fail here, by name, instead of shipping a
    # worker whose network looks "down" with nothing in its logs. The
    # intentional no-egress shape sets E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY=1.
    check_net_isolation_pairing(settings)
    # A7 follow-up (2026-09-16): the worker must actually run under the shipped
    # seccomp profile. A missing one is silent in two different ways -- no filter
    # at all (`Seccomp: 0`: the sandboxes inherit the worker's syscall surface),
    # or the runtime default instead of ours (a k8s node whose Localhost profile
    # file is missing: the kubelet skips it and the pod still comes up,
    # kubernetes#124944). Both would only surface as failing sandbox creates, so
    # they fail here by name. See envd_service/config.py for the two layers and
    # E2B_REQUIRE_SECCOMP_FILTER for the deliberate opt-out.
    check_seccomp_filter(settings)
    if getattr(settings, "allow_loopback_only", False) and getattr(
        settings, "enable_net_isolation", False
    ):
        logger.warning(
            "E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY=1: sandboxes will have no "
            "external egress at all (loopback-only, inbound via port_mappings)"
        )
    control_plane_url = control_plane_url or os.getenv("E2B_CONTROL_PLANE_URL")
    node_address = node_address or os.getenv("E2B_NODE_ADDRESS")
    quota_agent_client = None
    if settings.quota_via_agent:
        from envd_service.quota_agent import configure_quota_agent_client

        quota_agent_client = configure_quota_agent_client(
            url=settings.quota_agent_url,
            token=settings.quota_agent_token,
            timeout_s=settings.quota_agent_timeout_s,
        )
    runtime_registry = runtime_registry or RuntimeRegistry(
        workspace_base or settings.workspace_base
    )
    # Track F (Task F1): resolve the file-capability brokers once, before the
    # uid pool / route-B decisions below depend on them. A half-installed
    # broker pair raises here (named) instead of the worker quietly keeping a
    # weaker shape; "no brokers at all" keeps today's model with one warning.
    from envd_service import priv_helpers

    priv_helpers.configure_priv_helpers(settings)
    brokers = priv_helpers.active_helpers()
    unavailable = priv_helpers.helpers_unavailable_reason(settings)
    if unavailable is not None:
        logger.warning("%s", unavailable)
    # E5.1: per-sandbox host uids need a privileged supervisor -- root /
    # CAP_SETUID + chown, or (Track F) the two file-capability brokers, which
    # are exactly how a non-root worker (uid 65534) gets those steps. Without
    # either, the switch is auto-disabled and the worker keeps the
    # fixed-identity + Landlock model instead of crash-looping on EPERM.
    if settings.per_sandbox_uid and (os.geteuid() == 0 or brokers is not None):
        # Fix round 1 (c1) hard guard: a sandbox tree is
        # `0770 owner=<sandbox uid> group=<worker gid>` and the worker is a
        # member of that group, so a sandbox allocated the worker's own uid or
        # gid would be inside the worker's trust boundary (it could read every
        # other sandbox's workspace). Refuse the configuration by name instead
        # of shipping the hole -- for a root worker too, since the group model
        # is what makes the shared tree safe.
        priv_helpers.check_worker_identity_outside_pool(
            uid=os.geteuid(),
            gid=os.getegid(),
            start=settings.uid_pool_start,
            size=settings.uid_pool_size,
        )
        if os.geteuid() == 0 and not has_effective_cap(CAP_SYS_PTRACE):
            # Writing a *child's* uid_map needs CAP_SETUID **and** ptrace access
            # to that child, so a root worker with a hardened capability set
            # cannot remap sandboxes in-process -- measured: every create fails
            # with the fork's generic `sandlock_create failed`. Route B is not
            # affected (its slot already *is* the sandbox uid and self-maps), so
            # this is a disclosure with a remedy, not a new failure mode.
            logger.warning(PER_UID_NO_PTRACE_WARNING)
        runtime_registry.uid_pool = UidPool(
            start=settings.uid_pool_start,
            size=settings.uid_pool_size,
            workspace_base=workspace_base or settings.workspace_base,
        )
        runtime_registry.add_unregister_callback(
            runtime_registry.uid_pool.release
        )
    elif settings.per_sandbox_uid:
        logger.warning(PER_UID_NONROOT_WARNING)
    _disclose_nonroot_direct_quota(settings)
    quota_monitor = QuotaMonitor(
        workspace_base=settings.workspace_base,
        mount_point=settings.workspace_base,
        via_agent=settings.quota_via_agent,
        interval_s=settings.quota_monitor_interval_s,
        quota_warn_ratio=settings.quota_warn_ratio,
        disk_warn_ratio=settings.disk_warn_ratio,
        disk_error_ratio=settings.disk_error_ratio,
    )
    agent = NodeAgent(
        settings=settings,
        runtime_registry=runtime_registry,
        control_plane_url=control_plane_url,
        node_address=node_address,
        metrics_provider=quota_monitor.metrics,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enable_netns:
            from envd_service.netns import ensure_worker_netns_plumbing

            ensure_worker_netns_plumbing()
        # A5: volumes are opened by the mediator as the sandbox's own uid, so
        # an untraversable ancestor chain silently breaks every volume mount.
        _disclose_shared_volume_traversal(settings, runtime_registry)
        app.state.quota_monitor = quota_monitor
        quota_monitor.start()
        reconcile_task: asyncio.Task | None = None
        if settings.quota_reconcile_on_startup:
            reconcile_task = asyncio.create_task(_startup_reconcile(settings))
            app.state.reconcile_task = reconcile_task
        uid_reconcile_task: asyncio.Task | None = None
        if (
            settings.per_sandbox_uid
            and settings.uid_reconcile_on_startup
            and (os.geteuid() == 0 or priv_helpers.active_helpers() is not None)
            and runtime_registry.uid_pool is not None
        ):
            uid_reconcile_task = asyncio.create_task(
                _startup_uid_reconcile(runtime_registry.uid_pool)
            )
        agent.start()
        if settings.base_image and _executor_needs_images(settings.executor):
            app.state.warm_task = asyncio.create_task(_warm_base_image(settings))
        yield
        warm_task = getattr(app.state, "warm_task", None)
        if warm_task is not None:
            warm_task.cancel()
        if reconcile_task is not None:
            reconcile_task.cancel()
            try:
                await reconcile_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("startup reconcile task failed", exc_info=True)
        if uid_reconcile_task is not None:
            uid_reconcile_task.cancel()
            try:
                await uid_reconcile_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "startup uid reconcile task failed", exc_info=True
                )
        await agent.stop()
        await quota_monitor.stop()
        quota_agent_client = getattr(app.state, "quota_agent_client", None)
        if quota_agent_client is not None:
            quota_agent_client.close()
        for ctx in app.state.runtimes.values():
            ctx.shutdown()
        app.state.runtimes.clear()

    app = FastAPI(title="E2B Sandlock Gateway - Envd Service", lifespan=lifespan)
    app.state.settings = settings
    app.state.quota_agent_client = quota_agent_client
    app.state.runtime_registry = runtime_registry
    app.state.runtimes: dict[str, SandboxRuntimeContext] = {}
    app.state.context_factory = lambda record: SandboxRuntimeContext(
        record, settings, runtime_registry=runtime_registry
    )
    # N25/L2c: the disk accounting asks each sandbox's *mediator* what changed
    # (the file-level "who wrote what" lives inside its session), while the
    # registry owns the sizes. A sandbox with no live context has no mediator
    # to ask -- and nothing written through one either -- so this answers
    # `None`, which the caller reads as "walk the tree".
    runtime_registry.set_dirty_provider(
        lambda sandbox_id: (
            app.state.runtimes[sandbox_id].drain_dirty_dirs()
            if sandbox_id in app.state.runtimes
            else None
        )
    )
    # N25: the other half of the same split. The registry decides *when* a
    # sandbox's remaining budget is small enough to be worth acting on (it is
    # the only component that knows what is left); the sandbox's context
    # applies it to the live process. A sandbox with no live context has
    # nothing running to tighten, so it answers `None`.
    runtime_registry.set_disk_tightener(
        lambda sandbox_id, bytes_: (
            app.state.runtimes[sandbox_id].set_file_size_limit(bytes_)
            if sandbox_id in app.state.runtimes
            else None
        )
    )
    runtime_registry.add_unregister_callback(
        lambda sandbox_id: (
            app.state.runtimes.pop(sandbox_id, None).shutdown()
            if sandbox_id in app.state.runtimes
            else None
        )
    )
    runtime_registry.add_state_callback(
        lambda sandbox_id, state: (
            (
                app.state.runtimes[sandbox_id].pause()
                if state == "paused"
                else app.state.runtimes[sandbox_id].resume()
            )
            if sandbox_id in app.state.runtimes
            else None
        )
    )

    app.add_exception_handler(HttpAuthError, http_error_response)
    app.include_router(health_router)
    app.include_router(files_router)
    app.include_router(mcp_router)
    app.include_router(agent_router)
    register_rpc(app)
    return app
