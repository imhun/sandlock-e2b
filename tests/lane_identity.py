"""The lane's stand-in for the node agent: write the slot child's identity.

C3 grants an own-identity slot its identity by having the **agent** write the
child's ``uid_map``/``gid_map``. An in-process lane has no agent, but it runs as
root -- the same capability the agent's face A holds -- so it can perform the
identical write on the child the pool forked. That is what makes own identity
exercisable off-cluster at all, and it is why the same helper serves the
security lane (``tests/security/conftest.py``), the executor contract
(``tests/contract/test_own_identity_executor.py``), the non-root lane and the
multi-node harness (``tests/conftest.py``).

Without it, a worker whose ``E2B_IDENTITY_GRANT`` is ``agent-grant`` (the only
shape left since N52) refuses the create by name -- "own identity was requested
but … needs the control-plane reporter" -- and, for a chroot sandbox, has no
downgrade tier: the in-process mediator is refused by the fork instead of
writing files as the worker (T5, measured 2026-10-11 as the bulk of the
in-process lane's red).
"""

from __future__ import annotations

import os
from pathlib import Path


def lane_identity_reporter(uid_start: int, uid_size: int):
    """``(sandbox_id, pid) -> {"ok": True}``, or ``None`` off a root lane.

    A non-root lane answers ``None``, and own identity then declines with its
    named reason -- exactly as a worker whose agent is missing would (the
    callers that cannot run without a slot turn that into a skip rather than a
    false pass).

    The mapping is a *range* (``uid_start .. uid_start+uid_size-1``) because the
    reporter is handed ``(sandbox_id, pid)`` and not the uid the pool picked;
    every uid the pool can hand out is mapped, and the sandbox's host identity
    is the same number the pool reserved.
    """
    if os.geteuid() != 0:
        return None

    def report(sandbox_id: str, pid: int) -> dict:
        mapping = f"{uid_start} {uid_start} {uid_size}\n"
        Path(f"/proc/{pid}/uid_map").write_text(mapping, encoding="ascii")
        try:
            Path(f"/proc/{pid}/setgroups").write_text("deny\n", encoding="ascii")
        except OSError:
            # Already denied (the child or an earlier grant did it): the map
            # below is what matters.
            pass
        Path(f"/proc/{pid}/gid_map").write_text(mapping, encoding="ascii")
        return {"ok": True}

    return report


def with_lane_identity_reporter(settings):
    """``settings`` with the lane's stand-in agent attached when it needs one.

    One place for the "a root, in-process worker with a per-sandbox uid pool
    cannot report a slot child's identity by itself" wall (N52): since
    ``agent-grant`` is the only grant shape left, such a worker refuses a
    mediated (chroot) create by name -- the in-process mediator would run as the
    worker's euid while the sandbox's host uid is the pooled one (SL-1/T5, and
    there is no downgrade tier any more).  Harnesses that hand-build their own
    ``EnvdSettings`` (``tests/conftest.py``'s ``make_apps``, the pause/resume and
    lifecycle contracts) call this so the file they build is exercisable in the
    lane at all.

    The range is the settings' *own* pool, so every uid it can lease is mapped.
    Off a root lane the helper answers ``None`` and own identity declines with
    its named reason -- exactly as a worker whose agent is missing would, which
    is what the callers that cannot run without a slot turn into a skip rather
    than a false pass.
    """
    if getattr(settings, "identity_reporter", None) is not None:
        return settings
    if not getattr(settings, "per_sandbox_uid", False):
        return settings
    settings.identity_reporter = lane_identity_reporter(
        int(getattr(settings, "uid_pool_start", 10000)),
        int(getattr(settings, "uid_pool_size", 1000)),
    )
    return settings
