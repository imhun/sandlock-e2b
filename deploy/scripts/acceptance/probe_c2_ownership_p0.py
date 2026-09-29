#!/usr/bin/env python3
"""C2 P0 probe: the two NAS facts that decide whether C2 is zero-regression.

C2 (``docs/c2-ownership-frontload.md``) replaces "root chowns the sandbox tree"
with "the pooled uid creates every inode itself". That only works if the
*storage* agrees, and the storage here is an Aliyun NAS over NFSv4.0 with
AUTH_SYS: the server authorizes by the uid/gid inside the credential, so a
client-side capability is worthless and "become X" is the only lever. Two facts
therefore decide it, and neither can be measured off-NFS (a local filesystem
answers "yes" to everything and hides exactly the failures that matter):

  P0-a  what uid 0 can do to *another* uid's ``0600``/``0700`` tree on this NAS
        (read / traverse / delete / chown). Half of it is recorded -- uid 0
        cannot override-read another uid's ``0600`` (2026-09-17; the original
        measurement was the header of ``deploy/k8s/priv-broker.yaml``, retired
        by C3 Task 7 and re-recorded in
        ``docs/production-deployment-requirements.md`` §5.4(b)) -- the
        traverse/delete halves are not.
  P0-b  the sticky-bit and group-bit behaviour C2 leans on: a ``1777``+sticky
        parent must let X create and remove *its own* entries (C2's replacement
        for ``chown`` in shared directories), while ``0770 group=<worker gid>``
        is what today's ``rm``/``walk`` use.

This probe runs **as root inside the cluster** (the runner renders a Job) and
measures a matrix of (identity x fixture x operation) cells by forking a child
that ``setgroups([])`` + ``setgid`` + ``setuid``'s into the subject identity --
exactly the "become X" shape C2 would use. It is safe against the live NAS:

* the only paths it touches are inside ``--root/_probes/c2-p0-<utcstamp>-<pid>``,
  and it refuses to enter that directory if it already exists (no clobbering);
* every fixture is a directory plus one 6-byte file -- no payload, no copy;
* it removes its scratch tree on the way out, trying each identity that owns
  part of it first (a server that refuses root a cross-uid delete must not leave
  litter), then root; leftovers are reported non-zero;
* it never touches anything else: no platform state, no sandbox tree, no secret.

Printed contract (one ``P0-CELL`` line per cell, then the verdicts):

  ``C1-CONTROL=ok|broken:<cell>``   today's model still holds here: the broker's
                                    identity can list/stat/unlink a ``0770`` tree
                                    and its ``chown`` verb succeeds, and the
                                    worker lives off the group bits. Without this
                                    there is no baseline to compare C2 against.
  ``C2-PREMISE=ok|broken:<cell>``   "create as X" really lands owner=X on this NAS
                                    and X can build/tear down its own tree.
  ``P0A-uid0-override=yes|no``      can uid 0 read another uid's ``0600``?
  ``P0A-uid0-needs-the-group``      the same unlink as uid 0 fails without the
                                    tree's group and succeeds with it (the pair
                                    ``C5``/``C6``) -- i.e. the broker's power is
                                    the *group*, not uid 0 magic over the wire.
  ``P0B-sticky=enforced|unexpected``  does the sticky bit really stop a third uid
                                    from unlinking X's entry?
  ``P0B-x-can-chgrp=yes|no``        can the pooled uid give its own directory the
                                    worker's *group*? (``no`` means the
                                    ``0770 owner=X group=<worker gid>`` shape the
                                    C2 plan asserts can only come from a hand-over
                                    -- a decision, not a probe failure.)
  ``P0C-setgid-inheritance=yes|partial|no``
                                    does a one-time setgid on the shared parent
                                    carry the worker's group (and the setgid bit)
                                    down X's whole tree? (``partial`` = right one
                                    level deep, gone below.)
  ``P0C-worker-deletes-via-group``  can the worker unlink inside such a tree
                                    (``umask 007``, ``2770``)? If yes, walk/delete
                                    inside the tree need no "become X".
  ``P0C-sticky-preserved``          ...while the sticky bit still keeps it from
                                    removing another uid's *whole* subtree.
  ``C2-P0-VERDICT=zero-regression|regression:<cell>|unusable:<cell>``

Exit codes: 0 = zero-regression printed, 1 = regression/unusable, 2 = usage,
3 = not root, 4 = wrong filesystem (``--require-fstype``), 5 = scratch left behind.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_USAGE = 2
EXIT_NOT_ROOT = 3
EXIT_FSTYPE = 4
EXIT_CLEANUP = 5

#: The identities the matrix moves between. Defaults come from the deployment
#: (``E2B_UID_POOL_START`` and the worker image's ``USER 65534:65534``); the
#: runner passes them through so a deployment that moves them can re-measure.
WORKER_UID = 65534
WORKER_GID = 65534
POOL_UID = 10000
POOL_GID = 10000
OTHER_UID = 10001
OTHER_GID = 10001

#: The scratch tree is created as a **sticky 1777** directory -- the same shape as
#: the deployment's shared roots (``<workspaces>``, a volume root), and the only
#: mode that lets every subject create *its own* cell: the fixture has to be built
#: by its declared creator (that is C2's premise), so the harness may not make it
#: on the subject's behalf.
SCRATCH_PARENT = "_probes"
SCRATCH_PREFIX = "c2-p0-"
SCRATCH_MODE = 0o1777


@dataclass(frozen=True)
class Identity:
    """An identity the matrix can become (``setgroups([])`` + gid + uid)."""

    name: str
    uid: int
    gid: int


def identities(*, pool_uid: int, pool_gid: int, other_uid: int, other_gid: int,
               worker_uid: int, worker_gid: int) -> dict[str, Identity]:
    """The five subjects: two root variants, the worker, and two pooled uids."""
    return {
        # A plain root container -- the subject of P0-a.
        "root": Identity("root", 0, 0),
        # Today's broker: ``runAsUser: 0`` with the image's ``USER 65534:65534``
        # supplying the gid, i.e. a *member* of the tree's group.
        "broker": Identity("broker", 0, worker_gid),
        "worker": Identity("worker", worker_uid, worker_gid),
        "pool": Identity("pool", pool_uid, pool_gid),
        "other": Identity("other", other_uid, other_gid),
    }


@dataclass(frozen=True)
class FixtureSpec:
    """One directory plus one file, each with its own builder, owner and mode.

    ``*_builder`` is the identity that runs ``mkdir``/``chown``/``chmod``. It is
    part of the measurement, not a convenience: a spec whose owner differs from
    its builder has to be built by root (today's hand-over shape), because a
    non-root builder cannot ``chgrp`` to a group it is not in -- which is itself
    one of the things this probe measures (cell ``A6``).
    """

    name: str
    dir_mode: int
    dir_builder: str
    dir_uid: int
    dir_gid: int
    file_mode: int
    file_builder: str
    file_uid: int
    file_gid: int


def fixtures(worker_uid: int, worker_gid: int, pool_uid: int,
             pool_gid: int) -> dict[str, FixtureSpec]:
    """The four shapes the matrix needs.

    ``sticky-1777`` is the shared-parent shape (``<workspaces>`` tree root, a
    volume root); ``group-0770`` is today's sandbox tree (owner = pooled uid,
    group = the worker's gid); ``owner-0700`` is a tenant-created ``chmod 700`` /
    ``umask 077`` subdirectory; ``open-0755`` is the ordinary world-readable one.

    ``group-0770`` is built by **root on purpose**: that owner/group pair is what
    the *deployment* produces today (the broker chowns the tree). A pooled uid
    cannot chgrp to the worker's group -- see cell ``A6``.
    """
    return {
        # "created as X": X builds it, so the storage's answer is about X.
        "owner-0700": FixtureSpec("owner-0700", 0o700, "pool", pool_uid, pool_gid,
                                  0o600, "pool", pool_uid, pool_gid),
        "open-0755": FixtureSpec("open-0755", 0o755, "pool", pool_uid, pool_gid,
                                 0o644, "pool", pool_uid, pool_gid),
        # "handed over by root": today's sandbox tree, and the shared parent with
        # a file the sandbox itself would have written.
        "group-0770": FixtureSpec("group-0770", 0o770, "root", pool_uid, worker_gid,
                                  0o660, "root", pool_uid, worker_gid),
        "sticky-1777": FixtureSpec("sticky-1777", 0o1777, "root", 0, worker_gid,
                                   0o644, "pool", pool_uid, pool_gid),
        # The exact shape the 2026-09-17 record measured: a ``0600`` file **created
        # by 65534** (the worker), which root then failed to open. Included so the
        # two measurements can be compared like for like (cells E1-E3).
        "worker-0700": FixtureSpec("worker-0700", 0o700, "worker", worker_uid, worker_gid,
                                   0o600, "worker", worker_uid, worker_gid),
        # The candidate answer to the `gid = <worker gid>` decision: a **setgid +
        # sticky** shared parent (`0o3777`, group = the worker's gid), created once
        # by the platform. If the storage lets X's children inherit that group --
        # and the setgid bit -- then "create as X" produces the whole
        # ``owner=X group=<worker gid>`` tree with no hand-over at all, while the
        # sticky bit keeps X from deleting anyone else's tree (cells F1-F4).
        "setgid-parent": FixtureSpec("setgid-parent", 0o3777, "root", 0, worker_gid,
                                     0o644, "root", 0, worker_gid),
    }


# ---------------------------------------------------------------------------
# becoming an identity and doing one thing
# ---------------------------------------------------------------------------


def _become(identity: Identity) -> None:
    """Drop to ``identity`` the way a route-B slot does: no supplementary groups."""
    os.setgroups([])
    os.setgid(identity.gid)
    os.setuid(identity.uid)


def _fork_result(action: Callable[[], str]) -> str:
    """Run ``action`` in a forked child and bring its one-line answer back.

    Nothing raises across the fork: failures come back as strings so the matrix is
    data. ``CRASH:`` means the probe itself is broken and the caller counts the
    cell as broken.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child writes one line and exits
        try:
            os.close(read_fd)
            try:
                result = action()
            except OSError as exc:
                result = "ERR:" + errno.errorcode.get(exc.errno, str(exc.errno))
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                result = "CRASH:" + type(exc).__name__
            with os.fdopen(write_fd, "wb") as handle:
                handle.write((result + "\n").encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as handle:
        raw = handle.read().decode("utf-8", "replace").strip()
    os.waitpid(pid, 0)
    return raw or "CRASH:no-answer"


def run_as(identity: Identity, op: str, path: Path, *, mode: int = 0o700,
           target_uid: int = -1, target_gid: int = -1,
           umask: int | None = None) -> str:
    """Fork, become ``identity``, perform ``op`` on ``path``; return ``OK``/``ERR:..``."""
    def action() -> str:
        _become(identity)
        return _perform(op, path, mode=mode, target_uid=target_uid,
                        target_gid=target_gid, umask=umask)
    return _fork_result(action)


def _perform(op: str, path: Path, *, mode: int, target_uid: int,
             target_gid: int, umask: int | None = None) -> str:
    if umask is not None:
        # The deployment's own lever: `mkdir` mode is masked by the umask, so a
        # sandbox that must produce group-writable directories has to run with
        # `umask 007` (022 would strip the group write bit).
        os.umask(umask)
    if op == "stat":
        os.stat(path)
    elif op == "listdir":
        os.listdir(path)
    elif op == "read":
        with open(path, "rb") as handle:
            handle.read()
    elif op == "write":
        with open(path, "ab") as handle:
            handle.write(b"x")
    elif op == "unlink":
        os.unlink(path)
    elif op == "rmdir":
        os.rmdir(path)
    elif op == "mkdir":
        os.mkdir(path, mode)
    elif op == "rmtree":
        shutil.rmtree(path)
    elif op == "chmod":
        os.chmod(path, mode)
    elif op == "chown":
        os.chown(path, target_uid, target_gid)
    else:  # pragma: no cover - the matrix below only names real ops
        return "CRASH:unknown-op-" + op
    return "OK"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def create_as(identity: Identity, path: Path, mode: int, *, directory: bool,
              umask: int | None = None) -> str:
    """Fork a child that creates ``path`` (directory or file) as ``identity``."""
    def action() -> str:
        _become(identity)
        if umask is not None:
            os.umask(umask)
        if directory:
            os.mkdir(path, mode)
        else:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, mode)
            try:
                os.write(fd, b"c2-p0\n")
            finally:
                os.close(fd)
        return "OK"
    return _fork_result(action)


def _shape(identity: Identity, path: Path, mode: int, uid: int, gid: int, *,
           directory: bool) -> str:
    """``identity`` creates ``path``, then fixes owner/mode to the spec's."""
    result = create_as(identity, path, mode, directory=directory)
    if result != "OK":
        return f"create -> {result}"
    result = run_as(identity, "chown", path, target_uid=uid, target_gid=gid)
    if result != "OK":
        return f"chown {uid}:{gid} -> {result}"
    result = run_as(identity, "chmod", path, mode=mode)
    if result != "OK":
        return f"chmod {oct(mode)} -> {result}"
    return "OK"


def build_fixture(spec: FixtureSpec, directory: Path,
                  idents: dict[str, Identity]) -> None:
    """Create the cell's directory plus one file exactly as the spec says.

    Every step is checked: a fixture that is not what the spec says would make the
    cell answer a different question, which is worse than failing loudly here.
    """
    result = _shape(idents[spec.dir_builder], directory, spec.dir_mode,
                    spec.dir_uid, spec.dir_gid, directory=True)
    if result != "OK":
        raise RuntimeError(f"dir: {result}")
    payload = directory / "payload"
    result = _shape(idents[spec.file_builder], payload, spec.file_mode,
                    spec.file_uid, spec.file_gid, directory=False)
    if result != "OK":
        raise RuntimeError(f"payload: {result}")
    if not payload.is_file() or payload.stat().st_size != len(b"c2-p0\n"):
        raise RuntimeError("payload missing or wrong size")


def measured_fixture(directory: Path) -> dict[str, int]:
    """What the *storage* says the fixture is (owner/mode of dir and file)."""
    info = os.stat(directory)
    payload = os.stat(directory / "payload")
    return {
        "dir_mode": stat.S_IMODE(info.st_mode),
        "dir_uid": info.st_uid,
        "dir_gid": info.st_gid,
        "file_mode": stat.S_IMODE(payload.st_mode),
        "file_uid": payload.st_uid,
        "file_gid": payload.st_gid,
    }


# ---------------------------------------------------------------------------
# the matrix
# ---------------------------------------------------------------------------


class Ctx:
    """One cell: a freshly built fixture plus the identities."""

    def __init__(self, spec: FixtureSpec, directory: Path,
                 idents: dict[str, Identity], worker_gid: int) -> None:
        self.spec = spec
        self.dir = directory
        self.file = directory / "payload"
        self.worker_gid = worker_gid
        self._idents = idents

    def ident(self, name: str) -> Identity:
        return self._idents[name]

    def run(self, subject: str, op: str, path: Path | None = None, **kw) -> str:
        return run_as(self.ident(subject), op,
                      path if path is not None else self.file, **kw)


#: ``(name, fixture, action)``. ``action`` may fork several times (the
#: "create your own entry, then remove it" cells do). Every cell gets its own
#: fixture, so a deleting cell cannot poison a later one.
CHECKS: list[tuple[str, str, Callable[[Ctx], str]]] = []


def _cell(name: str, fixture: str) -> Callable[[Callable[[Ctx], str]], None]:
    def wrap(fn: Callable[[Ctx], str]) -> None:
        CHECKS.append((name, fixture, fn))
    return wrap


# --- C2's premise: "created as X" lands as X, and X can undo its own tree ----


@_cell("A1-fixture-ownership", "owner-0700")
def _a1(ctx: Ctx) -> str:
    """C2's premise: "created as X" really lands as X (nothing squashed).

    Reads back the *storage's* answer, so it also catches a NAS that maps the
    credential to a different uid, drops the setgid/sticky bits, or ignores the
    requested mode.
    """
    seen = measured_fixture(ctx.dir)
    same = (
        seen["dir_uid"] == ctx.spec.dir_uid
        and seen["dir_gid"] == ctx.spec.dir_gid
        and seen["dir_mode"] == ctx.spec.dir_mode
        and seen["file_uid"] == ctx.spec.file_uid
        and seen["file_mode"] == ctx.spec.file_mode
    )
    return "OK" if same else f"ERR:MISMATCH:{seen}"


@_cell("A6-as-X-chgrp-to-worker-gid", "owner-0700")
def _a6(ctx: Ctx) -> str:
    """Can X give its own directory the worker's *group*?

    The C2 plan's §5.2 asserts the tree stays ``0770 owner=X group=<worker gid>``.
    A pooled uid is not in the worker's group, so POSIX says no -- and this cell
    says whether this storage agrees. ``no`` is not a probe failure: it means that
    owner/group pair can only come from a hand-over, which is the decision the
    design has to make explicitly.
    """
    return ctx.run("pool", "chown", ctx.dir,
                   target_uid=ctx.ident("pool").uid, target_gid=ctx.worker_gid)


@_cell("A2-as-X-rmtree-own-tree", "owner-0700")
def _a2(ctx: Ctx) -> str:
    """X tears down its own ``0700``/``0600`` tree (C2's replacement for ``rm``)."""
    return ctx.run("pool", "rmtree", ctx.dir)


@_cell("A3-as-X-create-in-1777", "sticky-1777")
def _a3(ctx: Ctx) -> str:
    """X creates its own entry in a ``1777``+sticky shared parent."""
    return ctx.run("pool", "mkdir", ctx.dir / "pool-entry")


@_cell("A4-as-X-create-and-remove-in-1777", "sticky-1777")
def _a4(ctx: Ctx) -> str:
    """...and removes that same entry: the sticky rule must let the owner out."""
    entry = ctx.dir / "pool-entry"
    created = ctx.run("pool", "mkdir", entry)
    if created != "OK":
        return created
    return ctx.run("pool", "rmdir", entry)


@_cell("A5-as-X-write-in-own-0770", "group-0770")
def _a5(ctx: Ctx) -> str:
    """X writes inside its own ``0770`` tree (today's sandbox-tree shape)."""
    return ctx.run("pool", "write")


# --- C1's control: today's model must still hold, or there is no baseline ----


@_cell("B1-broker-listdir-0770", "group-0770")
def _b1(ctx: Ctx) -> str:
    return ctx.run("broker", "listdir", ctx.dir)


@_cell("B2-broker-stat-0660", "group-0770")
def _b2(ctx: Ctx) -> str:
    return ctx.run("broker", "stat")


@_cell("B3-broker-unlink-0660", "group-0770")
def _b3(ctx: Ctx) -> str:
    return ctx.run("broker", "unlink")


@_cell("B4-broker-chown-verb", "open-0755")
def _b4(ctx: Ctx) -> str:
    """The ``chown`` verb C1 externalised: root hands a file to another uid."""
    return ctx.run("broker", "chown",
                   target_uid=ctx.ident("other").uid, target_gid=ctx.ident("other").gid)


@_cell("B5-worker-group-bits", "group-0770")
def _b5(ctx: Ctx) -> str:
    """The worker lives off the group bits today (the dir's group is its own gid)."""
    listed = ctx.run("worker", "listdir", ctx.dir)
    if listed != "OK":
        return listed
    read = ctx.run("worker", "read")
    if read != "OK":
        return read
    return ctx.run("worker", "unlink")


# --- P0-a: what uid 0 can do to another uid's private tree ------------------


@_cell("C1-uid0-read-0600", "owner-0700")
def _c1(ctx: Ctx) -> str:
    return ctx.run("root", "read")


@_cell("C2-uid0-listdir-0700", "owner-0700")
def _c2(ctx: Ctx) -> str:
    return ctx.run("root", "listdir", ctx.dir)


@_cell("C3-uid0-unlink-inside-0700", "owner-0700")
def _c3(ctx: Ctx) -> str:
    return ctx.run("root", "unlink")


@_cell("C4-uid0-unlink-inside-0755", "open-0755")
def _c4(ctx: Ctx) -> str:
    """A file in a world-readable, not world-writable directory."""
    return ctx.run("root", "unlink")


@_cell("C5-uid0-unlink-inside-0770", "group-0770")
def _c5(ctx: Ctx) -> str:
    """uid 0 *without* the tree's group: is ``0770`` enough for root over the wire?"""
    return ctx.run("root", "unlink")


@_cell("C6-broker-unlink-inside-0770", "group-0770")
def _c6(ctx: Ctx) -> str:
    """Same uid, with the tree's group: the line C5 must be read against."""
    return ctx.run("broker", "unlink")


# --- P0-b: sticky and group bits in the shared shapes ----------------------


@_cell("D1-other-unlink-in-1777", "sticky-1777")
def _d1(ctx: Ctx) -> str:
    """A third uid must *not* be able to unlink X's entry (sticky enforced)."""
    return ctx.run("other", "unlink")


@_cell("D2-worker-unlink-in-1777", "sticky-1777")
def _d2(ctx: Ctx) -> str:
    """The worker cannot clean X's entry in a sticky parent either."""
    return ctx.run("worker", "unlink")


@_cell("D3-worker-unlink-in-0770", "group-0770")
def _d3(ctx: Ctx) -> str:
    """The group shape is what makes the worker's ``rm``/``walk`` work today."""
    return ctx.run("worker", "unlink")


@_cell("D4-worker-rmtree-0700", "owner-0700")
def _d4(ctx: Ctx) -> str:
    """A tenant-created ``0700`` subdirectory: the worker cannot even enter it."""
    return ctx.run("worker", "rmtree", ctx.dir)


# --- the 2026-09-17 record, re-measured like for like -----------------------


@_cell("E1-uid0-read-worker-0600", "worker-0700")
def _e1(ctx: Ctx) -> str:
    """The recorded claim: a ``0600`` file **created by 65534**, opened by uid 0.

    ``worker-root.patch.yaml`` (deleted by C1, in git history) recorded EACCES here
    and used it to argue the broker must keep the worker's *group*. Re-measuring it
    is what lets the record and today's storage be compared directly.
    """
    return ctx.run("root", "read")


@_cell("E2-broker-read-worker-0600", "worker-0700")
def _e2(ctx: Ctx) -> str:
    """Same file, uid 0 *with* the worker's gid (today's broker identity)."""
    return ctx.run("broker", "read")


@_cell("E3-uid0-listdir-worker-0700", "worker-0700")
def _e3(ctx: Ctx) -> str:
    """...and traversing the worker-owned ``0700`` directory around it."""
    return ctx.run("root", "listdir", ctx.dir)


# --- can a one-time setgid on the shared parent carry the worker's group? ----


def _gid_mode(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_gid, stat.S_IMODE(info.st_mode)


@_cell("F1-as-X-child-inherits-worker-group", "setgid-parent")
def _f1(ctx: Ctx) -> str:
    """X creates a directory inside the platform's setgid parent: whose group?

    ``umask 007`` is the other half of the recipe: the mode X asks for is masked by
    its umask, so a group-writable tree requires the sandbox to run with `007`
    (with `022` the child loses the group write bit and the worker cannot delete
    inside it -- see ``F3``).
    """
    child = ctx.dir / "pool-child"
    created = ctx.run("pool", "mkdir", child, mode=0o770, umask=0o007)
    if created != "OK":
        return created
    gid, _ = _gid_mode(child)
    return "OK" if gid == ctx.worker_gid else f"ERR:gid={gid}"


@_cell("F1b-child-keeps-the-setgid-bit", "setgid-parent")
def _f1b(ctx: Ctx) -> str:
    """...and does the storage keep the setgid bit on it (so *grand*children inherit)?"""
    child = ctx.dir / "pool-child"
    created = ctx.run("pool", "mkdir", child, mode=0o770, umask=0o007)
    if created != "OK":
        return created
    _, mode = _gid_mode(child)
    return "OK" if mode & stat.S_ISGID else f"ERR:setgid-cleared:mode={oct(mode)}"


@_cell("F2-as-X-grandchild-still-worker-group", "setgid-parent")
def _f2(ctx: Ctx) -> str:
    """Two levels down: does the group still come out as the worker's?"""
    child = ctx.dir / "pool-child"
    created = ctx.run("pool", "mkdir", child, mode=0o770, umask=0o007)
    if created != "OK":
        return created
    grandchild = child / "deeper"
    created = ctx.run("pool", "mkdir", grandchild, mode=0o770, umask=0o007)
    if created != "OK":
        return created
    gid, _ = _gid_mode(grandchild)
    return "OK" if gid == ctx.worker_gid else f"ERR:gid={gid}"


@_cell("F3-worker-deletes-inside-inherited-tree", "setgid-parent")
def _f3(ctx: Ctx) -> str:
    """The worker (65534) unlinks a file inside X's setgid-inherited directory."""
    child = ctx.dir / "pool-child"
    created = ctx.run("pool", "mkdir", child, mode=0o770, umask=0o007)
    if created != "OK":
        return created
    payload = child / "tenant-file"
    created = create_as(ctx.ident("pool"), payload, 0o660, directory=False, umask=0o007)
    if created != "OK":
        return created
    return ctx.run("worker", "unlink", payload)


@_cell("F4-worker-removes-X-subtree", "setgid-parent")
def _f4(ctx: Ctx) -> str:
    """The worker must **not** be able to remove X's subtree: the sticky bit holds.

    (The setgid change must not cost the shared root its cross-uid protection --
    if the worker *can* remove another uid's tree here, the shared root became
    unsafe for a different reason.)
    """
    child = ctx.dir / "pool-child"
    created = ctx.run("pool", "mkdir", child, mode=0o770, umask=0o007)
    if created != "OK":
        return created
    result = ctx.run("worker", "rmtree", child)
    return "OK" if result.startswith("ERR:") else f"ERR:worker-could-remove:{result}"


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

CONTROL_CELLS = (
    "B1-broker-listdir-0770", "B2-broker-stat-0660", "B3-broker-unlink-0660",
    "B4-broker-chown-verb", "B5-worker-group-bits",
)
PREMISE_CELLS = (
    "A1-fixture-ownership", "A2-as-X-rmtree-own-tree", "A3-as-X-create-in-1777",
    "A4-as-X-create-and-remove-in-1777", "A5-as-X-write-in-own-0770",
)
VERDICT_KEYS = (
    "C1-CONTROL", "C2-PREMISE", "P0A-uid0-override", "P0A-uid0-needs-the-group",
    "P0A-uid0-record-check", "P0B-sticky", "P0B-x-can-chgrp",
    "P0C-setgid-inheritance", "P0C-worker-deletes-via-group",
    "P0C-sticky-preserved", "C2-P0-VERDICT",
)


def _first_broken(rows: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        if rows.get(name) != "OK":
            return f"{name}={rows.get(name, 'MISSING')}"
    return None


def verdicts(rows: dict[str, str]) -> dict[str, str]:
    """Classify the matrix. A pure function of ``rows`` -- pinned by unit tests.

    ``unusable`` means "there is no baseline here" (today's model does not behave
    the way production says it does, so the measurement is not about C2 at all).
    ``regression`` means "C2's substitution does not work on this NAS". Everything
    else is ``zero-regression``, with the interesting deltas (what uid 0 can no
    longer do) reported as their own keys.
    """
    # A cell the harness could not even build ("FIXTURE:"/"CRASH:") is not a
    # measurement: it must never be read as "the storage said no".
    unknown = next(
        (f"{name}={value}" for name, value in rows.items()
         if value.startswith(("FIXTURE:", "CRASH:"))),
        None,
    )
    if unknown is not None:
        return {
            "C1-CONTROL": "unknown",
            "C2-PREMISE": "unknown",
            "P0A-uid0-override": "unknown",
            "P0A-uid0-needs-the-group": "unknown",
            "P0A-uid0-record-check": "unknown",
            "P0B-sticky": "unknown",
            "P0B-x-can-chgrp": "unknown",
            "P0C-setgid-inheritance": "unknown",
            "P0C-worker-deletes-via-group": "unknown",
            "P0C-sticky-preserved": "unknown",
            "C2-P0-VERDICT": f"unusable:{unknown}",
        }

    control = _first_broken(rows, CONTROL_CELLS)
    premise = _first_broken(rows, PREMISE_CELLS)
    uid0_read = rows.get("C1-uid0-read-0600", "MISSING")
    record_read = rows.get("E1-uid0-read-worker-0600", "MISSING")
    sticky = rows.get("D1-other-unlink-in-1777", "MISSING")
    chgrp = rows.get("A6-as-X-chgrp-to-worker-gid", "MISSING")
    inherit_child = rows.get("F1-as-X-child-inherits-worker-group", "MISSING")
    inherit_bit = rows.get("F1b-child-keeps-the-setgid-bit", "MISSING")
    inherit_deep = rows.get("F2-as-X-grandchild-still-worker-group", "MISSING")
    out = {
        "C1-CONTROL": "ok" if control is None else f"broken:{control}",
        "C2-PREMISE": "ok" if premise is None else f"broken:{premise}",
        "P0A-uid0-override": (
            "yes" if uid0_read == "OK"
            else "no" if uid0_read.startswith("ERR:")
            else "unknown"
        ),
        "P0A-uid0-needs-the-group": (
            "yes"
            if rows.get("C5-uid0-unlink-inside-0770", "MISSING") != "OK"
            and rows.get("C6-broker-unlink-inside-0770") == "OK"
            else "no"
        ),
        # The 2026-09-17 record claimed EACCES for exactly this cell.
        "P0A-uid0-record-check": (
            "matches-record" if record_read.startswith("ERR:")
            else "no-longer" if record_read == "OK"
            else "unknown"
        ),
        "P0B-sticky": (
            "enforced" if sticky.startswith("ERR:")
            else "unexpected" if sticky == "OK"
            else "unknown"
        ),
        "P0B-x-can-chgrp": (
            "yes" if chgrp == "OK" else "no" if chgrp.startswith("ERR:") else "unknown"
        ),
        # The candidate answer to the `gid = <worker gid>` decision: one setgid bit
        # on the shared parent, no hand-over. `partial` = the group comes out right
        # for the first level but the bit (and therefore the next level) does not.
        "P0C-setgid-inheritance": (
            "yes" if (inherit_child, inherit_bit, inherit_deep) == ("OK", "OK", "OK")
            else "partial" if inherit_child == "OK"
            else "no" if inherit_child.startswith("ERR:")
            else "unknown"
        ),
        "P0C-worker-deletes-via-group": (
            "yes" if rows.get("F3-worker-deletes-inside-inherited-tree") == "OK"
            else "no" if str(rows.get("F3-worker-deletes-inside-inherited-tree", "MISSING")).startswith("ERR:")
            else "unknown"
        ),
        "P0C-sticky-preserved": (
            "yes" if rows.get("F4-worker-removes-X-subtree") == "OK"
            else "no" if str(rows.get("F4-worker-removes-X-subtree", "MISSING")).startswith("ERR:")
            else "unknown"
        ),
    }
    if control is not None:
        out["C2-P0-VERDICT"] = f"unusable:{control}"
    elif premise is not None:
        out["C2-P0-VERDICT"] = f"regression:{premise}"
    else:
        out["C2-P0-VERDICT"] = "zero-regression"
    return out


# ---------------------------------------------------------------------------
# guards, mountinfo, cleanup
# ---------------------------------------------------------------------------


def root_refusal(euid: int) -> str | None:
    """The matrix *is* the identity switch; without root there is nothing to do."""
    if euid != 0:
        return (
            "must run as root (uid 0): every cell forks a child that setgid/setuid's "
            "into the subject identity, and this process is the only one that may. "
            "Run it through deploy/scripts/acceptance/c2-p0-probe.sh."
        )
    return None


def root_path_refusal(root: Path) -> str | None:
    if not root.is_dir():
        return f"--root {root} does not exist or is not a directory"
    if root.resolve() == Path("/"):
        return "--root must not be /"
    return None


def fstype_refusal(fstype: str | None, required: str | None) -> str | None:
    """Refuse to *record* a verdict on the wrong filesystem when asked to.

    An unknown filesystem counts as a refusal too: the point of ``--require-fstype``
    is to prove the answer came from the NAS, and "could not tell" cannot prove it.
    """
    if required is None:
        return None
    if fstype is not None and fstype.startswith(required):
        return None
    if fstype is None:
        return (
            f"--require-fstype {required}: could not determine the filesystem of the "
            "scratch dir (/proc/self/mountinfo unreadable) -- not recording a verdict."
        )
    return (
        f"--require-fstype {required}: the scratch dir is on {fstype!r}. The whole "
        "point of this probe is the NAS answer -- a local filesystem says yes to "
        "everything and hides the failures that matter."
    )


def mount_from_mountinfo(text: str, target: str) -> tuple[str | None, str | None]:
    """``(fstype, mountpoint)`` of the most specific mount covering ``target``.

    Split out of :func:`mount_fstype` so the parser is pinned by unit tests instead
    of by whatever the machine running the tests happens to have mounted.
    """
    best: tuple[int, str, str] | None = None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or "-" not in fields:
            continue
        dash = fields.index("-")
        if dash + 1 >= len(fields):
            continue
        mountpoint = fields[4].replace("\\040", " ")
        if not (target == mountpoint
                or target.startswith(mountpoint.rstrip("/") + "/")):
            continue
        candidate = (len(mountpoint), fields[dash + 1], mountpoint)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None, None
    return best[1], best[2]


def mount_fstype(path: Path) -> tuple[str | None, str | None]:
    """``(fstype, mountpoint)`` of the most specific mount covering ``path``."""
    try:
        text = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return None, None
    return mount_from_mountinfo(text, str(path.resolve()))


MOUNTINFO_FIELD_MOUNTPOINT = 4
PROC_MOUNTS_FIELD_OPTIONS = 3


def mount_options(mountpoint: str | None) -> str:
    """The options column of ``/proc/mounts`` for ``mountpoint`` (``unknown`` if gone).

    Recorded because the whole P0-a question ("can uid 0 override another uid's
    tree?") is a question about the *server*: the options here show which mount the
    measurement actually went through (``vers=``, and whether anything root-related
    is on it).
    """
    if mountpoint is None:
        return "unknown"
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return "unknown"
    for line in lines:
        fields = line.split()
        if len(fields) > PROC_MOUNTS_FIELD_OPTIONS and fields[1] == mountpoint:
            return fields[PROC_MOUNTS_FIELD_OPTIONS]
    return "unknown"


def effective_caps() -> str:
    """``CapEff`` of this process (``/proc/self/status``), or ``unknown``.

    Part of the answer, not decoration: a uid-0 subject *with* ``CAP_DAC_OVERRIDE``
    can be allowed by the client before the request reaches the server, so the
    "root with" and "root without the override cap" arms must never be confused.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def scratch_path(root: Path, *, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ",
                          time.gmtime(time.time() if now is None else now))
    return root / SCRATCH_PARENT / f"{SCRATCH_PREFIX}{stamp}-{os.getpid()}"


def cleanup(path: Path, idents: dict[str, Identity],
            out: Callable[[str], None],
            cells: list[tuple[Path, str]] | None = None) -> bool:
    """Remove the scratch tree, **each cell by the identity that built it**.

    Root-last is not enough on this storage: the whole question P0-a asks is
    whether uid 0 can delete another uid's entries, so a cell whose owner is a
    pooled uid may be undeletable by root over the wire. The owner removes its own
    cell; root then removes the scratch directory itself (which root owns).
    """
    for cell_dir, builder in cells or []:
        if not cell_dir.exists():
            continue
        result = run_as(idents[builder], "rmtree", cell_dir)
        out(f"P0-CLEANUP cell={cell_dir.name} as={builder} result={result}")
        if cell_dir.exists() and builder != "root":
            result = run_as(idents["root"], "rmtree", cell_dir)
            out(f"P0-CLEANUP cell={cell_dir.name} as=root result={result}")
    if not path.exists():
        return True
    if cells is None:  # no per-cell plan: fall back to the blunt owner order
        for name in ("pool", "other", "worker", "root"):
            if not path.exists():
                return True
            result = run_as(idents[name], "rmtree", path)
            out(f"P0-CLEANUP as={name} result={result}")
    if path.exists():
        # Last resort for a shape whose owning identity is gone from the matrix:
        # make it traversable as root, then remove it as root.
        for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                os.chmod(child, 0o777 if child.is_dir() else 0o666)
            except OSError:
                pass
        try:
            os.chmod(path, 0o777)
            shutil.rmtree(path)
        except OSError as exc:
            out(f"P0-CLEANUP root result=ERR:{exc}")
    return not path.exists()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="C2 P0 probe: uid-0 semantics and sticky/group bits on the store",
    )
    parser.add_argument("--root", required=True,
                        help="the shared export as the deployment mounts it, "
                             "e.g. /var/lib/e2b-sandboxes")
    parser.add_argument("--worker-uid", type=int, default=WORKER_UID)
    parser.add_argument("--worker-gid", type=int, default=WORKER_GID)
    parser.add_argument("--pool-uid", type=int, default=POOL_UID)
    parser.add_argument("--pool-gid", type=int, default=POOL_GID)
    parser.add_argument("--other-uid", type=int, default=OTHER_UID)
    parser.add_argument("--other-gid", type=int, default=OTHER_GID)
    parser.add_argument("--require-fstype", default=None,
                        help="refuse (exit 4) unless the scratch dir is on this "
                             "filesystem, e.g. 'nfs' -- use it for the real run")
    parser.add_argument("--print-plan", action="store_true",
                        help="print the identity/fixture/matrix plan and exit "
                             "(touches nothing)")
    parser.add_argument("--json", action="store_true",
                        help="also print one P0-JSON line with the whole matrix")
    return parser.parse_args(argv)


def print_plan(args: argparse.Namespace) -> int:
    idents = identities(
        pool_uid=args.pool_uid, pool_gid=args.pool_gid, other_uid=args.other_uid,
        other_gid=args.other_gid, worker_uid=args.worker_uid, worker_gid=args.worker_gid,
    )
    specs = fixtures(args.worker_uid, args.worker_gid, args.pool_uid, args.pool_gid)
    print(f"PLAN root={args.root} scratch_parent={args.root}/{SCRATCH_PARENT} "
          f"require_fstype={args.require_fstype or '(any)'}")
    for name, ident in idents.items():
        print(f"PLAN identity={name} uid={ident.uid} gid={ident.gid}")
    for name, spec in specs.items():
        print(f"PLAN fixture={name} "
              f"dir={oct(spec.dir_mode)}:{spec.dir_uid}:{spec.dir_gid}"
              f"@built-by-{spec.dir_builder} "
              f"file={oct(spec.file_mode)}:{spec.file_uid}:{spec.file_gid}"
              f"@built-by-{spec.file_builder}")
    for cell, fixture_name, _ in CHECKS:
        print(f"PLAN cell={cell} fixture={fixture_name}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    def out(line: str) -> None:
        print(line, flush=True)

    if args.print_plan:
        return print_plan(args)

    refusal = root_refusal(os.geteuid())
    if refusal is not None:
        print(f"REFUSE: {refusal}", file=sys.stderr)
        return EXIT_NOT_ROOT

    root = Path(args.root)
    refusal = root_path_refusal(root)
    if refusal is not None:
        print(f"REFUSE: {refusal}", file=sys.stderr)
        return EXIT_USAGE

    scratch = scratch_path(root)
    parent = root / SCRATCH_PARENT
    parent.mkdir(mode=SCRATCH_MODE, exist_ok=True)
    # Explicit: the umask strips the write bits an `mkdir(mode=…)` would ask for.
    os.chmod(parent, SCRATCH_MODE)
    if scratch.exists():
        print(f"REFUSE: {scratch} already exists (never reuse a scratch tree)",
              file=sys.stderr)
        return EXIT_USAGE
    scratch.mkdir(mode=SCRATCH_MODE)
    os.chmod(scratch, SCRATCH_MODE)

    fstype, mountpoint = mount_fstype(scratch)
    out(f"P0-ROOT path={root} resolved={root.resolve()}")
    out(f"P0-FS fstype={fstype or 'unknown'} mountpoint={mountpoint or 'unknown'} "
        f"scratch={scratch}")
    out(f"P0-CAPS CapEff={effective_caps()} (the `root`/`broker` subjects inherit "
        f"this set)")
    out(f"P0-MOUNT mountpoint={mountpoint or 'unknown'} "
        f"opts={mount_options(mountpoint)}")
    refusal = fstype_refusal(fstype, args.require_fstype)
    if refusal is not None:
        print(f"REFUSE: {refusal}", file=sys.stderr)
        shutil.rmtree(scratch, ignore_errors=True)
        return EXIT_FSTYPE

    idents = identities(
        pool_uid=args.pool_uid, pool_gid=args.pool_gid, other_uid=args.other_uid,
        other_gid=args.other_gid, worker_uid=args.worker_uid, worker_gid=args.worker_gid,
    )
    specs = fixtures(args.worker_uid, args.worker_gid, args.pool_uid, args.pool_gid)
    out("P0-IDENTITIES " + " ".join(
        f"{name}={ident.uid}:{ident.gid}" for name, ident in idents.items()
    ))

    rows: dict[str, str] = {}
    measurements: dict[str, dict[str, int]] = {}
    built: list[tuple[Path, str]] = []
    code = EXIT_OK
    try:
        for cell, fixture_name, action in CHECKS:
            spec = specs[fixture_name]
            # The fixture *is* the cell: built by its declared creator inside the
            # sticky scratch, so owner/mode are the storage's answer, not ours.
            fixture_dir = scratch / cell
            built.append((fixture_dir, spec.dir_builder))
            try:
                build_fixture(spec, fixture_dir, idents)
            except (RuntimeError, OSError) as exc:
                rows[cell] = f"FIXTURE:{exc}"
            else:
                if cell.startswith("A1-"):
                    measurements[fixture_name] = measured_fixture(fixture_dir)
                try:
                    rows[cell] = action(Ctx(spec, fixture_dir, idents, args.worker_gid))
                except OSError as exc:
                    rows[cell] = "ERR:" + errno.errorcode.get(exc.errno, str(exc.errno))
                except Exception as exc:  # noqa: BLE001
                    rows[cell] = f"CRASH:{type(exc).__name__}"
            out(f"P0-CELL name={cell} fixture={fixture_name} result={rows[cell]}")
        if args.json:
            out("P0-JSON " + json.dumps(
                {
                    "cells": rows,
                    "fixtures": measurements,
                    "fstype": fstype,
                    "mountpoint": mountpoint,
                    "identities": {n: [i.uid, i.gid] for n, i in idents.items()},
                },
                ensure_ascii=False, sort_keys=True,
            ))
        verdict = verdicts(rows)
        if verdict["C2-P0-VERDICT"] != "zero-regression":
            code = EXIT_REGRESSION
    finally:
        clean = cleanup(scratch, idents, out, built)
        try:
            parent.rmdir()
        except OSError:
            pass
        verdict = verdicts(rows)
        for key in VERDICT_KEYS:
            out(f"P0-VERDICT {key}={verdict[key]}")
        out(f"P0-CLEANUP scratch={scratch} removed={'yes' if clean else 'NO'}")
        if not clean:
            code = EXIT_CLEANUP
        out(f"P0-EXIT {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
