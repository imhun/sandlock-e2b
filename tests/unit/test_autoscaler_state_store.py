"""The loop's marks live outside the loop.

Since 2026-09-30 the autoscaler runs *inside* the control plane (k8s path),
which means one loop per control-plane replica and a loop that restarts on
every control-plane rollout. Cooldowns and "which node am I draining" are the
only state the loop has, and both are decisions about the *fleet*, not about
the process: a replica that has never ticked must still know that its peer
scaled up 10 s ago, and a restarted loop must still remember the drain it
started. That state therefore has to survive the process, which is what this
store is. Without Redis there is one process, and the in-memory store is the
whole fleet.
"""

from __future__ import annotations

from autoscaler.state import InMemoryLoopState, LoopMarks, RedisLoopState


class _FakeRedis:
    """``RedisLoopState``'s two commands, with redis-py's call shape.

    Real redis-py is not a test dependency of this repo (the control plane
    imports it lazily, only when ``E2B_REDIS_URL`` is set), so the store is
    exercised against the two calls it actually makes. ``fail`` flips both to
    raising, which is the outage the degraded path is about.
    """

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail = False

    def hgetall(self, key: str) -> dict[str, str]:
        if self.fail:
            raise ConnectionError("redis is down")
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, mapping: dict[str, str]) -> int:
        if self.fail:
            raise ConnectionError("redis is down")
        self.hashes.setdefault(key, {}).update(mapping)
        return len(mapping)


def test_a_fresh_loop_has_no_marks() -> None:
    marks = InMemoryLoopState().read()
    assert marks.last_scale_up == float("-inf")
    assert marks.last_scale_down == float("-inf")
    assert marks.draining_node_id is None


def test_in_memory_marks_are_read_back_after_a_write() -> None:
    store = InMemoryLoopState()
    before = store.read()
    after = LoopMarks(
        last_scale_up=100.0, last_scale_down=50.0, draining_node_id="n1"
    )
    store.write(before, after)

    marks = store.read()
    assert (marks.last_scale_up, marks.last_scale_down, marks.draining_node_id) == (
        100.0,
        50.0,
        "n1",
    )
    # A copy comes back, so a caller's later mutation is not written by
    # accident (the loop mutates its own marks object between read and write).
    marks.draining_node_id = "n2"
    assert store.read().draining_node_id == "n1"


def test_a_write_only_touches_the_fields_that_tick_changed() -> None:
    """A stale tick must not undo what a peer did while it was working.

    Measured on the cluster (2026-09-30, second acceptance cycle): two replicas
    ticked across the same rollout, replica B's read was from *before* replica
    A's scale-up, and B -- which had drained the node A had just created --
    wrote its whole snapshot back, resurrecting ``last_scale_up = -inf``. The
    cooldown the store exists to share disappeared with it. The fix is the
    signature: a tick hands over what it read and what it did, and only the
    difference is stored.
    """
    store = InMemoryLoopState()
    before = store.read()  # what THIS tick read: nothing had happened yet
    # ...meanwhile a peer replica scales up and publishes its mark.
    peer = store.read()
    peer.last_scale_up = 100.0
    store.write(before, peer)
    # Now this tick writes its own (different) action. Its `last_scale_up` is
    # still the stale -inf it read -- which is the point: it never scaled up,
    # so it must not publish that field at all.
    after = LoopMarks(last_scale_up=float("-inf"), last_scale_down=50.0)
    store.write(before, after)

    marks = store.read()
    assert (marks.last_scale_up, marks.last_scale_down) == (100.0, 50.0)


def test_redis_marks_round_trip_through_a_second_store() -> None:
    client = _FakeRedis()
    store = RedisLoopState(client)
    store.write(
        store.read(),
        LoopMarks(last_scale_up=1.5, last_scale_down=2.5, draining_node_id="n1"),
    )

    peer = RedisLoopState(client).read()
    assert peer == LoopMarks(
        last_scale_up=1.5, last_scale_down=2.5, draining_node_id="n1"
    )


def test_redis_write_sends_only_the_changed_fields() -> None:
    """The hash is shared, so the command must be a partial HSET.

    Same reasoning as the in-memory case above, plus one that is specific to
    Redis: writing all three fields would also make every no-op tick a
    read-modify-write over its peers' marks.
    """
    client = _FakeRedis()
    store = RedisLoopState(client)
    before = store.read()
    peer_after = LoopMarks(last_scale_up=100.0)
    store.write(before, peer_after)

    writes: list[dict[str, str]] = []
    real_hset = client.hset

    def recording_hset(key: str, mapping: dict[str, str]) -> int:
        writes.append(dict(mapping))
        return real_hset(key, mapping)

    client.hset = recording_hset  # type: ignore[method-assign]
    store.write(before, LoopMarks(last_scale_up=float("-inf"), last_scale_down=50.0))

    assert writes == [{"last_scale_down": "50.0"}]
    assert store.read().last_scale_up == 100.0


def test_redis_marks_read_defaults_when_nothing_was_written() -> None:
    assert RedisLoopState(_FakeRedis()).read() == LoopMarks()


def test_a_redis_outage_degrades_instead_of_failing_the_tick() -> None:
    """The store is a way to agree between replicas, not a new failure mode.

    ``try_claim`` takes the same posture for the same reason: an unreachable
    store must not stop the autoscaler from scaling, so a read falls back to
    "no marks" and a write is reported and dropped.
    """
    client = _FakeRedis()
    store = RedisLoopState(client)
    client.fail = True

    assert store.read() == LoopMarks()
    store.write(store.read(), LoopMarks(last_scale_up=9.0))

    client.fail = False
    # The write was dropped, not partially applied.
    assert store.read() == LoopMarks()
