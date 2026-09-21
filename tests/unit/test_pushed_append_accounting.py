"""N25: pushed append bytes accelerate the report, and only in one direction.

The mediator watches the sandbox's own open write descriptors and pushes what
they grew by (`docs/k8s-deployment.md` §22.5). The worker's contract with that
number is narrow on purpose:

* the round reports ``max(walked, last_report + appended)`` and *consumes* the
  appended bytes. Adding them to the walk instead double counts every byte the
  push saw and the filesystem later committed -- measured on the cluster as
  "287 MiB written, 647 MiB reported", which is what cut a legal 900 MiB file
  with EFBIG at 696 MiB (`docs/k8s-deployment.md` §22.5.8);
* the per-exec ceiling *peeks* instead, so the same bytes are still there for
  the round;
* nothing here may ever make the number smaller than the walk said.

The last one is the safety property: on this storage the filesystem answer can
lag seconds behind a writer, so the pushed bytes are the only early signal --
and a signal that could *lower* a size would let a runaway sandbox argue its
way back under its budget.
"""

from __future__ import annotations

import os
from pathlib import Path

from envd_service.runtime.registry import RuntimeRegistry


def _dirs_bytes(tree: Path) -> int:
    """The directories' own ``st_size``, which the walk counts since N31 fix 2.

    Every "the walk governs" expectation below is therefore spelled as
    ``<file bytes> + this``: the push side of the identity is unchanged (it is
    a byte count the mediator reports), and the tests that pin the *push*
    winning keep their plain numbers. Probed with ``os.stat`` rather than
    ``priv_helpers.dir_size`` so the expectation cannot follow the
    implementation; the definition is pinned by ``tests/unit/test_dir_ledger.py``.
    """
    return sum(
        os.stat(dirpath).st_blocks * 512
        for dirpath, _dirs, _files in os.walk(tree)
    )


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


def test_a_round_reports_what_was_appended_on_top_of_its_last_report(tmp_path):
    registry = _registry(tmp_path, "sbx_pushed")
    registry.note_appended("sbx_pushed", 4096)

    # Nothing was reported yet, so the walk (1 kB) is below
    # "previous report (0) + pushed (4 kB)" and the push wins.
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_pushed": 4096}


def test_a_round_does_not_count_a_byte_twice_when_the_walk_catches_up(tmp_path):
    """The production bug, pinned.

    The push reports 4 kB while the file is uncommitted; the next round's walk
    *also* sees those 4 kB (the writeback landed) and the sandbox has pushed
    another 1 kB. A sum would report 1000 + 5000 + 1024; the identity reports
    the last report plus what is new.
    """
    registry = _registry(tmp_path, "sbx_once")
    registry.note_appended("sbx_once", 4096)
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_once": 4096}

    # The commit lands: the tree is now 1000 + 4096, and the push adds 1024.
    (tmp_path / "sbx_once" / "workspace" / "a.bin").write_bytes(b"a" * 5096)
    registry.note_appended("sbx_once", 1024)
    # The identity's two sides, spelled out: the walk (the committed 5096 plus
    # the directories' own blocks, N31 fix 2) against the rebased push
    # (4096 + 1024). They differ by 4 bytes here, which is the point -- the
    # round reports the larger, and neither side counts a byte twice.
    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_once": max(
            5096 + _dirs_bytes(tmp_path / "sbx_once"),
            4096 + 1024,
        )
    }


def test_a_late_sample_of_bytes_the_walk_already_banked_is_not_added_twice(tmp_path):
    """The cluster's 700 -> 777 MiB phantom, pinned.

    A 700 MiB fill was reported as 777 MiB because the mediator's samples of it
    arrived *after* the walk had already banked the same bytes, and the old
    identity added them to the previous report. That 77 MiB of phantom usage
    left a new file 247 MiB of a 324 MiB budget. Both sides are estimates of the
    same quantity, so the combination has to be a max.
    """
    registry = _registry(tmp_path, "sbx_late")
    # The walk banks 4000 bytes with nothing pushed: this is the round that
    # re-bases the push side.
    (tmp_path / "sbx_late" / "workspace" / "a.bin").write_bytes(b"a" * 4000)
    late = 4000 + _dirs_bytes(tmp_path / "sbx_late")
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_late": late}

    # Then the mediator's samples of those same bytes arrive late.
    registry.note_appended("sbx_late", 3000)
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_late": late}


def test_the_walk_governs_once_the_pushes_stop(tmp_path):
    """A sandbox that deletes its files is not held at the high-water mark."""
    registry = _registry(tmp_path, "sbx_delete")
    registry.note_appended("sbx_delete", 10 * 1024 * 1024)
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_delete": 10 * 1024 * 1024}

    (tmp_path / "sbx_delete" / "workspace" / "a.bin").unlink()
    # The file is gone; the directories' own blocks are all that is left.
    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_delete": _dirs_bytes(tmp_path / "sbx_delete")
    }


def test_the_appended_bytes_are_consumed_by_the_round_that_reported_them(tmp_path):
    registry = _registry(tmp_path, "sbx_consumed")
    registry.note_appended("sbx_consumed", 4096)
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_consumed": 4096}

    # Second round: the same bytes must not be added twice (the walk now sees
    # whatever committed, and the accumulator is empty).
    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_consumed": 1000 + _dirs_bytes(tmp_path / "sbx_consumed")
    }


def test_the_ceiling_refresh_peeks_so_the_round_can_still_take_them(tmp_path):
    registry = _registry(tmp_path, "sbx_peek")
    registry.note_appended("sbx_peek", 2048)

    # Peek: the walk is 1 kB, "previous report + pending" is 2 kB, so the
    # pending bytes win -- and they are still pending afterwards.
    assert registry.refresh_disk_usage("sbx_peek") == 2048
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_peek": 2048}


def test_appends_for_an_unknown_sandbox_are_ignored_by_the_round(tmp_path):
    registry = _registry(tmp_path, "sbx_known")
    registry.note_appended("sbx_other", 8192)

    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_known": 1000 + _dirs_bytes(tmp_path / "sbx_known")
    }


def test_a_non_positive_append_is_not_counted(tmp_path):
    registry = _registry(tmp_path, "sbx_zero")
    registry.note_appended("sbx_zero", 0)
    registry.note_appended("sbx_zero", -5)

    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_zero": 1000 + _dirs_bytes(tmp_path / "sbx_zero")
    }


def test_appends_accumulate_until_a_round_takes_them(tmp_path):
    registry = _registry(tmp_path, "sbx_sum")
    registry.note_appended("sbx_sum", 1)
    registry.note_appended("sbx_sum", 2)
    registry.note_appended("sbx_sum", 3)

    # 6 bytes of appends is far below what the walk already sees, and the walk
    # is the floor: the number may never be smaller than it.
    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_sum": 1000 + _dirs_bytes(tmp_path / "sbx_sum")
    }


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
    assert registry.disk_usage_snapshot(dirty=True) == {
        "sbx_gone": 1000 + _dirs_bytes(tmp_path / "sbx_gone")
    }


def test_a_crossing_seen_only_through_pushed_bytes_is_still_a_crossing(tmp_path):
    """The point: the crossing must be reportable *now*.

    The tree on disk is 1 kB and the filesystem says so; the mediator says the
    sandbox has appended 3 MiB. With a 2 MiB budget the sandbox is over, and
    the immediate-report path has to see it even though nothing on disk has
    changed yet.
    """
    registry = _registry(tmp_path, "sbx_crossed", disk_mb=2)
    registry.note_appended("sbx_crossed", 3 * 1024 * 1024)

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_crossed": 3 * 1024 * 1024}
    assert registry.take_budget_crossings() == {"sbx_crossed": 3 * 1024 * 1024}


# ---------------------------------------------------------------------------
# N25: the push also *wakes* the round. Waiting for the cadence is what the
# cluster measured as the remaining overshoot: a writer at ~130 MB/s put
# 124 MiB into the tree in the 927 ms between the round that saw exactly
# 1024 MiB and the next one. So the bytes decide when the round runs.
# ---------------------------------------------------------------------------


def _waking_registry(base: Path, sandbox_id: str) -> tuple[RuntimeRegistry, list[str]]:
    registry = _registry(base, sandbox_id)
    woken: list[str] = []
    registry.set_disk_wakeup(woken.append)
    return registry, woken


def test_appends_below_the_trigger_do_not_wake_the_round(tmp_path):
    registry, woken = _waking_registry(tmp_path, "sbx_quiet")
    registry._append_trigger_bytes = 1024 * 1024

    registry.note_appended("sbx_quiet", 1024)

    assert woken == []


def test_enough_appended_bytes_wake_the_round(tmp_path):
    registry, woken = _waking_registry(tmp_path, "sbx_loud")
    registry._append_trigger_bytes = 1024 * 1024

    registry.note_appended("sbx_loud", 1024 * 1024)

    assert woken == ["sbx_loud"]


def test_the_wakeup_is_rate_limited(tmp_path):
    """A 130 MB/s writer crosses the trigger every few ms.

    Waking on every crossing would run the round continuously; the point is to
    run it *as well as* the cadence, not instead of having a cadence.
    """
    registry, woken = _waking_registry(tmp_path, "sbx_burst")
    registry._append_trigger_bytes = 1024
    registry._append_min_interval_s = 3600.0

    for _ in range(100):
        registry.note_appended("sbx_burst", 1024)

    assert woken == ["sbx_burst"]


def test_a_round_that_catches_up_stops_the_waking(tmp_path):
    registry, woken = _waking_registry(tmp_path, "sbx_catchup")
    registry._append_trigger_bytes = 4096
    registry._append_min_interval_s = 0.0

    registry.note_appended("sbx_catchup", 8192)
    assert woken == ["sbx_catchup"]

    # The round consumes the accumulator, so the next few bytes are below the
    # trigger again -- the waking follows the accounting, not the clock.
    registry.disk_usage_snapshot(dirty=True)
    registry._disk_wakeup_at = 0.0
    registry.note_appended("sbx_catchup", 1024)

    assert woken == ["sbx_catchup"], "nothing new has accumulated"
