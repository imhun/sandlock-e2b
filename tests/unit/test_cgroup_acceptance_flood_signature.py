"""Check 3's verdict must not be satisfiable with the notification cap still on.

``deploy/scripts/acceptance/cgroup_acceptance.py`` check 3 asserts that the N82
probe's ``openclose`` flood is bounded by the sandbox's own ``cpu.max``. Every
other clause it has also holds when the *notification rate cap* is what did the
bounding -- a capped flood is simply slower and books less CPU, so the cgroup
looks innocent either way. That is how the k0s run of 2026-10-07 could report
``9/9`` while having read the limiter's value nowhere at all (``lane.worker_env``
records it, it does not judge it).

So the script reads the probe's own stall counter, and these cases pin the line
it draws against readings the lanes actually produced: the cap firing about
once a second (N82: **40 stalls in 51 rounds**) versus not firing at all (N82's
uncapped leg at 18149 op/s and N83's local lane at 9794 op/s both read **0**).

The counter alone was not enough (2026-10-07): a *CPU-bound* op stalls on nearly
every round while its worst single op stays around 25 ms (`clone`: 16/16 rounds,
0.99 ms mean ops), which the one-dimensional line called "capped". The line now
needs **both** dimensions -- the counter above a quarter of the rounds *and* a
worst op in the hundreds of milliseconds -- and an unread amplitude counts as
"capped" (fail closed), exactly like an unread counter.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "acceptance" / "cgroup_acceptance.py"


def _flood_is_capped():
    """Load the acceptance script by path; it has no cluster-side import deps."""
    spec = importlib.util.spec_from_file_location("cgroup_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.flood_is_capped


def test_the_capped_reading_is_recognised() -> None:
    """N82's capped lane: one stall per second, worst op 772-784 ms."""
    assert _flood_is_capped()(stalls=40, rounds=51, max_stall_us=784_000) is True


def test_the_two_measured_uncapped_readings_are_not() -> None:
    """Both uncapped legs, at the round counts their own rates produce."""
    flood_is_capped = _flood_is_capped()
    # N83 local, 9794 op/s
    assert flood_is_capped(stalls=0, rounds=196, max_stall_us=0) is False
    # N82 online, 18149 op/s
    assert flood_is_capped(stalls=0, rounds=40, max_stall_us=0) is False
    # The shipped `required` lane today: 1 stall in 342 rounds, ~24 ms worst.
    assert flood_is_capped(stalls=1, rounds=342, max_stall_us=24_000) is False


def test_a_cpu_bound_op_is_not_a_capped_one() -> None:
    """The false positive the second dimension exists for.

    `clone` (fork + wait) saturates the sandbox's own core, so it stalls on
    every round -- but its worst single op is ~25 ms, an order of magnitude away
    from the cap's "sleep out the window": that is a busy box, not a throttled
    notification budget.
    """
    flood_is_capped = _flood_is_capped()
    assert flood_is_capped(stalls=16, rounds=16, max_stall_us=26_682) is False
    # ...while the same counter with the cap's magnitude is the cap.
    assert flood_is_capped(stalls=16, rounds=16, max_stall_us=860_000) is True


def test_an_unread_flood_is_not_evidence_of_an_uncapped_one() -> None:
    """A probe that printed no DONE line, or too few rounds, proves nothing."""
    flood_is_capped = _flood_is_capped()
    assert flood_is_capped(stalls=None, rounds=51, max_stall_us=800_000) is True
    assert flood_is_capped(stalls=0, rounds=None, max_stall_us=800_000) is True
    assert flood_is_capped(stalls=0, rounds=3, max_stall_us=800_000) is True
    # An unread *amplitude* is the same kind of not-evidence.
    assert flood_is_capped(stalls=40, rounds=51, max_stall_us=None) is True


def test_the_line_sits_between_the_two_signatures() -> None:
    """One round in four and 200 ms, exactly: both sides are strict."""
    flood_is_capped = _flood_is_capped()
    # N82's getdents leg: 13/20 rounds, hundreds of ms.
    assert flood_is_capped(stalls=13, rounds=20, max_stall_us=883_000) is True
    # Exactly the share, exactly the magnitude: neither is "capped".
    assert flood_is_capped(stalls=10, rounds=40, max_stall_us=800_000) is False
    assert flood_is_capped(stalls=11, rounds=40, max_stall_us=200_000) is False
    # ...and one microsecond past it on both sides is.
    assert flood_is_capped(stalls=11, rounds=40, max_stall_us=200_001) is True
