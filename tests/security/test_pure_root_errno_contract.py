"""The errno contract for a path outside every grant, in the pure shape.

N15 fixed "authorised-outside answers EACCES with one diagnostic line"
(``docs/pure-shape-decision.md`` §6). The synthesized root (N16, the only pure
root since N14 S5 retired the identity one) refines that sentence, and the
refinement is the point of the route: the answer depends on whether the *path*
resolves in the tree the shape gives the sandbox.

* **The open path is decided by the grant, not by the tree.** ``cat`` of a path
  outside the allow-list answers ``EACCES`` whether or not the path exists: the
  mediator refuses the path string before the kernel looks it up. ``/src`` does
  not exist in the skeleton and still answers "Permission denied".
* **The stat path is decided by the tree.** A path whose parent chain resolves
  in the skeleton answers ``EACCES`` (exists-but-not-granted); a path whose
  parent chain is missing answers ``ENOENT``. The skeleton's ``/var`` (like
  every non-bound system directory) is an *empty* directory, so a host path
  under the workspace base is "in no tree at all" and answers ENOENT -- exactly
  the case Task 10's ``test_distinct_host_uids_isolate_same_path_files`` red was
  about. (Both spellings were measured 2026-09-26; the identity half is gone
  with the identity root, and what is left is what is pinned here.)

Run through ``route_b_sandbox(None, None)`` so the shape comes from the same
entry point every other security case uses (``tests/security/conftest.py``,
which synthesizes the one pure root unconditionally since N14 S5).

Deviation from the task brief, recorded because it changes what is pinned: the
brief expected ``cat /src/host-only/SECRET`` and ``stat /src`` to answer
``ENOENT`` under the synthesized root. They do not -- both answer ``EACCES`` in
both shapes (``tmp/k0s/task11/probe-boundary.log``), for the reason above: the
grant check answers before the lookup for the open path, and ``/`` resolves for
the stat path. The ENOENT half of the contract lives on *paths under a missing
parent*, which is what the last assertion measures.
"""
from __future__ import annotations

import pytest

from tests.security.conftest import require_mediation_capable, route_b_sandbox, run_sh


@pytest.mark.usefixtures("require_sandlock")
async def test_the_errno_for_a_path_outside_every_grant_is_pinned(workspace):
    # The tree this case needs is the suite's own ``workspace`` (under
    # ``tests/conftest.TMP_ROOT``), not the helper's default ``/tmp`` scratch
    # directory: the synthesized skeleton *has* an (empty) ``/tmp``, so a
    # ``/tmp``-based path resolves its parents and answers EACCES -- measured,
    # and exactly the trap this assertion exists to avoid. Under TMP_ROOT the
    # parents are in no tree the skeleton has, which is what produces the
    # ENOENT half of the contract.
    executor, workspace = route_b_sandbox(None, None, workspace=workspace)
    # Prove the shape before asserting on it: the helper is the only place a
    # security case gets a shape, and a rootless one (the retired identity
    # shape) would make the ENOENT assertion below vacuous.
    assert executor._has_sandbox_root is True
    # A host path that exists -- but sits under the empty skeleton `/var`, so it
    # is in no tree the sandbox has.
    host_only = workspace.parent / "task11-host-only"
    host_only.mkdir(exist_ok=True)
    try:
        require_mediation_capable(executor)

        # The open path: the grant check answers before any lookup, so a path
        # that exists in no tree answers the same as one that does.
        assert await run_sh(executor, workspace, "cat /etc/passwd") == (
            1,
            b"",
            b"cat: /etc/passwd: Permission denied\n",
        )
        assert await run_sh(executor, workspace, "cat /src/host-only/SECRET") == (
            1,
            b"",
            b"cat: /src/host-only/SECRET: Permission denied\n",
        )

        # The stat path: `/` resolves in the skeleton, so the answer stops at
        # the grant; a parent below it that is missing answers ENOENT instead.
        assert await run_sh(executor, workspace, "stat /src") == (
            1,
            b"",
            b"stat: cannot statx '/src': Permission denied\n",
        )
        assert await run_sh(executor, workspace, "stat /src/host-only/SECRET") == (
            1,
            b"",
            b"stat: cannot statx '/src/host-only/SECRET': No such file or directory\n",
        )

        # The tree decides it: the skeleton has no such parents, so the lookup
        # never reaches the grant check.
        assert await run_sh(executor, workspace, f"stat {host_only}") == (
            1,
            b"",
            b"stat: cannot statx '%s': No such file or directory\n"
            % str(host_only).encode(),
        )
    finally:
        executor.close()
