"""Blind spot 2 of E9.1's idle detection: a sandbox that only burns CPU.

Idle is decided from activity the **platform** can see: requests that cross
envd's auth layer (worker side) and lifecycle calls (control plane). Three
shapes have none of that (`docs/resource-contention.md` §6), and this module
closes the cheapest one: a sandbox running a long CPU-bound task, with no
requests and no egress, looks exactly like an empty one -- so eviction pauses it
and releases its reservation while it is still working.

Two decisions worth stating, because both have an obvious wrong answer:

* **The per-sandbox host uid is the key** (E3.2). Under own identity the slot *is* the
  sandbox's process tree and runs as that pooled uid, so one pass over ``/proc``
  that sums every process's ``utime+stime`` by *owner uid* yields the sandbox's
  CPU without walking a process tree per sandbox. A sandbox with no pooled uid
  (a non-root worker, the shared-uid shape) is deliberately **not** sampled: its
  CPU cannot be told apart from the worker's own, and a guess there would make
  the signal a lie that the eviction then acts on.
* **The threshold is a percentage of one core**, averaged over the sampling
  window (``E2B_CPU_ACTIVITY_PERCENT``, default 5). Treating "there was a delta"
  as activity would make every sandbox un-evictable -- a process that wakes once
  a minute has a delta -- which is the one failure mode this must not have.

The report rides the **existing** channel: a sandbox above the threshold is
marked active, so it travels the same ``sandboxActivity`` heartbeat and the same
``apply_activity_report`` merge a request would. No new wire field, and no
second threshold on the control plane to keep in step.
"""

from __future__ import annotations

import os
from pathlib import Path

#: `_SC_CLK_TCK`: the unit `/proc/<pid>/stat`'s utime/stime are counted in.
TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")


def parse_stat_cpu(stat: str) -> int:
    """``utime + stime`` (in clock ticks) out of one ``/proc/<pid>/stat`` line.

    The process name sits in parentheses and may contain spaces or parentheses
    of its own, so the fields are read from the **last** ``)`` -- the same rule
    ``/proc`` readers everywhere have to follow.
    """
    fields = stat[stat.rindex(")") + 2 :].split()
    return int(fields[11]) + int(fields[12])


def sample_cpu_ticks(
    proc_root: Path | str = "/proc", *, owner_uid=None
) -> dict[int, int]:
    """CPU ticks per owning uid, over every process in ``proc_root``.

    One pass, no per-sandbox tree walk: the *owner uid* of each process is what
    separates sandboxes under E3.2, and ``/proc/<pid>``'s own owner is that uid
    (the sandbox's slot and everything it spawns). ``owner_uid`` is injectable so
    the tests can label fake processes (`os.stat` is the real one).

    A process that disappears mid-walk is skipped, not an error: this runs on a
    timer, and a churn of short-lived processes is normal.
    """
    if owner_uid is None:
        def owner_uid(path: Path) -> int:  # noqa: E306 - tiny injectable default
            return os.stat(path).st_uid

    root = Path(proc_root)
    ticks: dict[int, int] = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        return ticks
    for entry in entries:
        if not entry.name.isdigit():
            continue
        path = Path(entry.path)
        try:
            uid = owner_uid(path)
            stat = (path / "stat").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        try:
            spent = parse_stat_cpu(stat)
        except (ValueError, IndexError):
            continue
        ticks[uid] = ticks.get(uid, 0) + spent
    return ticks


class CpuActivityTracker:
    """Two samples in, "which sandboxes were actually working" out.

    Percentages are *of one core*, averaged over the wall-clock window between
    the two samples: a sandbox that used 0.25 s of CPU over a 5 s window is at
    5 %. The first call has nothing to compare against and reports zeros -- a
    worker that just started must not claim every sandbox is busy.
    """

    def __init__(
        self,
        *,
        percent_threshold: float = 5.0,
        ticks_per_second: int = TICKS_PER_SECOND,
    ) -> None:
        self.percent_threshold = float(percent_threshold)
        self._ticks_per_second = int(ticks_per_second)
        self._last: dict[int, int] | None = None
        self._last_at: float | None = None

    def observe(
        self, ticks_by_uid: dict[int, int], *, now: float
    ) -> dict[int, float]:
        """Percent-of-one-core per uid since the previous call."""
        previous, previous_at = self._last, self._last_at
        self._last, self._last_at = dict(ticks_by_uid), now
        if previous is None or previous_at is None:
            return {}
        elapsed = now - previous_at
        if elapsed <= 0:
            return {}
        percents: dict[int, float] = {}
        for uid, ticks in ticks_by_uid.items():
            delta = ticks - previous.get(uid, ticks)
            if delta <= 0:
                continue
            cpu_seconds = delta / self._ticks_per_second
            percents[uid] = 100.0 * cpu_seconds / elapsed
        return percents

    def busy(self, percents: dict[int, float]) -> set[int]:
        """The uids the tracker would call "working" (>= the threshold)."""
        return {
            uid for uid, percent in percents.items()
            if percent >= self.percent_threshold
        }
