"""N60: one node's full ledger must not 503 the whole fleet.

The live shape (2026-10-02, ``docs/deploy-clusters.md`` §7.33.4): worker-0's
shared quota ledger had drifted high, ``select_and_reserve`` ranked it first,
the store refused that one candidate and the function gave up -- so every
create answered ``503 No resources available`` while worker-1 sat empty. The
fix is to walk the ranked candidates in order and only give up when the list
runs out, with one named WARNING per skip so the decision stays visible.

These cases pin the hand-over itself, the visibility (node + all four
dimensions, verbatim), the exhaustion line, and the deliberate exception:
``reserve_node`` names its target, so it must not shop around.

The fleet used here is two *identical* nodes, which leaves the ranking a tie;
the sort is stable, so the candidate order is registration order and
``node_a`` is the first candidate.
"""

from __future__ import annotations

import logging

from control_plane.registry.nodes import NodeRegistry

_DIMS = {"memory_mb": 512, "cpu_percent": 100, "disk_mb": 1024, "processes": 64}


class _RefusingStore:
    """A duck-typed ``RedisQuotaStore`` that refuses named nodes.

    ``NodeRegistry.__init__`` leaves ``_quota_store`` as ``None`` without a
    redis client, and this lane's cases are about *which* candidate gets the
    reservation rather than about Redis. So the one behaviour under test --
    ``reserve`` answering False for a given node -- is injected directly; the
    other three methods are no-ops so registration and release keep working.
    """

    def __init__(self, *refusing: str) -> None:
        self.refusing = set(refusing)
        self.reserved: list[str] = []

    def reserve(
        self, name: str, limits: dict[str, int], dims: dict[str, int]
    ) -> bool:
        if name in self.refusing:
            return False
        self.reserved.append(name)
        return True

    def get(self, name: str) -> dict[str, int]:
        return {}

    def reconcile(self, name: str, dims: dict[str, int]) -> dict[str, int]:
        return {}

    def release(self, name: str, dims: dict[str, int]) -> None:
        return None


def _fleet(*node_ids: str) -> NodeRegistry:
    """Healthy, identical nodes registered in the order given."""
    registry = NodeRegistry(heartbeat_timeout=600.0)
    for index, node_id in enumerate(node_ids):
        registry.register(
            node_id=node_id,
            address=f"http://10.0.0.{index + 2}:49983",
            total_memory_mb=8192,
            total_cpu_percent=800,
            total_disk_mb=16384,
            total_processes=512,
        )
    return registry


def _place(nodes: NodeRegistry):
    return nodes.select_and_reserve(base_image=None, **_DIMS)


def test_a_refused_candidate_hands_the_placement_to_the_next_one() -> None:
    """The store refuses ``node_a``; ``node_b`` is empty and takes it."""
    nodes = _fleet("node_a", "node_b")
    nodes._quota_store = _RefusingStore("node_a")

    picked = _place(nodes)

    assert picked is not None
    assert picked.node_id == "node_b"
    assert picked.reserved_memory_mb == _DIMS["memory_mb"]
    assert nodes.get("node_a").reserved_memory_mb == 0


def test_the_skip_names_the_node_and_the_dimensions(caplog) -> None:
    """Every skip is one named WARNING -- node and all four dimensions."""
    nodes = _fleet("node_a", "node_b")
    nodes._quota_store = _RefusingStore("node_a")

    with caplog.at_level(logging.WARNING):
        picked = _place(nodes)

    assert picked.node_id == "node_b"
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == [
        "quota store refused node node_a for memory=512 cpu=100 disk=1024 "
        "processes=64; trying the next candidate (1 left)"
    ]
    # The refused candidate is not reserved in memory either: the skip is a
    # hand-over, not a reservation the fleet cannot see.
    assert nodes.get("node_a").reserved_memory_mb == 0
    assert nodes.get("node_a").reserved_processes == 0


def test_every_candidate_refused_still_answers_with_capacity_left_elsewhere(
    caplog,
) -> None:
    """A fleet-wide refusal is named as such, once per candidate and once at the end."""
    nodes = _fleet("node_a", "node_b")
    nodes._quota_store = _RefusingStore("node_a", "node_b")

    with caplog.at_level(logging.WARNING):
        picked = _place(nodes)

    assert picked is None
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == [
        "quota store refused node node_a for memory=512 cpu=100 disk=1024 "
        "processes=64; trying the next candidate (1 left)",
        "quota store refused node node_b for memory=512 cpu=100 disk=1024 "
        "processes=64; trying the next candidate (0 left)",
        "quota store refused every candidate for memory=512 cpu=100 disk=1024 "
        "processes=64; this placement answers 503",
    ]


def test_reserve_node_does_not_retry_a_named_target(caplog) -> None:
    """A caller that names its node gets that node or ``None``, never a substitute."""
    nodes = _fleet("node_a", "node_b")
    nodes._quota_store = _RefusingStore("node_a")

    with caplog.at_level(logging.WARNING):
        picked = nodes.reserve_node("node_a", **_DIMS)

    assert picked is None
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == []
    assert nodes.get("node_a").reserved_memory_mb == 0
    assert nodes.get("node_b").reserved_memory_mb == 0
