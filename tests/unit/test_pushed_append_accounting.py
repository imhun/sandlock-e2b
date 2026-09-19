"""N25: pushed append bytes accelerate the report, and only in one direction.

The mediator watches the sandbox's own open write descriptors and pushes what
they grew by (`docs/k8s-deployment.md` §22.5). The worker's contract with that
number is narrow on purpose:

* the round reports ``walked + appended`` and *consumes* the appended bytes,
  because the walked number already contains everything committed up to that
  moment;
* the per-exec ceiling *peeks* instead, so the same bytes are still there for
  the round;
* nothing here may ever make the number smaller than the walk said.

The last one is the safety property: on this storage the filesystem answer can
lag seconds behind a writer, so the pushed bytes are the only early signal --
and a signal that could *lower* a size would let a runaway sandbox argue its
way back under its budget.
"""

from __future__ import annotations

from pathlib import Path

from envd_service.runtime.registry import RuntimeRegistry


def _registry(base: Path, sandbox_id: str, *, disk_mb: int = 1024) -> RuntimeRegistry:
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
    return registry


def test_a_round_reports_the_walk_plus_what_was_appended(tmp_path):
    registry = _registry(tmp_path, "sbx_pushed")
    registry.note_appended("sbx_pushed", 4096)

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_pushed": 1000 + 4096}


def test_the_appended_bytes_are_consumed_by_the_round_that_reported_them(tmp_path):
    registry = _registry(tmp_path, "sbx_consumed")
    registry.note_appended("sbx_consumed", 4096)
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_consumed": 5096}

    # Second round: the same bytes must not be added twice (the walk now sees
    # whatever committed, and the accumulator is empty).
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_consumed": 1000}


def test_the_ceiling_refresh_peeks_so_the_round_can_still_take_them(tmp_path):
    registry = _registry(tmp_path, "sbx_peek")
    registry.note_appended("sbx_peek", 2048)

    assert registry.refresh_disk_usage("sbx_peek") == 1000 + 2048
    # Peeking does not consume: the round still adds them.
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_peek": 3048}


def test_appends_for_an_unknown_sandbox_are_ignored_by_the_round(tmp_path):
    registry = _registry(tmp_path, "sbx_known")
    registry.note_appended("sbx_other", 8192)

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_known": 1000}


def test_a_non_positive_append_is_not_counted(tmp_path):
    registry = _registry(tmp_path, "sbx_zero")
    registry.note_appended("sbx_zero", 0)
    registry.note_appended("sbx_zero", -5)

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_zero": 1000}


def test_appends_accumulate_until_a_round_takes_them(tmp_path):
    registry = _registry(tmp_path, "sbx_sum")
    registry.note_appended("sbx_sum", 1)
    registry.note_appended("sbx_sum", 2)
    registry.note_appended("sbx_sum", 3)

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_sum": 1006}


def test_unregistering_drops_the_accumulator(tmp_path):
    registry = _registry(tmp_path, "sbx_gone")
    registry.note_appended("sbx_gone", 4096)
    registry.unregister("sbx_gone")
    registry.register(
        sandbox_id="sbx_gone",
        access_token="t",
        workspace_dir=str(tmp_path / "sbx_gone"),
        disk_mb=1024,
    )

    # A re-created id must not inherit the previous incarnation's bytes.
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_gone": 1000}


def test_a_crossing_seen_only_through_pushed_bytes_is_still_a_crossing(tmp_path):
    """The point: the crossing must be reportable *now*.

    The tree on disk is 1 kB and the filesystem says so; the mediator says the
    sandbox has appended 3 MiB. With a 2 MiB budget the sandbox is over, and
    the immediate-report path has to see it even though nothing on disk has
    changed yet.
    """
    registry = _registry(tmp_path, "sbx_crossed", disk_mb=2)
    registry.note_appended("sbx_crossed", 3 * 1024 * 1024)

    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_crossed": 1000 + 3 * 1024 * 1024
    }
    assert registry.take_budget_crossings() == {"sbx_crossed": 1000 + 3 * 1024 * 1024}
