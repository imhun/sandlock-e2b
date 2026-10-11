"""The errno contract for a path outside every grant, in the pure shape.

N15 fixed "authorised-outside answers EACCES with one diagnostic line"
(``docs/pure-shape-decision.md`` §6). The synthesized root (N16, the only pure
root since N14 S5 retired the identity one) refines that sentence, and the
refinement is the point of the route: **how much of the path has to resolve
before the grant check is consulted differs between the two syscalls, and the
answers differ with it.** Each arm below is therefore a measurement, not a rule
read off one case.

* **The tree decides what exists.** The skeleton carries the system
  *directories*, empty, so ``stat /etc`` answers 0 while ``stat /etc/passwd``,
  ``stat /src``, ``stat /src/host-only/SECRET`` and any host path under the
  workspace base answer ``ENOENT`` -- the name is simply not in the tree. (The
  last of those is exactly the case Task 10's
  ``test_distinct_host_uids_isolate_same_path_files`` red was about.)
* **A directory the skeleton carries is statable but not openable**:
  ``ls /etc`` is ``EACCES`` while ``stat /etc`` is 0. The mediator's grant
  covers the workspace and its aliases, so *reading* a system directory is
  refused even though the name resolves. ``ls /`` is the boundary case the
  other way: the sandbox's own root is listable, which is how the shape stays
  usable at all.
* **The open path asks the grant as soon as the *parent* resolves.** ``cat
  /etc/passwd`` answers ``EACCES`` even though nothing named ``passwd`` exists
  in the skeleton's ``/etc`` -- the mediator refuses the path string before the
  kernel looks the last component up. A path whose parent does not resolve never
  reaches that check: ``cat /src/host-only/SECRET`` is ``ENOENT``.
* Therefore the two halves are falsifiable against each other: change the
  skeleton's directory set and the ``stat`` arm moves; change the grant order
  and the ``cat`` arm moves.

Run through ``own_identity_sandbox(None, None)`` so the shape comes from the same
entry point every other security case uses (``tests/security/conftest.py``,
which synthesizes the one pure root unconditionally since N14 S5).

What the rule replaced, recorded because the assertion carried it for a while:
the task brief expected ``cat /src/host-only/SECRET`` and ``stat /src`` to answer
``ENOENT``, ``tmp/k0s/task11/probe-boundary.log`` then measured ``EACCES`` for
both under the *emulated* root (there the mediator answered from the grant check
alone, with no tree to look in), and the synthesized root answers ``ENOENT``
again -- for ``/src`` because nothing there resolves, for ``/etc/passwd``
because what resolves is the skeleton's *empty* ``/etc``. Same three letters,
two different reasons, which is what the arms below separate.
"""
from __future__ import annotations

import pytest

from tests.security.conftest import require_mediation_capable, own_identity_sandbox, run_sh


@pytest.mark.usefixtures("require_sandlock")
async def test_the_errno_for_a_path_outside_every_grant_is_pinned(workspace):
    # The tree this case needs is the suite's own ``workspace`` (under
    # ``tests/conftest.TMP_ROOT``), not the helper's default ``/tmp`` scratch
    # directory: the synthesized skeleton *has* an (empty) ``/tmp``, so a
    # ``/tmp``-based path resolves its parents and answers EACCES -- measured,
    # and exactly the trap this assertion exists to avoid. Under TMP_ROOT the
    # parents are in no tree the skeleton has, which is what produces the
    # ENOENT half of the contract.
    executor, workspace = own_identity_sandbox(None, None, workspace=workspace)
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

        # An open whose *parent* resolves: the lookup is done, so the grant
        # check is what refuses it -- the N15 sentence, still true for `/etc`,
        # and answerable without the last component existing.
        assert await run_sh(executor, workspace, "cat /etc/passwd") == (
            1,
            b"",
            b"cat: /etc/passwd: Permission denied\n",
        )

        # The stat path answers from the tree: `/etc` is a skeleton directory
        # (0), everything under it is absent (ENOENT). `%F` keeps the assertion
        # exact without pinning a size or a timestamp.
        assert await run_sh(executor, workspace, "stat -c %F /etc") == (
            0,
            b"directory\n",
            b"",
        )
        assert await run_sh(executor, workspace, "stat /etc/passwd") == (
            1,
            b"",
            b"stat: cannot statx '/etc/passwd': No such file or directory\n",
        )

        # ...and *reading* that directory is a different question: the grant
        # covers the workspace, not the skeleton's system directories.
        assert await run_sh(executor, workspace, "ls /etc") == (
            2,
            b"",
            b"ls: cannot open directory '/etc': Permission denied\n",
        )
        assert await run_sh(executor, workspace, "ls /") == (
            0,
            b"bin\ndev\netc\nhome\nlib\nlib64\nmedia\nmnt\nopt\nproc\nroot\nrun\n"
            b"sbin\nsrv\ntmp\nusr\nvar\nworkspace\n",
            b"",
        )

        # A path whose components do not resolve: the lookup fails before the
        # grant is consulted, for the open and the stat path alike.
        assert await run_sh(executor, workspace, "cat /src/host-only/SECRET") == (
            1,
            b"",
            b"cat: /src/host-only/SECRET: No such file or directory\n",
        )
        assert await run_sh(executor, workspace, "stat /src") == (
            1,
            b"",
            b"stat: cannot statx '/src': No such file or directory\n",
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
