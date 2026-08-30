"""Node selection: filter capable nodes, then score by affinity and balance."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from control_plane.registry.nodes import NodeRecord


def select_node(
    nodes: list[NodeRecord],
    *,
    base_image: str | None,
    volume_node_id: str | None = None,
    memory_mb: int,
    cpu_percent: int,
    disk_mb: int,
    processes: int,
) -> NodeRecord | None:
    """Pick the best node or ``None`` when no node can fit the sandbox."""
    return pick_best(
        [n for n in nodes if n.status == "healthy" and not n.draining],
        base_image=base_image,
        volume_node_id=volume_node_id,
        memory_mb=memory_mb,
        cpu_percent=cpu_percent,
        disk_mb=disk_mb,
        processes=processes,
    )


def pick_best(
    candidates: list[NodeRecord],
    *,
    base_image: str | None,
    volume_node_id: str | None = None,
    memory_mb: int,
    cpu_percent: int,
    disk_mb: int,
    processes: int,
) -> NodeRecord | None:
    """Score capable candidates without mutating them.

    Capacity checks and reservations are the caller's job (the node registry
    does them atomically under its lock); this function only picks.
    """
    candidates = [
        n
        for n in candidates
        if n.can_fit(memory_mb, cpu_percent, disk_mb, processes)
    ]
    if not candidates:
        return None
    if volume_node_id:
        volume_node = next(
            (n for n in candidates if n.node_id == volume_node_id), None
        )
        if volume_node is not None:
            return volume_node
    # Score: image affinity > remaining capacity > balance.
    best = max(
        candidates,
        key=lambda n: (
            _image_affinity(n, base_image),
            _remaining_ratio(n),
            -len(getattr(n, "labels", {}) or {}),  # stable tie-break by labels
        ),
    )
    return best


def _image_affinity(node: NodeRecord, base_image: str | None) -> float:
    if not base_image:
        return 30.0  # pure Sandlock: no image resolution needed anywhere
    for cached in node.images:
        if cached == base_image:
            return 30.0
    return 0.0


def _remaining_ratio(node: NodeRecord) -> float:
    """Fraction of the most contended dimension still available (0..1)."""
    ratios: list[float] = []
    if node.total_memory_mb > 0:
        ratios.append(
            (node.total_memory_mb - node.reserved_memory_mb) / node.total_memory_mb
        )
    if node.total_cpu_percent > 0:
        ratios.append(
            (node.total_cpu_percent - node.reserved_cpu_percent)
            / node.total_cpu_percent
        )
    if node.total_disk_mb > 0:
        ratios.append(
            (node.total_disk_mb - node.reserved_disk_mb) / node.total_disk_mb
        )
    if node.total_processes > 0:
        ratios.append(
            (node.total_processes - node.reserved_processes) / node.total_processes
        )
    if not ratios:
        return 1.0
    return min(ratios)
