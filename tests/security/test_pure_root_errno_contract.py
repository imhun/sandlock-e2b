"""The errno contract for a path outside every grant, in the pure shape.

N15 fixed "authorised-outside answers EACCES with one diagnostic line"
(``docs/pure-shape-decision.md`` §6). The synthesized root (N16,
``E2B_PURE_ROOTFS=synth``) splits that sentence in two, and the split is the
point of the route: the answer now depends on whether the *path* exists in the
tree the shape gives the sandbox.

Measured, byte-exactly, over both legal pure shapes (2026-09-26):

* **The open path is decided by the grant, not by the tree.** ``cat`` of a path
  outside the allow-list answers ``EACCES`` in *both* shapes, whether or not the
  path exists: the mediator refuses the path string before the kernel looks it
  up. ``/src`` does not exist in either tree and still answers "Permission
  denied".
* **The stat path is decided by the tree.** A path whose parent chain resolves
  in the shape's tree answers ``EACCES`` (exists-but-not-granted); a path whose
  parent chain is missing answers ``ENOENT``. The identity shape's tree is the
  host root, the synthesized one's is the skeleton, and the skeleton's ``/var``
  (like every non-bound system directory) is an *empty* directory -- so a host
  path under the workspace base is "exists, not granted" in one shape and "in no
  tree at all" in the other. That difference is exactly the case Task 10's
  ``test_distinct_host_uids_isolate_same_path_files`` red was about, and both
  spellings are pinned here.

Run through ``route_b_sandbox(None, None)`` so the shape comes from the same
entry point every other security case uses (``tests/security/conftest.py``
mirrors ``E2B_REAL_ROOT`` and ``E2B_PURE_ROOTFS``).

Deviation from the task brief, recorded because it changes what is pinned: the
brief expected ``cat /src/host-only/SECRET`` and ``stat /src`` to answer
``ENOENT`` under the synthesized root. They do not -- both answer ``EACCES`` in
both shapes (``tmp/k0s/task11/probe-boundary.log``), for the reason above: the
grant check answers before the lookup for the open path, and ``/`` resolves for
the stat path. The ENOENT half of the contract lives on *paths under a missing
parent*, which is what the last assertion measures.
"""
from __future__ import annotations

import os

import pytest

from tests.security.conftest import require_mediation_capable, route_b_sandbox, run_sh


@pytest.mark.usefixtures("require_sandlock")
async def test_the_errno_for_a_path_outside_every_grant_is_pinned(workspace):
    # The tree this case needs is the suite's own ``workspace`` (under
    # ``tests/conftest.TMP_ROOT``), not the helper's default ``/tmp`` scratch
    # directory: the synthesized skeleton *has* an (empty) ``/tmp``, so a
    # ``/tmp``-based path resolves its parents in both shapes and answers EACCES
    # twice -- measured, and exactly the trap this assertion exists to avoid.
    # Under TMP_ROOT the parents are in the identity tree and nowhere in the
    # skeleton, which is what splits the answer.
    executor, workspace = route_b_sandbox(None, None, workspace=workspace)
    # Prove the shape before asserting on it: `route_b_sandbox` is the only
    # place the switch is mirrored, and a lane that forgot it would silently
    # measure the identity shape and make the split below vacuous.
    synth = os.environ.get("E2B_PURE_ROOTFS", "off").strip().lower() == "synth"
    assert executor._has_sandbox_root is synth, (
        f"shape mismatch: E2B_PURE_ROOTFS={os.environ.get('E2B_PURE_ROOTFS')!r} "
        f"but has_sandbox_root={executor._has_sandbox_root}"
    )
    # A host path that exists (identity shape: not granted; synthesized shape:
    # under the empty skeleton `/var`, so not in the tree at all).
    host_only = workspace.parent / "task11-host-only"
    host_only.mkdir(exist_ok=True)
    try:
        require_mediation_capable(executor)

        # The open path: the grant check answers before any lookup, so the
        # answer is the same in both shapes -- including for a path that exists
        # in neither tree.
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

        # The stat path: `/` resolves in both trees, so both shapes stop at the
        # grant; a parent below it that is missing from either tree answers
        # ENOENT instead.
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

        # The split itself: one command, one path, two shapes. The identity
        # tree resolves the path's parents and refuses it as ungranted; the
        # synthesized tree has no such parents and never gets that far.
        expected = (
            b"stat: cannot statx '%s': No such file or directory\n"
            % str(host_only).encode()
            if synth
            else b"stat: cannot statx '%s': Permission denied\n"
            % str(host_only).encode()
        )
        assert await run_sh(executor, workspace, f"stat {host_only}") == (
            1,
            b"",
            expected,
        )
    finally:
        executor.close()
