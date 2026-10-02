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

Two more pin the two boundaries the refactor into ``rank_candidates`` could
quietly have moved: an empty candidate set, and the volume-pinned node (it must
lead the ranking *and* still be the exact object ``pick_best`` hands back).
The pin is also why there is a *second* exception: a pinned placement must not
hand over to another node at all, so the last case pins that 503.

The fleet used here is two *identical* nodes, which leaves the ranking a tie;
the sort is stable, so the candidate order is registration order and
``node_a`` is the first candidate.
"""

from __future__ import annotations

import logging

from control_plane.registry.nodes import NodeRegistry
from control_plane.scheduler import pick_best, rank_candidates

_DIMS = {"memory_mb": 512, "cpu_percent": 100, "disk_mb": 1024, "processes": 64}


class _RefusingStore:
    """A duck-typed ``RedisQuotaStore`` that refuses named nodes.

    ``NodeRegistry.__init__`` leaves ``_quota_store`` as ``None`` without a
    redis client, and this lane's cases are about *which* candidate gets the
    reservation rather than about Redis. So the one behaviour under test --
    ``reserve`` answering False for a given node -- is injected directly; the
    other three methods are no-ops so registration and release keep working.
    ``attempts`` is what the placement asked for, in order, which is how the
    cases see that a candidate was skipped *and* that the next one was tried.
    """

    def __init__(self, *refusing: str) -> None:
        self.refusing = set(refusing)
        self.attempts: list[str] = []

    def reserve(
        self, name: str, limits: dict[str, int], dims: dict[str, int]
    ) -> bool:
        self.attempts.append(name)
        if name in self.refusing:
            return False
        return True

    def get(self, name: str) -> dict[str, int]:
        return {}

    def reconcile(self, name: str, dims: dict[str, int]) -> dict[str, int]:
        return {}

    def release(self, name: str, dims: dict[str, int]) -> None:
        return None


class _RecordingView:
    """A duck-typed ``RedisNodeStore`` that only remembers what was published.

    ``get``/``list`` answer "nothing is stored here", so a placement reads this
    replica's own rows; ``put`` is the half the *other* replica would see, and
    that is what these cases watch.
    """

    def __init__(self) -> None:
        self.published: list[str] = []

    def put(self, node_id: str, payload: dict, *, ttl: int | None = None) -> None:
        self.published.append(node_id)

    def get(self, node_id: str) -> dict | None:
        return None

    def list(self) -> list[dict]:
        return []

    def delete(self, node_id: str) -> None:
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


def test_a_refused_candidate_hands_the_placement_to_the_next_one(caplog) -> None:
    """The store refuses ``node_a``; ``node_b`` is empty and takes it."""
    nodes = _fleet("node_a", "node_b")
    nodes._quota_store = _RefusingStore("node_a")

    with caplog.at_level(logging.WARNING):
        picked = _place(nodes)

    assert picked is not None
    assert picked.node_id == "node_b"
    assert picked.reserved_memory_mb == _DIMS["memory_mb"]
    assert nodes.get("node_a").reserved_memory_mb == 0
    assert nodes._quota_store.attempts == ["node_a", "node_b"]
    # The hand-over works, so the "every candidate" line must not be here: it
    # belongs to an exhausted candidate set only.
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == [
        "quota store refused node node_a for memory=512 cpu=100 disk=1024 "
        "processes=64; trying the next candidate (1 left)"
    ]


def test_the_skipped_candidate_is_not_published_to_the_shared_view() -> None:
    """The hand-over is in the shared view too: only the winner is published.

    ``_persist_locked`` is the half the *other* replica reads, so a candidate
    that was refused (and therefore never reserved) must not appear there as if
    it had taken the sandbox. This is the assertion the hand-over case above
    does not make -- the refusal itself is pinned there, verbatim.
    """
    nodes = _fleet("node_a", "node_b")
    nodes._quota_store = _RefusingStore("node_a")
    view = _RecordingView()
    nodes._view = view

    picked = _place(nodes)

    assert picked.node_id == "node_b"
    assert view.published == ["node_b"]


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


def test_a_pinned_placement_does_not_fall_back_off_the_pinned_node(caplog) -> None:
    """A volume pin is a requirement: no second candidate, and the 503 is named.

    The caller pins the node the snapshot or the non-shared volume lives on
    (``migrate`` pins a target for the same reason), so handing the sandbox to
    another node would put it away from its data. ``node_b`` is empty and would
    fit -- the point is that it is *not* tried.
    """
    nodes = _fleet("node_a", "node_b")
    store = _RefusingStore("node_a")
    nodes._quota_store = store

    with caplog.at_level(logging.WARNING):
        picked = nodes.select_and_reserve(
            base_image=None, volume_node_id="node_a", **_DIMS
        )

    assert picked is None
    assert store.attempts == ["node_a"]
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == [
        "quota store refused node node_a for memory=512 cpu=100 disk=1024 "
        "processes=64; this placement is pinned to volume node node_a, so no "
        "other candidate is tried and it answers 503"
    ]
    assert nodes.get("node_a").reserved_memory_mb == 0
    assert nodes.get("node_b").reserved_memory_mb == 0


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


def test_an_empty_candidate_set_ranks_to_nothing() -> None:
    """No candidates at all, and no capable one, both rank to ``[]``.

    ``rank_candidates`` answering ``[]`` is what keeps ``pick_best``'s ``None``
    (and therefore ``select_node``'s) exactly where it was: a fleet that cannot
    fit must stay a refusal, not become an ``IndexError`` or a stray element.
    """
    nodes = _fleet("node_a", "node_b")
    for node in nodes.list():
        node.reserve(8192, 800, 16384, 512)  # nothing fits any more

    assert rank_candidates([], base_image=None, **_DIMS) == []
    assert rank_candidates(nodes.list(), base_image=None, **_DIMS) == []
    assert pick_best([], base_image=None, **_DIMS) is None
    assert pick_best(nodes.list(), base_image=None, **_DIMS) is None


def test_a_volume_pinned_node_leads_even_when_it_scores_worse() -> None:
    """The pinned node is ``[0]``, and it is the same object ``pick_best`` returns.

    ``node_a`` outranks ``node_b`` on image affinity here, so a ranking that
    forgot the pin (or re-sorted it away) would hand back ``node_a`` -- and a
    pin rebuilt by copying the record instead of moving it would hand back an
    equal-but-different object, which the reservation then writes into the
    wrong place.
    """
    nodes = _fleet("node_a", "node_b")
    nodes.get("node_a").images = ["python:3.11-slim"]
    candidates = nodes.list()

    ranked = rank_candidates(
        candidates,
        base_image="python:3.11-slim",
        volume_node_id="node_b",
        **_DIMS,
    )
    picked = pick_best(
        candidates,
        base_image="python:3.11-slim",
        volume_node_id="node_b",
        **_DIMS,
    )

    assert [node.node_id for node in ranked] == ["node_b", "node_a"]
    assert ranked[0] is nodes.get("node_b")
    assert picked is nodes.get("node_b")

    # ...and the pin *moves* the record instead of adding a second copy of it:
    # a ranking that lists the pinned node twice would reserve the same node
    # twice for one sandbox.
    twins = _fleet("node_a", "node_b", "node_c")
    twin_ranked = rank_candidates(
        twins.list(), base_image=None, volume_node_id="node_b", **_DIMS
    )

    assert [node.node_id for node in twin_ranked] == ["node_b", "node_a", "node_c"]
    assert twin_ranked[0] is twins.get("node_b")
    assert sorted(id(node) for node in twin_ranked) == sorted(
        id(node) for node in twins.list()
    )


def test_identical_candidates_keep_the_stable_first_maximum() -> None:
    """A tie keeps the caller's order, and both entry points agree on it.

    ``pick_best`` used to be a plain ``max``, which returns the *first* maximal
    element; ``rank_candidates`` is a stable ``sorted``. The two agree only
    while the tie-break is stable, and that stability is what keeps placement
    bit-for-bit identical on a fleet of equal nodes -- so it is pinned here in
    the repo rather than left to a scratch script.
    """
    nodes = _fleet("node_a", "node_b", "node_c")
    candidates = nodes.list()

    ranked = rank_candidates(candidates, base_image=None, **_DIMS)
    picked = pick_best(candidates, base_image=None, **_DIMS)

    assert [node.node_id for node in ranked] == ["node_a", "node_b", "node_c"]
    assert [id(node) for node in ranked] == [id(node) for node in candidates]
    assert picked is candidates[0]
