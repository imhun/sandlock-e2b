"""The fleet's record surfaces, named once (C3 Task 6).

The worker's own reconcile compared two *surfaces* before it deleted anything
(Task 4's reviews pinned the discipline): the fleet-scope id enumeration
(``GET /internal/fleet/sandboxes``) and the fleet-wide record count
(``GET /internal/fleet/metrics`` → ``activeSandboxes``). The second read is what
catches an enumeration that came back short -- the whole reason the shape is
two reads rather than one.

The self-heal sweep (``control_plane/self_heal.py``) keeps that discipline,
now on the side that holds the authority, so the number it compares against has
one definition and one owner:

* the **enumeration** is :meth:`SandboxRegistry.fleet_id_snapshot` -- the ids
  plus "what the store could not answer" (``unreadable``), because a short
  answer has to be distinguishable from a complete one;
* the **count** is :func:`active_sandbox_count` here, the same expression
  ``/internal/fleet/metrics`` reports as ``activeSandboxes``.

These are two reads of the same store, and that is the point: a record created
or removed between them is a race, and a race is a reason to defer rather than
to delete the tree of a sandbox that was just registered.
"""

from __future__ import annotations

from typing import Any


def active_sandbox_count(state: Any) -> int:
    """The count ``GET /internal/fleet/metrics`` reports as ``activeSandboxes``.

    Kept identical to that handler's expression ``len(registry.list())`` on
    purpose (``tests/unit/test_c3_self_heal_sweep.py`` pins the agreement): the
    self-heal sweep has to compare against *the* fleet-wide record count, not
    against a second definition of it that could drift.
    """
    return len(state.registry.list())
