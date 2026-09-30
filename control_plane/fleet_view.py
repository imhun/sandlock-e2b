"""The fleet's record surfaces, named once (C3 Task 6).

The worker's own reconcile compared two *surfaces* before it deleted anything
(Task 4's reviews pinned the discipline): the fleet-scope id enumeration
(``GET /internal/fleet/sandboxes``) and the fleet-wide record count
(``GET /internal/fleet/metrics`` → ``activeSandboxes``). The second read is what
catches an enumeration that came back short -- the whole reason the shape is
two reads rather than one.

The self-heal sweep (``control_plane/self_heal.py``) keeps that discipline,
now on the side that holds the authority, so the number it compares against has
one definition and one owner:

* the **enumeration** is :meth:`SandboxRegistry.fleet_id_snapshot` -- the ids
  plus "what the store could not answer" (``unreadable``), because a short
  answer has to be distinguishable from a complete one;
* the **count** is :func:`active_sandbox_count` here, the same expression
  ``/internal/fleet/metrics`` reports as ``activeSandboxes``.

These are two reads of the same store, and that is the point: a record created
or removed between them is a race, and a race is a reason to defer rather than
to delete the tree of a sandbox that was just registered.

Since 2026-09-30 the same discipline covers the autoscaler. It used to be a
Deployment that read ``GET /internal/fleet/metrics`` over HTTP and asked for a
drain through ``POST /internal/nodes/{id}/drain``; the k8s path now hosts the
loop *inside* this app (``control_plane/autoscaler_service.py``), and it calls
the two functions below -- the ones those handlers call. So "the fleet the
autoscaler sees" and "the fleet the API serves" are one computation with two
readers, rather than two endpoints that have to agree.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def active_sandbox_count(state: Any) -> int:
    """The count ``GET /internal/fleet/metrics`` reports as ``activeSandboxes``.

    Kept identical to that handler's expression ``len(registry.list())`` on
    purpose (``tests/unit/test_c3_self_heal_sweep.py`` pins the agreement): the
    self-heal sweep has to compare against *the* fleet-wide record count, not
    against a second definition of it that could drift.
    """
    return len(state.registry.list())


def fleet_metrics_payload(state: Any) -> dict[str, Any]:
    """The fleet view ``GET /internal/fleet/metrics`` serves.

    Per-node utilization and active sandboxes, the fleet aggregates, the
    remaining standard-sandbox capacity, and the recent-503 count -- the
    autoscaler's whole input. Both readers call this: the endpoint
    (``control_plane/api/internal.py``) and the in-process loop, which is why
    a drift between "what the API reports" and "what the autoscaler acts on"
    is not a failure mode this deployment has.

    Reads only; every number is derived from the node registry, the sandbox
    registry and the request-failure window on ``state``.
    """
    settings = state.settings
    nodes = state.nodes.list()
    registry = state.registry
    records = registry.list()
    active_by_node: Counter[str] = Counter(r.node_id for r in records)

    dims = {
        "memory": ("reserved_memory_mb", "total_memory_mb", settings.default_memory_mb),
        "cpu": ("reserved_cpu_percent", "total_cpu_percent", settings.default_cpu_percent),
        "disk": ("reserved_disk_mb", "total_disk_mb", settings.default_disk_mb),
        "processes": (
            "reserved_processes",
            "total_processes",
            settings.default_max_processes,
        ),
    }

    node_metrics: list[dict[str, Any]] = []
    fleet_totals = {key: {"reserved": 0, "total": 0} for key in dims}
    remaining_capacity: int | None = 0
    unlimited_node = False
    for node in nodes:
        per_node: dict[str, Any] = {}
        node_remaining: int | None = None
        for key, (reserved_attr, total_attr, demand) in dims.items():
            reserved = getattr(node, reserved_attr)
            total = getattr(node, total_attr)
            fleet_totals[key]["reserved"] += reserved
            fleet_totals[key]["total"] += total
            utilization = (reserved / total) if total > 0 else 0.0
            per_node[key] = {
                "reserved": reserved,
                "total": total,
                "utilization": round(utilization, 4),
            }
            if total > 0 and demand > 0:
                candidate = max(0, (total - reserved) // demand)
                node_remaining = (
                    candidate
                    if node_remaining is None
                    else min(node_remaining, candidate)
                )
        if node_remaining is None:
            unlimited_node = True
        elif remaining_capacity is not None:
            remaining_capacity += node_remaining
        node_metrics.append(
            {
                "nodeID": node.node_id,
                "status": node.status,
                "draining": node.draining,
                "images": node.images,
                "activeSandboxes": active_by_node.get(node.node_id, 0),
                "utilization": per_node,
            }
        )

    fleet: dict[str, Any] = {}
    for key, totals in fleet_totals.items():
        fleet[key] = {
            "reserved": totals["reserved"],
            "total": totals["total"],
            "utilization": round(
                (totals["reserved"] / totals["total"])
                if totals["total"] > 0
                else 0.0,
                4,
            ),
        }
    # The *fleet-wide* ledger, next to the per-node budgets above. They answer
    # different questions: the per-node numbers say how much each worker has
    # promised, this one says how much the deployment has sold in total -- and
    # on a **shared** workspace that second number is the one with a real
    # ceiling (`E2B_MAX_TOTAL_DISK_MB`, the slice's size). It is also the only
    # honest disk signal here: `usedDiskMB`/`diskTotalMB` in the node view are
    # the whole NAS filesystem (measured 10 PiB against a 50 GiB claim), so the
    # percent thresholds derived from them can never fire.
    global_ledger = registry.global_reserved()
    disk_limit = int(getattr(settings, "max_total_disk_mb", 0) or 0)
    disk_reserved = int(global_ledger.get("disk", 0))
    workspace_disk = {
        "reservedMB": disk_reserved,
        "limitMB": disk_limit,
        "warn": bool(disk_limit and disk_reserved >= 0.85 * disk_limit),
        "saturated": bool(disk_limit and disk_reserved >= disk_limit),
    }
    # N25: who is *over* their own budget right now, and by how much. The write
    # side is enforced inside the sandbox (zero ceiling + `ENOSPC` for new
    # names) and deliberately does not freeze anyone, so this is the number an
    # operator or an alert watches instead of a log line.
    workspace_disk.update(registry.disk_overrun_stats())
    return {
        "nodes": node_metrics,
        "fleet": fleet,
        "workspaceDisk": workspace_disk,
        "standardSandboxDims": {
            "memory": settings.default_memory_mb,
            "cpu": settings.default_cpu_percent,
            "disk": settings.default_disk_mb,
            "processes": settings.default_max_processes,
        },
        "remainingSandboxCapacity": (
            None if unlimited_node else remaining_capacity
        ),
        "activeSandboxes": len(records),
        "recent503Count": state.recent_failures.count(),
    }


def drain_node(state: Any, node_id: str) -> dict[str, Any] | None:
    """Mark ``node_id`` draining and report how many sandboxes it still holds.

    ``None`` means "no such node" -- the one branch the caller has to answer
    differently (the HTTP surface turns it into a 404, the in-process
    autoscaler refuses the whole tick), so it is the return value rather than a
    raised error.

    The count comes from the same records ``fleet_metrics_payload`` counts, and
    it is what the autoscaler's scale-down uses to decide between "retire now"
    and "wait for the node to empty".
    """
    record = state.nodes.set_draining(node_id, True)
    if record is None:
        return None
    active = sum(1 for r in state.registry.list() if r.node_id == node_id)
    return {"nodeID": node_id, "activeSandboxes": active, "draining": True}
