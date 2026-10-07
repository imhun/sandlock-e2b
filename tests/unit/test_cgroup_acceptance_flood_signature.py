"""Check 3's verdict must not be satisfiable with the notification cap still on.

``deploy/scripts/acceptance/cgroup_acceptance.py`` check 3 asserts that the N82
probe's ``openclose`` flood is bounded by the sandbox's own ``cpu.max``. Every
other clause it has also holds when the *notification rate cap* is what did the
bounding -- a capped flood is simply slower and books less CPU, so the cgroup
looks innocent either way. That is how the k0s run of 2026-10-07 could report
``9/9`` while having read the limiter's value nowhere at all (``lane.worker_env``
records it, it does not judge it).

So the script reads the probe's own stall counter, and these cases pin the line
it draws against readings the two lanes actually produced: the cap firing about
once a second (N82: **40 stalls in 51 rounds**) versus not firing at all (N82's
uncapped leg at 18149 op/s and N83's local lane at 9794 op/s both read **0**).
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
    """N82's capped lane: one stall per second, for the whole run."""
    assert _flood_is_capped()(stalls=40, rounds=51) is True


def test_the_two_measured_uncapped_readings_are_not() -> None:
    """Both uncapped legs, at the round counts their own rates produce."""
    flood_is_capped = _flood_is_capped()
    assert flood_is_capped(stalls=0, rounds=196) is False  # N83 local, 9794 op/s
    assert flood_is_capped(stalls=0, rounds=40) is False  # N82 online, 18149 op/s


def test_an_unread_flood_is_not_evidence_of_an_uncapped_one() -> None:
    """A probe that printed no DONE line, or too few rounds, proves nothing."""
    flood_is_capped = _flood_is_capped()
    assert flood_is_capped(stalls=None, rounds=51) is True
    assert flood_is_capped(stalls=0, rounds=None) is True
    assert flood_is_capped(stalls=0, rounds=3) is True


def test_the_line_sits_between_the_two_signatures() -> None:
    """One round in four, exactly: the boundary is `stalls > rounds / 4`."""
    flood_is_capped = _flood_is_capped()
    assert flood_is_capped(stalls=13, rounds=20) is True  # N82's getdents leg
    assert flood_is_capped(stalls=10, rounds=40) is False
    assert flood_is_capped(stalls=11, rounds=40) is True
