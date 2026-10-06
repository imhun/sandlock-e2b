"""The per-sandbox *policy* ceiling (N83 phase 2, D5): one rule, two readers.

What a **single** sandbox may be configured to is a deployment decision, and
the plan's ruling (P3, corrected 2026-10-06) is that it comes from an explicit
env -- ``E2B_MAX_SANDBOX_CPU_PERCENT`` / ``E2B_MAX_SANDBOX_MEMORY_MB`` /
``E2B_MAX_SANDBOX_PROCESSES`` -- *not* from reading the kernel. Two reasons,
both measured:

* the compose lanes set no ``cpus``/``mem_limit`` at all, so their kernel layer
  reads ``max``; any "derive the ceiling from the kernel" rule would silently
  degrade to "no ceiling" exactly where a ceiling is the only thing there;
* a sandbox cannot be bigger than the node that hosts it, so the node's own
  total is the safe default -- but the deployment must be able to lower it
  (four cores for the node does not mean four cores for one sandbox).

Both sides of the wire read this one function, because the control plane and
the worker have to agree about the *names* and about the "unset or ``<=0``
follows the node total" rule: the worker reports what it resolved in its
heartbeat and the control plane stores it, so a rule that drifted would show
up as two different ceilings for the same node.

``<= 0`` never means "unlimited" here. ``0`` is this repo's convention for a
*node* budget whose dimension is switched off; on a per-sandbox ceiling it
would read downstream as "one sandbox may take everything", which is the
fail-open the plan's Review Focus §1 names. The rule is therefore strictly
positive: the configured value, else the node total, else the per-sandbox
create default (the only positive signal left on a node that declared no
total at all).
"""

from __future__ import annotations

from dataclasses import dataclass

#: The three env names, spelled once. The manifests, the control plane's
#: ``Settings`` and the worker's ``Settings`` all read these.
MAX_SANDBOX_CPU_PERCENT_ENV = "E2B_MAX_SANDBOX_CPU_PERCENT"
MAX_SANDBOX_MEMORY_MB_ENV = "E2B_MAX_SANDBOX_MEMORY_MB"
MAX_SANDBOX_PROCESSES_ENV = "E2B_MAX_SANDBOX_PROCESSES"


@dataclass(frozen=True)
class SandboxCeiling:
    """A ceiling for one sandbox, in the plan's three dimensions.

    ``None`` means "no limit is set here": for a *kernel* read that is the
    ``max`` the kernel reports (the physical layer caps nothing, see
    ``deploy/compose``), and for a *policy* ceiling it is a dimension the
    caller does not police. A policy ceiling resolved through
    :func:`resolve_sandbox_ceiling` is always a positive integer.
    """

    cpu_percent: int | None = None
    memory_mb: int | None = None
    processes: int | None = None


def resolve_sandbox_ceiling(
    *, configured: int, node_total: int, create_default: int
) -> int:
    """One dimension of the per-sandbox policy ceiling, never ``<= 0``.

    ``configured`` is the explicit ``E2B_MAX_SANDBOX_*`` value (``0``/negative
    = "not set"); ``node_total`` is the node's own total for the same dimension
    (``0`` = the node declared none); ``create_default`` is the per-sandbox
    create default (``E2B_DEFAULT_*``), the last positive signal.
    """
    if configured > 0:
        return configured
    if node_total > 0:
        return node_total
    return create_default
