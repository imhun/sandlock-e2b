"""The boxed-quota contracts must describe whatever ceiling the lane runs.

Both contract files used to hardcode the 1 GiB code default, so a lane
reproducing the deployed 512MB shape (docs/production-deployment-requirements.md
2.4.8) still asserted 800/450 MiB allocations that do not exist there. They now
derive their sizes from ``E2B_DEFAULT_MEMORY_MB`` via ``tests/_memory_budget.py``;
this test pins the properties those sizes have to keep at any ceiling, so an
edit to the fractions cannot quietly make an assertion vacuous:

* boxed sibling contract -- the holder fits, holder + over-budget sibling does
  not, holder + control does;
* gateway contract -- the gateway reserve plus its stdio server fits, plus the
  denied sibling does not, plus the control command does.
"""

from __future__ import annotations

import pytest

from tests._memory_budget import (
    MEASURED_512_HEADROOM_MB,
    boxed_sizes,
    gateway_sizes,
    per_sandbox_memory_mb,
)

CEILINGS = (512, 1024)


def test_ceiling_defaults_to_the_code_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("E2B_DEFAULT_MEMORY_MB", raising=False)
    assert per_sandbox_memory_mb() == 1024


@pytest.mark.parametrize("ceiling", CEILINGS)
def test_ceiling_comes_from_the_environment(
    ceiling: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("E2B_DEFAULT_MEMORY_MB", str(ceiling))
    assert per_sandbox_memory_mb() == ceiling


@pytest.mark.parametrize("ceiling", CEILINGS)
def test_boxed_sizes_keep_all_three_properties(ceiling: int) -> None:
    holder, sibling, control = boxed_sizes(ceiling)
    assert holder < ceiling, "the holder must fit on its own"
    assert holder + sibling > ceiling, "the denied sibling must overcommit"
    assert holder + control <= ceiling, "the control command must fit"


@pytest.mark.parametrize("ceiling", CEILINGS)
def test_gateway_sizes_keep_all_three_properties(ceiling: int) -> None:
    server, denied, control = gateway_sizes(ceiling)
    # The denied sibling asks for the whole box, so it overcommits whatever the
    # gateway itself accounts for; the server and the control command together
    # have to stay inside the headroom the box is known to have, which scales
    # with the ceiling (the gateway's own footprint does not).
    headroom = MEASURED_512_HEADROOM_MB * ceiling // 512
    assert server + denied > ceiling, "the denied sibling must overcommit"
    assert server + control <= headroom, "server + control must fit"


def test_sizes_are_the_documented_numbers() -> None:
    """The exact sizes the two contracts allocate at each ceiling."""
    assert boxed_sizes(512) == (358, 204, 25)
    assert boxed_sizes(1024) == (716, 409, 51)
    assert gateway_sizes(512) == (64, 512, 25)
    assert gateway_sizes(1024) == (128, 1024, 51)
