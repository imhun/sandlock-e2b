"""Per-sandbox memory ceiling and the sizes the boxed-quota contracts use.

The ceiling is a *deployment* value, not a constant: ``E2B_DEFAULT_MEMORY_MB``
is read by both the control plane and the workers, the shipped stack runs
512MB (``deploy/stack/.env``, docs/production-deployment-requirements.md
2.4.8) while the code default is 1024MB, and
``deploy/scripts/test-prod-shaped.sh`` forwards the host's value into the
lane. The two boxed-quota contracts therefore derive their allocation sizes
from it instead of hardcoding the 1 GiB shape they were written against --
otherwise a lane that says "reproduce the deployed shape" still asserts
800/450 MiB allocations that do not exist in it.

Kept import-light (no SDK, no app imports) so the arithmetic can be unit
tested on a host without the E2B client installed.
"""

from __future__ import annotations

import os

#: What a 512MB box can still hand to an MCP stdio server while its gateway
#: runs. Measured on the target 2026-09-16: a server holding 110 MiB
#: initializes and serves ``tools/list``, one holding 120 MiB is killed before
#: it can (``MCP gateway exited ... exit_code=1``, ``MCPError: Connection
#: closed``). That headroom -- not the raw ceiling -- is what bounds
#: ``gateway_sizes`` below; the gateway eats the rest of the box.
MEASURED_512_HEADROOM_MB = 110


def per_sandbox_memory_mb() -> int:
    """The ceiling this run is configured for (code default 1024MB)."""
    raw = os.environ.get("E2B_DEFAULT_MEMORY_MB")
    return int(raw) if raw else 1024


def boxed_sizes(ceiling_mb: int) -> tuple[int, int, int]:
    """``(holder, over_budget_sibling, control)`` for the gateway-free contract.

    Fractions of the ceiling, so the three properties hold at any ceiling:
    the holder fits, holder + over-budget sibling does not, holder + control
    does. 512MB -> 358/204/25; 1024MB -> 716/409/51.
    """
    return (
        ceiling_mb * 7 // 10,
        ceiling_mb * 4 // 10,
        ceiling_mb // 20,
    )


def gateway_sizes(ceiling_mb: int) -> tuple[int, int, int]:
    """``(server_hold, denied, control)`` for the MCP-gateway contract.

    Neither ``denied`` nor ``control`` is derived from the gateway's footprint,
    because that footprint is not the same everywhere it runs (measured ~400
    MiB of a 512MB box on the target, well under 344 MiB in the in-lane
    harness). What holds in both is the *box*:

    * ``denied`` asks for the whole ceiling on its own. Whatever the gateway
      already accounts for, the sum exceeds the ceiling, so the sibling is
      killed -- no matter how large or small the gateway's own footprint is.
    * ``server_hold`` and ``control`` together stay inside the smallest
      headroom the box has been measured to have
      (:data:`MEASURED_512_HEADROOM_MB`), so both fit while proving the
      memory they hold is charged against the same box.

    512MB -> 64/512/25; 1024MB -> 128/1024/51.
    """
    return (ceiling_mb // 8, ceiling_mb, ceiling_mb // 20)
