"""Task 1 of the route B -> own_identity rename: the new names, and the freeze.

Two things are asserted here, and both are the point of the rename rather than
decorations on it:

* the **frozen** return value of the instance-name rule. The function is
  renamed, but its output is a disk contract the control plane and the worker
  both derive the slot documents' path from (ruling D20) -- so the bytes that
  come out of it may not move with the identifier. The three literals are
  lifted byte-for-byte from the pre-rename implementation (short id passes
  through, an 84-byte id is replaced by ``sbx_<sha256[:16]>``, an id exactly at
  the 64-byte ceiling passes through).
* the backend types are reachable under their new names, so the module move is
  not just a file rename that leaves the symbols behind.
"""

from __future__ import annotations


def test_the_own_identity_instance_name_is_frozen() -> None:
    from gateway_common.paths import (
        OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES,
        own_identity_instance_name,
    )

    assert OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES == 64
    assert own_identity_instance_name("sbx_0123456789abcdef") == "sbx_0123456789abcdef"
    # Over-long id -> hash: this branch is part of the rule, not an
    # implementation detail of the worker. A control plane that did not know it
    # would derive the wrong directory for exactly those sandboxes (D20).
    assert own_identity_instance_name("sbx_" + "a" * 80) == "sbx_b926db5e21e80dd2"
    # Exactly at the ceiling passes through unchanged.
    at_limit = "sbx_" + "a" * 60
    assert own_identity_instance_name(at_limit) == at_limit


def test_the_backend_types_are_reachable_under_their_new_names() -> None:
    from envd_service.own_identity import (
        OwnIdentityConfig,
        OwnIdentityExecProcess,
        OwnIdentityInstance,
    )

    assert OwnIdentityConfig(mode="off").mode == "off"
    # The two client classes exist under the new names (their behaviour is the
    # rest of the suite's business, not this guard's).
    assert OwnIdentityExecProcess.__name__ == "OwnIdentityExecProcess"
    assert OwnIdentityInstance.__name__ == "OwnIdentityInstance"
