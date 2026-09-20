"""N25: the registry's live tightening, and its three restraints.

The pause gate is the slow half of enforcement (the control plane only knows
what a heartbeat told it). Tightening the *running* process's file-size limit
is the fast half: the kernel refuses the next write past what is left, in the
process that is writing. What these tests pin is that the fast half can never
make things worse than the slow half would have:

* it is only ever asked to *lower* a limit;
* it is not asked on every round (a byte of movement is not news);
* it is not asked at all without a tightener or a budget.
"""

from __future__ import annotations

from pathlib import Path

from envd_service.runtime.registry import RuntimeRegistry


def _registry(base: Path, sandbox_id: str, *, disk_mb: int = 100) -> RuntimeRegistry:
    registry = RuntimeRegistry(base)
    registry.register(
        sandbox_id=sandbox_id,
        access_token="t",
        workspace_dir=str(base / sandbox_id),
        disk_mb=disk_mb,
    )
    tree = base / sandbox_id
    (tree / "workspace").mkdir(parents=True)
    (tree / "workspace" / "a.bin").write_bytes(b"a" * 1000)
    registry.set_dirty_provider(lambda sandbox_id: ([str(tree / "workspace")], False))
    # No sleeping in tests: the interval is exercised by its own test below.
    registry._tighten_interval_s = 0.0
    return registry


def _recorder():
    calls: list[tuple[str, int]] = []
    stamps_seen: list[tuple[int, int] | None] = []

    def tightener(sandbox_id: str, bytes_: int, stamps=None):
        # N25: the third argument is the walk's own date (the mediator's
        # `(spent, freed)` counters, read before the walk). Kept out of `calls`
        # so every existing assertion stays about the number.
        calls.append((sandbox_id, bytes_))
        stamps_seen.append(stamps)
        return {"applied_bytes": bytes_}

    # Attached rather than returned, so the dozen call sites that unpack two
    # values keep working and every existing assertion stays about the number.
    tightener.stamps_seen = stamps_seen
    return calls, tightener


def test_a_round_dates_its_walk_with_the_counter_provider(tmp_path):
    # N25: the number the round sends has to say *when* it was true. The
    # provider is asked before the walk, and what it answers travels with the
    # budget, so the mediator can subtract everything since -- without it, the
    # gap between the walk and its arrival was handed out as free space
    # (measured: 48 MiB past the budget).
    registry = _registry(tmp_path, "sbx_dated")
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    registry.set_counter_provider(lambda sandbox_id: (11, 22))

    registry.disk_usage_snapshot(dirty=True)

    assert calls, "the round must have tightened"
    assert tightener.stamps_seen == [(11, 22)] * len(calls)


def test_a_round_without_a_counter_provider_sends_no_date(tmp_path):
    registry = _registry(tmp_path, "sbx_undated")
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)

    registry.disk_usage_snapshot(dirty=True)

    assert calls, "the round must have tightened"
    assert tightener.stamps_seen == [None] * len(calls)


def test_a_round_that_drops_the_remaining_budget_tightens(tmp_path):
    registry = _registry(tmp_path, "sbx_tighten", disk_mb=100)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    # 100 MiB budget, 1 kB used: the remaining budget is sent as the ceiling.
    registry.disk_usage_snapshot(dirty=True)

    assert calls == [("sbx_tighten", 100 * 1024 * 1024 - 1000)]


def test_nothing_is_sent_twice_for_the_same_remaining(tmp_path):
    registry = _registry(tmp_path, "sbx_same", disk_mb=1)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)

    registry.disk_usage_snapshot(dirty=True)
    registry.disk_usage_snapshot(dirty=True)
    registry.disk_usage_snapshot(dirty=True)

    assert len(calls) == 1


def test_a_small_drop_is_not_worth_a_verb(tmp_path):
    registry = _registry(tmp_path, "sbx_small", disk_mb=100)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    registry.disk_usage_snapshot(dirty=True)

    # 1 kB more used: far below the material step, so nothing is sent.
    (tmp_path / "sbx_small" / "workspace" / "b.bin").write_bytes(b"b" * 1000)
    registry.disk_usage_snapshot(dirty=True)

    assert len(calls) == 1


def test_a_material_drop_is_sent(tmp_path):
    registry = _registry(tmp_path, "sbx_material", disk_mb=100)
    registry._tighten_step_bytes = 4096
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    registry.disk_usage_snapshot(dirty=True)

    (tmp_path / "sbx_material" / "workspace" / "big.bin").write_bytes(b"b" * 65536)
    registry.disk_usage_snapshot(dirty=True)

    assert [c[1] for c in calls] == [
        100 * 1024 * 1024 - 1000,
        100 * 1024 * 1024 - 66536,
    ]
    assert calls[1][1] < calls[0][1]


def test_the_interval_gates_a_second_tightening(tmp_path):
    registry = _registry(tmp_path, "sbx_interval", disk_mb=100)
    registry._tighten_step_bytes = 1
    registry._tighten_interval_s = 3600.0  # never elapses inside the test
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)

    registry.disk_usage_snapshot(dirty=True)
    (tmp_path / "sbx_interval" / "workspace" / "b.bin").write_bytes(b"b" * 65536)
    registry.disk_usage_snapshot(dirty=True)

    assert len(calls) == 1


def test_a_budget_rise_below_the_step_is_not_sent(tmp_path):
    """A move that changes no decision is not worth a verb.

    The step exists so a round that moved the number by a few kilobytes does
    not talk to the slot. 1 MB of room in a 100 MiB budget changes no grant.
    """
    registry = _registry(tmp_path, "sbx_shrank", disk_mb=100)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    (tmp_path / "sbx_shrank" / "workspace" / "big.bin").write_bytes(b"b" * 1_000_000)
    registry.disk_usage_snapshot(dirty=True)
    # The fixture's own 1000-byte file is in the tree too.
    assert calls[-1][1] == 100 * 1024 * 1024 - (1_000_000 + 1000)

    (tmp_path / "sbx_shrank" / "workspace" / "big.bin").unlink()
    registry.disk_usage_snapshot(dirty=True)

    assert calls[-1][1] == 100 * 1024 * 1024 - (1_000_000 + 1000), (
        "a sub-step rise changes no decision"
    )


def test_leaving_the_exhausted_state_is_always_sent(tmp_path):
    """The one rise that matters, however small.

    A sandbox that deleted its way back inside has to be able to write again,
    and *that* decision lives in the mediator: the budget it holds is what its
    `open` grants and its "may the tree grow?" refusals are computed from. So
    a crossing of zero is exempt from the step.
    """
    registry = _registry(tmp_path, "sbx_full", disk_mb=1)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    # The verb is rate-limited, and this test does not wait a scan interval.
    registry._tighten_interval_s = 0.0
    # The fixture's own 1000-byte file is in the tree too, so this is 2000
    # bytes *past* the 1 MiB budget: the pool is exhausted.
    (tmp_path / "sbx_full" / "workspace" / "huge.bin").write_bytes(b"b" * (1024 * 1024 + 1000))
    registry.disk_usage_snapshot(dirty=True)
    assert calls[-1][1] == 0

    # Now 100 bytes *inside* the budget: a rise of 100 bytes, far below the
    # step, but it is the difference between "may not create anything" and
    # "may".
    (tmp_path / "sbx_full" / "workspace" / "huge.bin").write_bytes(b"b" * (1024 * 1024 - 1100))
    registry.disk_usage_snapshot(dirty=True)

    assert calls[-1][1] == 100, "leaving the exhausted state is always sent"


def test_the_ceiling_reaches_zero_when_the_budget_is_gone(tmp_path):
    """Over budget the answer is *no growth*, not "one more tiny file".

    The floor that used to sit here was the one MiB that put a full sandbox
    past its budget -- where the platform then froze it, taking away the
    deletes it needed to get back inside. Zero is a real value: every write
    fails with EFBIG, and deleting still works, which is the product semantic.
    """
    registry = _registry(tmp_path, "sbx_over", disk_mb=1)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    # Already far over budget: the remaining budget is negative.
    (tmp_path / "sbx_over" / "workspace" / "huge.bin").write_bytes(b"b" * (2 * 1024 * 1024))
    registry.disk_usage_snapshot(dirty=True)

    assert calls == [("sbx_over", 0)], "no growth: the ceiling is zero"


def test_a_sandbox_inside_its_budget_keeps_what_is_left(tmp_path):
    registry = _registry(tmp_path, "sbx_inside", disk_mb=1)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    # 1000 bytes of a 1 MiB budget: the ceiling is the remaining budget.
    registry.disk_usage_snapshot(dirty=True)

    assert calls == [("sbx_inside", 1024 * 1024 - 1000)]


def test_without_a_tightener_nothing_is_asked(tmp_path):
    registry = _registry(tmp_path, "sbx_none", disk_mb=1)
    # No `set_disk_tightener` call at all: the round must still work.
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_none": 1000}


def test_a_sandbox_with_no_budget_is_not_tightened(tmp_path):
    registry = _registry(tmp_path, "sbx_unbounded", disk_mb=0)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)

    registry.disk_usage_snapshot(dirty=True)

    assert calls == [], "0 means 'no budget', not 'a budget of nothing'"


def test_a_tightener_that_raises_does_not_break_the_round(tmp_path):
    registry = _registry(tmp_path, "sbx_boom", disk_mb=1)

    def explode(sandbox_id: str, bytes_: int):
        raise RuntimeError("slot gone")

    registry.set_disk_tightener(explode)

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_boom": 1000}
