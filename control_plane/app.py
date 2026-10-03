"""Control plane FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI

from control_plane.api.errors import OfficialError, official_error_handler
from control_plane.api.internal import router as internal_router
from control_plane.api.nodes import router as nodes_router
from control_plane.api.sandboxes import (
    pause_record_for_platform,
    router as sandboxes_router,
)
from control_plane.api.secrets import router as secrets_router
from control_plane.api.snapshots import (
    reconcile_pending_snapshots_or_report,
    router as snapshots_router,
    snapshot_reconcile_loop,
)
from control_plane.api.templates import router as templates_router
from control_plane.api.volumes import router as volumes_router
from control_plane.autoscaler_service import build_service as build_autoscaler_service
from control_plane.c3_agent_client import (
    C3AgentClient,
    build_agent_address_resolver,
)
from control_plane.config import Settings, local_node_quota_via_agent
from control_plane.metrics import SlidingWindowCounter
from control_plane.node_address import build_node_address_resolver
from control_plane.queue import CreateQueue
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.ledger_alert import (
    PlatformLedgerAlerter,
    ledger_alert_ratio,
)
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.idle_pause import (
    IdlePauseSweeper,
    idle_pause_after_seconds,
)
from control_plane.registry.paused_ttl import (
    PausedTTLSweeper,
    paused_ttl_seconds,
    reap_paused_sandbox,
)
from control_plane.registry.secrets import SecretRegistry
from control_plane.registry.snapshots import SnapshotRegistry
from control_plane.registry.templates import TemplateRegistry
from control_plane.ratelimit import SlidingWindowRateLimiter
from control_plane.registry.ttl import TTLSweeper
from control_plane.registry.redis_backend import try_claim

#: The TTL sweeper's cadence, and therefore the width of its single-flight
#: claim (F11 step 4): one replica per interval, no lock to release.
_TTL_SWEEP_INTERVAL_S = 1.0

#: E9.1 x E9.2 idle->pause cadence. Deliberately slower than the TTL sweep:
#: the threshold it measures against is minutes wide, so a 15 s round is
#: plenty, and each round is a shared-store scan. The claim below is the
#: interval, exactly like the other periodic jobs (F11 step 4).
_IDLE_PAUSE_INTERVAL_S = 15.0

#: E7's platform-account scan. Deliberately slower than the TTL sweep: the
#: numbers only move when a worker heartbeats (every 5 s) or a capture lands,
#: and the alert is a crossing, not a live gauge -- 30 s is well inside the
#: window in which an operator can still react to "a fifth of the account
#: left", and it keeps the shared node view off the once-a-second hot path.
_LEDGER_ALERT_INTERVAL_S = 30.0
from control_plane.registry.volumes import VolumeRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only (envd may be absent)
    from envd_service.quota_agent import QuotaAgentClient


logger = logging.getLogger(__name__)

#: "The caller said nothing about the C3 agent client", as opposed to passing
#: ``None`` -- which is a deployment that deliberately has none, and must make
#: the slot-identity endpoint refuse by name rather than quietly build one.
_UNSET = object()


class LegacyVolumeLayoutError(RuntimeError):
    """N73: old flat volumes would be hidden by the new volume store root.

    ``E2B_SHARED_VOLUME_ROOT`` means the shared *export* root (the worker and
    the per-node agent both read it that way), and since Task 20 the control
    plane derives ``<shared_volume_root>/"_volumes"`` from it instead of using
    it as the store root. A deployment that set it *before* Task 20 built its
    volumes as flat ``<shared_volume_root>/vol_*`` directories; deriving the
    subdirectory would leave those volumes invisible to the API. Starting is
    refused by name so the operator migrates them (or names the old directory
    outright with ``E2B_VOLUME_STORE_ROOT``) instead of silently losing them.
    """


def _legacy_flat_volumes(shared_root: Path) -> list[Path]:
    """N73: the pre-Task-20 flat ``vol_*`` directories under a shared root."""
    return sorted(path for path in shared_root.glob("vol_*") if path.is_dir())


def _derive_volume_store_root(settings: Settings, platform_root: Path) -> Path:
    """N73: the volume store root, from the dedicated name or the shared root.

    ``E2B_VOLUME_STORE_ROOT`` (new) is the authority; otherwise a named
    ``E2B_SHARED_VOLUME_ROOT`` is the *export* root and the store is
    ``<shared_volume_root>/"_volumes"``; neither named keeps the pre-N73
    default ``<platform_root>/"_volumes"`` (today's k8s shape, byte for byte).

    The self-protection is the reason this is not a silent path move: the
    compose stacks set ``E2B_SHARED_VOLUME_ROOT`` on the control plane today,
    so a deployment whose volumes are the old flat ``<shared_volume_root>/
    vol_*`` directories would lose them to the derived subdirectory. It refuses
    by name instead (unless the derived root *is* that directory -- naming the
    old root with ``E2B_VOLUME_STORE_ROOT`` keeps them in view).
    """
    # ``str(...)`` on purpose: embedders and tests pass ``Path`` here, and the
    # fields are only typed ``str | None`` because that is how the env reads.
    named_store = (
        str(settings.volume_store_root).strip()
        if settings.volume_store_root
        else ""
    )
    shared = (
        str(settings.shared_volume_root).strip()
        if settings.shared_volume_root
        else ""
    )
    shared_root = Path(shared).resolve() if shared else None
    if named_store:
        store_root = Path(named_store).resolve()
    elif shared_root is not None:
        store_root = shared_root / "_volumes"
    else:
        return platform_root / "_volumes"
    if shared_root is not None:
        legacy = _legacy_flat_volumes(shared_root)
        if legacy and store_root != shared_root:
            raise LegacyVolumeLayoutError(
                "N73: found the legacy flat volume layout under the shared "
                f"volume root {shared_root} (e.g. {legacy[0]}), while the "
                f"control plane's volume store root is {store_root}. "
                "E2B_SHARED_VOLUME_ROOT is the shared *export* root, so "
                "starting would hide those volumes "
                "(legacy-flat-volume-layout). Migrate them under "
                f"{shared_root / '_volumes'}, or point E2B_VOLUME_STORE_ROOT "
                f"at {shared_root} to keep the old location, then restart (N73)."
            )
    return store_root


class _NoopRuntimeRegistry:
    """Empty runtime registry for the separated control-plane deployment.

    The envd service (and its runtime registry) only exists on worker hosts;
    the control plane never provisions local sandboxes when
    ``E2B_ENABLE_LOCAL_NODE=false``, so every registry call is a safe no-op.
    """

    def register(self, **kwargs):
        return None

    def get(self, sandbox_id):
        return None

    def unregister(self, sandbox_id) -> None:
        pass

    def set_state(self, sandbox_id, state) -> None:
        pass

    def freeze(self, sandbox_id) -> None:
        pass

    def thaw(self, sandbox_id) -> None:
        pass


def _wire_local_node_quota_agent(
    settings: Settings,
) -> "QuotaAgentClient | None":
    """Wire the quota-agent hooks for a combined ("合体") node.

    ``control_plane.combined_main`` runs the control plane and the envd
    gateway in one process, but it never builds the envd worker app
    (``envd_service.app.create_app``) -- which is where the agent hooks
    (``xfs_quota.agent_query`` / ``agent_ops``) are normally wired. The
    control plane's own volume quota reads the same switch
    (:func:`control_plane.config.local_node_quota_via_agent`), so the hooks
    have to exist here too; without them a deployment that *did* configure
    ``E2B_QUOTA_AGENT_URL`` would still be told "quota-agent not configured"
    and silently keep the degraded (no hard limit) shape.

    The three agent coordinates are read with the envd service's own names and
    defaults (pinned by ``tests/unit/test_controlplane_local_node_quota.py``),
    *not* by building ``envd_service.config.Settings``: the combined control
    plane has no business validating the rest of the worker's configuration
    (port mappings, template images, ...), and a malformed unrelated value
    must not take it down at startup.

    Returns the client to close on shutdown, or ``None`` when the agent form
    is off. A separated control plane (``E2B_ENABLE_LOCAL_NODE=false``)
    provisions no quota in-process and wires nothing, exactly like it does not
    need the envd runtime registry.
    """
    if not settings.enable_local_node:
        return None
    try:
        from envd_service.quota_agent import configure_quota_agent_client
    except ImportError:  # pragma: no cover - separated control-plane image
        return None
    if not local_node_quota_via_agent():
        return None
    from gateway_common.env import env_float

    return configure_quota_agent_client(
        url=(os.getenv("E2B_QUOTA_AGENT_URL") or "").strip() or None,
        token=os.getenv("E2B_QUOTA_AGENT_TOKEN") or None,
        timeout_s=env_float("E2B_QUOTA_AGENT_TIMEOUT_S", 5.0),
    )


async def _node_health_loop(
    app: FastAPI,
    registry,
    *,
    window_s: float,
    interval_s: float = 1.0,
    clock=time.monotonic,
) -> None:
    """Mark sandboxes on lost remote nodes orphaned (E6.1).

    The evidence is "this node's last heartbeat is older than the window", and
    that evidence is only valid while *this loop* is running on schedule. After
    a control-plane stall -- a blocking call in a handler, a long pause -- the
    heartbeats that arrived during the stall have not been handled yet, so
    their timestamps are still the old ones and *every* node looks lost at
    once. The verdict is destructive (live sandboxes are torn down as orphans
    and every later request for them answers 409), so a round that finds
    itself ``window_s`` behind skips it and lets the next one decide: by then
    the queued heartbeats have been handled and a node that really is gone is
    still stale. Measured before this guard (2026-09-22, k0s cluster): one
    2000-file snapshot blocked the loop for 76.1 s behind a synchronous
    ``httpx.post``, the two workers' heartbeats gapped 77.2 s / 78.3 s in the
    access log, and the first sweep after the loop resumed answered
    ``node health sweep: orphaned sandboxes on e2b-worker-1``.
    """
    log = logging.getLogger(__name__)
    previous = clock()
    while True:
        now = clock()
        behind = now - previous
        previous = now
        if behind > window_s:
            log.warning(
                "node health sweep: skipping this round -- the control plane "
                "itself was %.1fs behind (>= the %.0fs heartbeat window), so a "
                "stale heartbeat is not yet evidence that a node is gone",
                behind,
                window_s,
            )
            await asyncio.sleep(interval_s)
            continue
        try:
            # F11 step 2: one replica sweeps per round. The view is shared, so
            # every replica computes the same health verdict and the same
            # orphan set -- a second sweep in the same window is duplicated
            # work (and a duplicated warning), not extra coverage. A round
            # whose claim is lost simply does nothing; the next one is a
            # second later.
            if app.state.nodes.try_acquire_sweep(ttl_s=interval_s):
                marked = await asyncio.to_thread(
                    app.state.nodes.reap_unhealthy, registry
                )
            else:
                marked = []
            if marked:
                log.warning(
                    "node health sweep: orphaned sandboxes on %s",
                    ", ".join(marked),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("node health sweep failed")
        await asyncio.sleep(interval_s)


def create_app(
    *,
    settings: Settings | None = None,
    registry: SandboxRegistry | None = None,
    runtime_registry=None,
    workspace_base=None,
    volumes_registry=None,
    secrets_registry=None,
    snapshots_registry=None,
    nodes_registry=None,
    templates_registry=None,
    node_address_resolver=None,
    c3_agent_client=_UNSET,
    worker_identity_source=_UNSET,
    autoscaler_backend=_UNSET,
) -> FastAPI:
    settings = settings or Settings()
    redis_client = None
    quota_agent_client = _wire_local_node_quota_agent(settings)
    if settings.redis_url:
        from control_plane.registry.redis_backend import create_redis_client

        redis_client = create_redis_client(settings.redis_url)
    registry = registry or SandboxRegistry(settings, redis_client=redis_client)
    # E9.4: capacity-release queue for creates that survive an eviction round
    # (E9.3) but still have no room. The registry fires the callback on every
    # real quota release (pause / delete / expiry); CreateQueue turns it into
    # event-loop wakeups for waiting POST /sandboxes requests.
    create_queue = CreateQueue(
        timeout_s=settings.create_queue_timeout_s,
        max_waiters=settings.create_queue_max,
    )
    registry.add_on_quota_released(create_queue.notify_capacity)

    def _release_node_quota(record) -> None:
        if record.quota_released:
            # E9.2: a paused sandbox already handed its node reservation back;
            # releasing it again on delete would steal quota from the
            # sandboxes that still hold it.
            return
        app.state.nodes.release_quota(
            record.node_id or "local",
            memory_mb=record.memory_mb,
            cpu_percent=record.cpu_count * 100,
            disk_mb=record.disk_size_mb,
            processes=record.max_processes,
        )

    registry.add_on_removed(_release_node_quota)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def _on_sandbox_removed(record) -> list[str]:
            """Tear a sandbox down where it lives; report what went with it.

            Shared by the two sweeps that outlive their caller (the ordinary
            TTL sweep, and E6's paused sweep). Both of them answer a *deadline*
            rather than a request, so both run the teardown the delete endpoint
            runs -- the worker's own ``DELETE /agent/sandboxes/{id}``, which is
            what removes the runtime, the tree and the checkpoint image (the
            largest thing the platform holds for a sandbox). A node that
            refuses or cannot be reached is a WARNING and "the worker's next
            reconcile reclaims it", never a silent success, and the return
            value says what really went so the caller's one line can name it.
            """
            node = app.state.nodes.get(record.node_id or "local")
            if node is not None and node.address != "local://":
                import httpx

                try:
                    # Async, and awaited by the TTL sweeper: a synchronous call
                    # here blocked the event loop for up to its 30 s timeout --
                    # the same class of stall the node-health sweep now refuses
                    # to read as a dead node (N32).
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.delete(
                            f"{node.address}/agent/sandboxes/{record.sandbox_id}",
                            headers={
                                "X-Internal-Key": settings.internal_api_key
                            },
                        )
                except httpx.HTTPError as exc:
                    # TTL expiry has no HTTP response to answer with, so the
                    # only honest signal is this line (review W7 / C1-1): the
                    # node could not be reached, so the tree stays on the
                    # worker and its next reconcile reclaims it (no
                    # control-plane record references it any more).
                    logging.getLogger(__name__).warning(
                        "TTL: %s was not torn down on %s: %s; its tree is left "
                        "to that worker's next reconcile",
                        record.sandbox_id,
                        node.node_id,
                        exc,
                    )
                    return []
                else:
                    if resp.status_code != 204:
                        # The node answered and refused (a rewritten
                        # ``sandbox.json`` is the shape): nothing was torn
                        # down, and the tree is an orphan now that the record
                        # is gone, so the orphan-tree GC is the retry.
                        logging.getLogger(__name__).warning(
                            "TTL: %s was refused by %s (HTTP %s): %s; its tree "
                            "is left to the orphan-tree GC",
                            record.sandbox_id,
                            node.node_id,
                            resp.status_code,
                            (resp.text or "")[:300],
                        )
                        return []
                app.state.runtime_registry.unregister(record.sandbox_id)
                return ["runtime", "checkpoint-image"]
            else:
                # Local runtime: full teardown (unregister + workspace and
                # per-sandbox volume slice cleanup, E2.5).
                from control_plane.api.sandboxes import _destroy_local

                if not _destroy_local(app.state, record).acknowledged:
                    logging.getLogger(__name__).warning(
                        "TTL: the local teardown of %s was refused; its "
                        "runtime was stopped and its tree is left to the "
                        "orphan-tree GC",
                        record.sandbox_id,
                    )
                    return []
                # The local shape's checkpoint images are not part of that
                # tree; the worker's own reconcile reclaims an image with no
                # owner (E4), so this branch reports exactly what it removed.
                return ["runtime", "workspace"]

        sweeper = TTLSweeper(
            on_expired=_on_sandbox_removed,
            interval_seconds=_TTL_SWEEP_INTERVAL_S,
            # F11 step 4: one replica per round. The deadlines live on shared
            # records, so every replica would otherwise expire the same
            # sandboxes (and call every teardown twice).
            #
            # N61 裁定 A（2026-10-03）：claim 的 TTL **留在节奏上**（1 s），
            # 不是"一轮的上界"。``try_claim`` 是 ``SET key 1 NX EX ttl``、
            # 全仓库没有释放路径，所以比节奏更长的 TTL 会让舰队级的扫描周期
            # 等于那个 TTL —— 实测（真实 ``try_claim`` + 假 client，``ttl_s=60``）：
            # A 第一轮 True、A 第二轮 False、B 也 False、key TTL=60 ⇒ 1 s 的节奏
            # 变 60 s 的周期，是行为回退。代价（已知、本批不改）：一轮超过 1 s 时
            # claim 会在轮内过期，peer 可能同时开一轮；这件事由 sweeper 的
            # "轮次超时 WARNING" 具名（`control_plane/registry/ttl.py`）。
            claim=lambda: try_claim(
                redis_client, "e2b:ttl:sweep", ttl_s=int(_TTL_SWEEP_INTERVAL_S)
            ),
        )
        app.state.sweeper = sweeper
        sweeper.start(registry)

        # E6: parked sandboxes are exempt from the TTL sweep above on purpose
        # (destroying a park destroys user state), so ending a park is its own
        # opt-in switch with its own single-flight claim -- and its cleanup is
        # the *same* teardown the delete endpoint runs, never a parallel one.
        paused_sweeper = PausedTTLSweeper(
            ttl_s=paused_ttl_seconds(settings),
            interval_seconds=_TTL_SWEEP_INTERVAL_S,
            on_expired=lambda record, paused_for_s: reap_paused_sandbox(
                record, registry=registry, teardown=_on_sandbox_removed
            ),
            claim=lambda: try_claim(
                redis_client,
                "e2b:paused-ttl:sweep",
                ttl_s=int(_TTL_SWEEP_INTERVAL_S),
            ),
        )
        app.state.paused_sweeper = paused_sweeper
        paused_sweeper.start(registry)

        # E9.1 x E9.2: a sandbox nobody is using gives its capacity back. Same
        # shape as the paused sweep above -- one replica per round, selected
        # from shared records -- and the action is the pause endpoint's own
        # chain, so "paused" cannot come to mean two different things
        # depending on who pressed the button.
        idle_sweeper = IdlePauseSweeper(
            after_s=idle_pause_after_seconds(settings),
            on_idle=lambda record, idle_s: pause_record_for_platform(
                app.state, record, reason=f"idle {idle_s:.0f}s"
            ),
            interval_seconds=_IDLE_PAUSE_INTERVAL_S,
            claim=lambda: try_claim(
                redis_client,
                "e2b:idle-pause:sweep",
                ttl_s=int(_IDLE_PAUSE_INTERVAL_S),
            ),
        )
        app.state.idle_sweeper = idle_sweeper
        idle_sweeper.start(registry)

        # E7: the platform account is a soft ledger (see §6(k)②), so the one
        # thing it needs is a reader. One replica per interval, one line per
        # crossing -- the shared node view is what makes both possible.
        ledger_alerter = PlatformLedgerAlerter(
            threshold_ratio=ledger_alert_ratio(settings),
            interval_seconds=_LEDGER_ALERT_INTERVAL_S,
            claim=lambda: try_claim(
                redis_client,
                "e2b:ledger-alert:sweep",
                ttl_s=int(_LEDGER_ALERT_INTERVAL_S),
            ),
        )
        app.state.ledger_alerter = ledger_alerter
        ledger_alerter.start(app.state.nodes)

        health_task = asyncio.create_task(
            _node_health_loop(
                app, registry, window_s=settings.node_heartbeat_timeout_s
            )
        )
        # N29 (async shape): a reserved copy is an in-process task, so a
        # restart leaves its record at `creating` and a poller waiting on a
        # copy nobody runs. One pass settles them -- resumed from the worker's
        # idempotent answer when the payload did finish, failed otherwise --
        # and skips the ones whose shared claim a *live* peer still holds
        # (F11 step 3: those are that replica's copy, not an orphan).
        startup_snapshot_task = asyncio.create_task(
            reconcile_pending_snapshots_or_report(app)
        )
        # N46: the startup pass above is not enough once ``creating`` records
        # can belong to a *live* owner. An unnamed async copy now holds a
        # short lease that its owner renews while it runs, so "no claim" still
        # means "orphan" -- but only after the owner's lease lapses, which the
        # one-shot pass cannot wait for. This cadence is that second look; it
        # is single-flight (``try_acquire_reconcile``), so two replicas never
        # settle the same record.
        snapshot_reconcile_task = asyncio.create_task(
            snapshot_reconcile_loop(app)
        )
        # The hosted autoscaler (k8s shape): one decision interval at a time
        # fleet-wide (the claim inside the service), started and cancelled with
        # this app so a rolling control plane never leaves a loop behind
        # pointing at a process that already stopped serving.
        if app.state.autoscaler is not None:
            app.state.autoscaler_task = asyncio.create_task(
                app.state.autoscaler.run_forever()
            )
        app.state.node_health_task = health_task
        # One keep-alive client for the control plane's own calls to worker
        # agents (the create provisioning POST, and anything else that talks to
        # a node's agent). It used to be built per call -- a fresh TCP connect
        # (and a DNS lookup of the worker's pod IP) on every sandbox create.
        app.state.remote_http = httpx.AsyncClient(timeout=60)
        try:
            yield
        finally:
            # The startup pass is short, but it must not outlive the app: an
            # interrupted resume would leave a record half-updated.
            if not startup_snapshot_task.done():
                try:
                    await asyncio.wait_for(startup_snapshot_task, timeout=30)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    startup_snapshot_task.cancel()
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass
            snapshot_reconcile_task.cancel()
            try:
                await snapshot_reconcile_task
            except asyncio.CancelledError:
                pass
            autoscaler_task = app.state.autoscaler_task
            if autoscaler_task is not None:
                autoscaler_task.cancel()
                try:
                    await autoscaler_task
                except asyncio.CancelledError:
                    pass
        await sweeper.stop()
        await paused_sweeper.stop()
        await idle_sweeper.stop()
        await ledger_alerter.stop()
        remote_http = getattr(app.state, "remote_http", None)
        if remote_http is not None:
            # ``app.state.remote_http`` is "whatever shape is wired": this
            # repo's tests (and an embedder) replace the client wholesale, so
            # the teardown asks for the method rather than assuming it.
            aclose = getattr(remote_http, "aclose", None)
            if aclose is not None:
                await aclose()
            app.state.remote_http = None
        if quota_agent_client is not None:
            quota_agent_client.close()

    # SEC-K0S-002 (2026-10-01): no interactive surface. The tenant entrance is
    # reachable (and, per SEC-K0S-004, reachable *from inside a sandbox*), so
    # `/openapi.json` handed an unauthenticated caller the whole internal path
    # inventory -- including the C3 `file-op` relay and the prose that spells
    # out the trust model. The privileged C3 agent already closes these three
    # for the same reason; the control plane must match.
    app = FastAPI(
        title="E2B Sandlock Gateway - Control Plane",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.redis_client = redis_client
    app.state.quota_agent_client = quota_agent_client
    app.state.registry = registry
    app.state.create_queue = create_queue

    # Health endpoints for load balancer / monitoring. These are registered
    # before the gateway mount (combined_main), so they win over the gateway
    # catch-all; the sandbox-scoped /health with E2b-Sandbox-Id still routes
    # through the gateway proxy untouched.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root_health() -> dict[str, str]:
        return {"status": "ok", "service": "e2b-sandlock"}

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if runtime_registry is None:
        # The envd service is only imported on the single-host deployment:
        # a separated control-plane image (E2B_ENABLE_LOCAL_NODE=false) runs
        # without it and uses a no-op registry instead.
        try:
            from envd_service.runtime.registry import RuntimeRegistry
        except ImportError:  # pragma: no cover - separated control plane
            RuntimeRegistry = None  # type: ignore[assignment]
        if RuntimeRegistry is not None:
            runtime_registry = RuntimeRegistry(
                workspace_base or settings.workspace_base,
                state_base=settings.state_base,
            )
        else:
            runtime_registry = _NoopRuntimeRegistry()
    app.state.runtime_registry = runtime_registry
    # E9.1: a single-process (combined/local-node) deployment has no worker
    # heartbeat to carry sandbox activity, so the shared runtime registry
    # reports it straight into the sandbox registry's idle accounting.
    add_activity_callback = getattr(
        runtime_registry, "add_activity_callback", None
    )
    if callable(add_activity_callback):
        add_activity_callback(
            lambda sandbox_id, moment: registry.apply_activity_report(
                None, {sandbox_id: moment}
            )
        )
    app.state.workspace_base = workspace_base or settings.workspace_base
    # Task 3: only the process that *owns* a tree root may create it. In the
    # shared shape (``trees_shared``) that path is on the shared volume and this
    # mkdir keeps the pre-reslice behaviour; on the combined node
    # (``enable_local_node``) this process is the tree's node; but a separated
    # control plane whose trees are node-local has no mount at that path at
    # all, and inventing one -- ``mkdir(parents=True)`` in its own container
    # layer -- would hand it a fake tree root that no agent can ever see. The
    # value stays named (paths it derives for the agents are resolved on those
    # nodes); the directory is theirs to make.
    if settings.trees_shared or settings.enable_local_node:
        app.state.workspace_base.mkdir(parents=True, exist_ok=True)
    # N27: the base the platform's *own* files live under -- the sandboxes'
    # runtime records, their command logs, the checkpoint images. Same rule as
    # the worker's (``envd_service.app``): the *registry's* answer wins, because
    # that is where the records this app serves were written; ``create_app`` may
    # have been handed a registry of its own (tests, embedders) sitting on a
    # base the settings do not name. Unset, this is the workspace base, i.e. the
    # layout that predates N27.
    app.state.state_base = (
        getattr(runtime_registry, "state_base", None) or settings.state_base
    )
    # N27: the root the platform's *shared* directories sit on (``_secrets``,
    # ``_snapshots``, ``_templates``, ``_volumes``). That is the shared
    # workspace root, *not* the workspace base: the latter names the sunk tree
    # root (``<export>/workspaces``) once N27 is deployed, while those
    # directories stay where the worker's ``workspace-root-init`` creates them
    # and where this pod's writable subPath mounts are. Deriving them from the
    # tree root instead would put every one of these registries on the
    # read-only view of the volume (OBS-9).
    platform_root = (
        Path(settings.shared_workspace_root).resolve()
        if settings.shared_workspace_root
        else app.state.workspace_base
    )
    #: ...and the same root for the rest of the platform's top-level namespaces
    #: (``_builds`` is the one another module reaches for). Exposed on
    #: ``app.state`` so no module has to re-derive it from the tree base.
    app.state.platform_root = platform_root
    # The two bases, said out loud once: a deployment that moves either one can
    # be reconciled against the worker's line by grepping (Task 7), and the
    # split between the two processes is otherwise invisible until a record
    # "goes missing".
    logger.info("workspace base = %s", app.state.workspace_base)
    logger.info("platform state base = %s", app.state.state_base)
    # N73: the store root and the shared *export* root are two names now.
    # ``E2B_VOLUME_STORE_ROOT`` wins; a named ``E2B_SHARED_VOLUME_ROOT`` only
    # derives ``<shared_volume_root>/"_volumes"`` from it (pre-N73 it *was* the
    # store root, which pointed the store at the read-only export mount).
    # Deriving over a deployment's old flat ``vol_*`` directories refuses to
    # start by name rather than hiding them -- see the helper.
    volume_root = _derive_volume_store_root(settings, platform_root)
    app.state.volumes = volumes_registry or VolumeRegistry(
        volume_root,
        redis_client=redis_client,
        token_ttl_seconds=settings.volume_token_ttl_s,
    )
    app.state.secrets = secrets_registry or SecretRegistry(
        platform_root / "_secrets",
        redis_client=redis_client,
        master_key=settings.secret_master_key,
        legacy_master_keys=settings.secret_master_keys,
    )
    app.state.snapshots = snapshots_registry or SnapshotRegistry(
        platform_root,
        # F11 step 3: the per-id copy claim is fleet-wide when the deployment
        # has a shared store (the record's ``creating`` status is the durable
        # half of it; this is the window between "no record" and "record").
        redis_client=redis_client,
    )
    app.state.nodes = nodes_registry or NodeRegistry(
        redis_client=redis_client,
        heartbeat_timeout=settings.node_heartbeat_timeout_s,
    )
    # C3 Task 2 / D4: where the internal API's *expected* node address and
    # source IP come from. Injected by tests and embedders; otherwise built from
    # ``E2B_NODE_ADDRESS_MODE`` (k8s pod API, or compose hostname resolution).
    # Never learned from the request being validated (N49).
    app.state.node_address_resolver = (
        node_address_resolver or build_node_address_resolver(settings)
    )
    # C3 Task 3: the CP→agent instruction channel (the other half of the fleet's
    # two channels). Built from the deployment's own shape; the token is what
    # the agent demands, and an unset one is a named refusal per instruction
    # rather than a silent unauthenticated call.
    if c3_agent_client is _UNSET:
        c3_agent_client = C3AgentClient(
            resolver=build_agent_address_resolver(settings),
            token=settings.c3_agent_token,
            timeout_s=settings.c3_agent_timeout_s,
            file_op_timeout_s=settings.c3_agent_file_op_timeout_s,
            materialize_timeout_s=settings.c3_agent_materialize_timeout_s,
            max_concurrency=settings.c3_agent_max_concurrency,
        )
    app.state.c3_agent_client = c3_agent_client
    # Where a worker's own uid/gid may come from (C3 Task 4, fourth review ②):
    # a trusted source, never the worker's own report. Unresolvable for a shape
    # means "record no identity" -- and then every file operation that needs one
    # refuses by name.
    if worker_identity_source is _UNSET:
        from control_plane.worker_identity_source import (
            build_worker_identity_source,
        )

        worker_identity_source = build_worker_identity_source(settings)
    app.state.worker_identity_source = worker_identity_source
    app.state.recent_failures = SlidingWindowCounter()
    app.state.templates = templates_registry or TemplateRegistry(
        platform_root / "_templates"
    )
    # F11 step 4: every limiter below is named and shares its window through
    # the deployment's Redis when there is one. Without a name they would share
    # a key namespace and two limits would spend each other's budget; without
    # Redis they keep the per-process window they always had.
    def _limiter(name: str, limit: int) -> SlidingWindowRateLimiter:
        return SlidingWindowRateLimiter(
            limit, name=name, redis_client=redis_client
        )

    app.state.create_limiter = _limiter("create", settings.create_rate_limit_per_min)
    # E3.5: template build admission. The slot counter bounds concurrent
    # buildkit builds (the actual CPU/disk consumer); the per-key limiter
    # additionally throttles serial build bombardment. Both are per-process
    # (same shape as the create limiter; single control-plane deployment).
    app.state.template_build_slots = 0
    app.state.template_build_slots_lock = threading.Lock()
    app.state.template_build_limiter = _limiter(
        "template-build", settings.template_build_rate_limit_per_min
    )
    app.state.tenant_create_limiter = _limiter(
        "tenant-create", settings.create_rate_limit_per_min
    )
    # Same admission shape for the other resource-creating endpoints. Per
    # endpoint, so a snapshot burst cannot spend the sandbox-create budget.
    app.state.snapshot_limiter = _limiter(
        "snapshot", settings.snapshot_rate_limit_per_min
    )
    app.state.volume_limiter = _limiter("volume", settings.volume_rate_limit_per_min)
    app.state.tenant_snapshot_limiter = _limiter(
        "tenant-snapshot", settings.snapshot_rate_limit_per_min
    )
    app.state.tenant_volume_limiter = _limiter(
        "tenant-volume", settings.volume_rate_limit_per_min
    )
    app.state.tenant_limiters = {
        tenant_id: _limiter(f"tenant:{tenant_id}", limit)
        for tenant_id, limit in settings.tenant_rate_limits.items()
    }
    if settings.enable_local_node and app.state.nodes.get("local") is None:
        app.state.nodes.add_local_node(
            total_memory_mb=settings.max_total_memory_mb,
            total_cpu_percent=settings.max_total_cpu_percent,
            total_disk_mb=settings.max_total_disk_mb,
            total_processes=settings.max_total_processes,
        )
    app.state.select_node = app.state.nodes.select_and_reserve
    # The autoscaler, when this deployment hosts it (the k8s shape): built
    # here, started as a task by the lifespan below. `E2B_AS_ENABLED` is the
    # switch -- a deployment that runs no autoscaler (a single-host compose
    # stack, a test) leaves it off and never reaches for the k8s API.
    app.state.autoscaler = None
    app.state.autoscaler_task = None
    if settings.autoscaler_enabled:
        app.state.autoscaler = build_autoscaler_service(
            app,
            backend=None if autoscaler_backend is _UNSET else autoscaler_backend,
        )
    app.add_exception_handler(OfficialError, official_error_handler)
    # Snapshots first so DELETE /templates/{snapshotID} wins over the
    # templates catch-all in the sandboxes router.
    app.include_router(snapshots_router)
    app.include_router(templates_router)
    app.include_router(sandboxes_router)
    app.include_router(volumes_router)
    app.include_router(secrets_router)
    app.include_router(nodes_router)
    app.include_router(internal_router)
    return app
