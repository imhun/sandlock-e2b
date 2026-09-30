"""File-capability brokers for a **non-root** worker (Track F / Task F1).

Why brokers at all: route B needs one ``sandlock-supervise`` per sandbox
running *as that sandbox's own host uid*, and that is the one step a uid-65534
worker cannot perform -- its ``CapEff`` is empty, so ``setuid(X)`` is EPERM
(the F1 probe report; user namespaces are not an option on the target hosts --
``newuidmap`` refuses every non-``SYS_ADMIN`` shape there).

Everything else is deliberately *not* delegated any more (fix round 1,
裁定 c1): the worker is the data-plane owner of every sandbox tree (files API,
watcher, command logs, snapshots, lifecycle -- all of which must work while a
sandbox is paused, frozen or gone), so its access is expressed as a
**permission**: ``0770 owner=<sandbox uid> group=<worker gid>``. The broker
keeps ``chown`` (the worker is in the group, not the owner, so it cannot hand
a tree to a pooled uid or reclaim an orphan by itself) and ``rm``/``walk``
only as the fallback for trees group access cannot reach: a sandbox-made
``0700`` subdirectory, a ``1777`` volume root, or a root-owned leftover from
before the cut-over.

So two **compiled** binaries carry the capability as a *file capability*
(``setcap`` xattr, applied in the final image stage -- ``COPY --from`` does
not preserve xattrs), live in ``/var/lib/e2b-priv`` (root-owned, mode 0700,
outside every sandbox mount view) and do the privileged step themselves:

* ``e2b-slot-spawn`` (``cap_setuid,cap_setgid+ep``) --
  ``spawn --uid X --gid X -- <sandlock-supervise abs path> <args...>``:
  ``setgroups([])`` → ``setgid(X)`` → ``setuid(X)`` → ``execve``. The program
  is **pinned**: the broker only ever launches the wheel's ``sandlock-supervise``
  absolute path with a uid from the configured pool, so it is not a general
  "run this as uid X" primitive. The slot starts with ``CapEff=0`` because a
  capability-carrying process loses permitted/effective on ``setuid`` and the
  exec'd binary has no file capabilities of its own.
* ``e2b-maint`` (``cap_chown,cap_dac_override+ep``) --
  ``chown --uid X [--recursive] --path P`` / ``rm --path P`` /
  ``walk --path P`` for paths that resolve under ``<workspace_base>/``,
  ``<state_base>/`` (N27's ``E2B_STATE_BASE``, where the platform's own
  records live) or ``<shared_volume_root>/`` only (``realpath``, so ``..`` and
  symlinks cannot escape).

Both link one shared validator (``c3_agent/priv/priv_common.c``) so the pool
range / root whitelist / argument shapes cannot drift apart.

``E2B_PRIV_HELPERS``:

* ``auto`` (default): a non-root worker that ships *both* brokers uses them --
  which is what turns per-sandbox host uids and route-B slots on for the
  production non-root shape. A broker pair that is present but incomplete
  (one binary missing, capability stripped, reachable by a sandbox, or a
  route-B scratch root the maintenance broker cannot reach) **fails closed**:
  a half-installed privileged broker must be named, never guessed at. A
  worker with no brokers at all keeps today's in-process (E5.1) shape and
  says so once at startup.
* ``off``: never use the brokers (today's behaviour everywhere). The escape
  hatch for environments that cannot carry file capabilities.

``E2B_PRIV_HELPER_TRANSPORT`` picks *how* a maintenance request travels:
``exec`` runs the file-capability binary here, in this process's namespaces,
and ``agent`` is C3's shape -- the request is not an argv at all but
``{sandbox_id, op}``, and it travels to the control plane, which instructs the
per-node agent that executes it (hard rules 1/3, §14.4). ``auto`` (the
default) is ``agent`` when that shape is configured and ``exec`` otherwise.
The two share nothing but the validator discipline: under ``agent`` this
module's argv builders and the file-capability binaries are not used at all,
and :mod:`envd_service.agent_fileops` is the client instead.

C1's per-node root broker -- a ``socket`` transport that handed the argv to one
daemon per node over ``E2B_PRIV_HELPER_SOCKET`` -- is **retired** (C3 Task 7).
There is no third transport any more: a deployment either execs the
file-capability binary or routes to the agent, decided once at startup.

``e2b-slot-spawn`` is never externalized: a route-B slot has to start inside
the worker's own namespaces, so :meth:`PrivHelpers.spawn_argv` always runs the
local binary.

A root worker is untouched: root already has the capabilities, and route B
keeps using its own privileged starter.
"""

from __future__ import annotations

import logging
import os
import stat
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

#: The fallback branches of :func:`remove_tree` / :func:`dir_size` log why the
#: in-process attempt failed and that the broker is taking over. This module had
#: no logger at all (review W7 / W7-4), so the ``e2b-maint rm`` fallback raised
#: ``NameError`` *before* the broker call: every tree the worker's own DAC could
#: not reach (a sandbox-made ``0700`` subdirectory, a sealed ``0555`` directory,
#: a root-owned leftover) made the teardown fail instead of delegating it, and
#: the broker that exists for exactly that shape was never executed.
logger = logging.getLogger(__name__)

#: Where the image installs the brokers. Deliberately **not** ``/usr/local``
#: or ``/opt``: the pure shape's Landlock rules cover those prefixes, so a
#: broker there would be reachable from a sandbox (which is exactly the
#: threat model line "whoever can exec the broker gets its capability").
#:
#: The directory is root-owned with the **worker's gid** and mode 0710 (the
#: binaries 0750): the worker (uid 65534, gid 65534) can traverse and exec
#: them, while a sandbox uid (a pool uid, never 65534) cannot -- DAC, not
#: Landlock, is what makes it unreachable, so the guarantee holds in every
#: sandbox shape. A plain 0700 root-owned directory cannot be used here: it is
#: also unexecutable by the non-root worker itself (measured in
#: ``tmp/f1/f1-stage1.log``), which would make the whole broker route dead.
DEFAULT_HELPER_DIR = Path("/var/lib/e2b-priv")
SLOT_SPAWN_NAME = "e2b-slot-spawn"
MAINT_NAME = "e2b-maint"
HELPER_DIR_MODE = 0o710
HELPER_FILE_MODE = 0o750

TRANSPORT_ENV = "E2B_PRIV_HELPER_TRANSPORT"
#: ``agent`` is C3's shape (Task 4): the maintenance verbs are not executed
#: here at all -- every file operation travels to the control plane as
#: ``{sandbox_id, op}`` and the agent executes it (hard rules 1/3, §14.4). It
#: is a *shape*, not a transport in the argv sense: this module's argv builders
#: and the file-capability binaries are simply not used in it, and
#: :mod:`envd_service.agent_fileops` is the client instead.
TRANSPORTS = ("auto", "exec", "agent")
#: The transport value that selects the C3 shape.
AGENT_TRANSPORT = "agent"


def _image_cache_root() -> Path | None:
    """``E2B_IMAGE_CACHE_DIR`` as a whitelist root, or ``None`` when unset.

    The image cache is a root because of what lives in it: the sandbox secrets
    (``<cache>/secrets/<sandbox_id>/``) are ``0600`` files a *non-root* worker
    can only hand to a pooled uid through the broker. Read from the
    environment rather than from ``Settings`` on purpose -- the whitelist has
    to be the *broker's* list, the broker is configured by this same variable
    (it is a separate pod one node at a time), and the settings' own default
    for the cache is the legacy in-workspace ``tmp/sandboxes/_images``, which
    the first root already covers.
    """
    raw = os.environ.get("E2B_IMAGE_CACHE_DIR")
    return Path(raw) if raw else None


def _transport_setting() -> str:
    """``E2B_PRIV_HELPER_TRANSPORT`` (default ``auto``), validated by name."""
    value = str(os.environ.get(TRANSPORT_ENV, "auto") or "auto").lower()
    if value not in TRANSPORTS:
        raise PrivHelperError(
            "E2B_PRIV_HELPER_TRANSPORT must be 'auto', 'exec' or 'agent' "
            f"(got {value!r})"
        )
    return value


# capability(7) numbers, as bit positions in a capability mask.
CAP_CHOWN = 0
CAP_DAC_OVERRIDE = 1
CAP_SETGID = 6
CAP_SETUID = 7

_CAP_NAMES = {
    CAP_CHOWN: "CAP_CHOWN",
    CAP_DAC_OVERRIDE: "CAP_DAC_OVERRIDE",
    2: "CAP_DAC_READ_SEARCH",
    3: "CAP_FOWNER",
    4: "CAP_FSETID",
    5: "CAP_KILL",
    CAP_SETGID: "CAP_SETGID",
    CAP_SETUID: "CAP_SETUID",
    8: "CAP_SETPCAP",
    9: "CAP_LINUX_IMMUTABLE",
    10: "CAP_NET_BIND_SERVICE",
    11: "CAP_NET_BROADCAST",
    12: "CAP_NET_ADMIN",
    13: "CAP_NET_RAW",
    14: "CAP_IPC_LOCK",
    15: "CAP_IPC_OWNER",
    16: "CAP_SYS_MODULE",
    17: "CAP_SYS_RAWIO",
    18: "CAP_SYS_CHROOT",
    19: "CAP_SYS_PTRACE",
    20: "CAP_SYS_PACCT",
    21: "CAP_SYS_ADMIN",
    22: "CAP_SYS_BOOT",
    23: "CAP_SYS_NICE",
    24: "CAP_SYS_RESOURCE",
    25: "CAP_SYS_TIME",
    26: "CAP_SYS_TTY_CONFIG",
    27: "CAP_MKNOD",
    28: "CAP_LEASE",
    29: "CAP_AUDIT_WRITE",
    30: "CAP_AUDIT_CONTROL",
    31: "CAP_SETFCAP",
    32: "CAP_MAC_OVERRIDE",
    33: "CAP_MAC_ADMIN",
    34: "CAP_SYSLOG",
    35: "CAP_WAKE_ALARM",
    36: "CAP_BLOCK_SUSPEND",
    37: "CAP_AUDIT_READ",
    38: "CAP_PERFMON",
    39: "CAP_BPF",
    40: "CAP_CHECKPOINT_RESTORE",
}

CAP_SETUID_MASK = 1 << CAP_SETUID
CAP_SETGID_MASK = 1 << CAP_SETGID
CAP_CHOWN_MASK = 1 << CAP_CHOWN
CAP_DAC_OVERRIDE_MASK = 1 << CAP_DAC_OVERRIDE

SLOT_SPAWN_CAPS = frozenset({"CAP_SETUID", "CAP_SETGID"})
MAINT_CAPS = frozenset({"CAP_CHOWN", "CAP_DAC_OVERRIDE"})

#: ``setcap`` hint per broker, quoted in the self-check failure.
_SETCAP_HINT = {
    SLOT_SPAWN_NAME: "cap_setuid,cap_setgid+ep",
    MAINT_NAME: "cap_chown,cap_dac_override+ep",
}

#: xattr revision 2 (``VFS_CAP_REVISION_2``) with the all-or-nothing
#: effective flag (``VFS_CAP_FLAGS_EFFECTIVE``).
_VFS_CAP_REVISION_1 = 0x01000000
_VFS_CAP_REVISION_2 = 0x02000000
_VFS_CAP_REVISION_3 = 0x03000000
_VFS_CAP_FLAGS_EFFECTIVE = 0x000001
_VFS_CAP_REVISION_MASK = 0xFF000000
_VFS_CAP_FLAGS_MASK = ~_VFS_CAP_REVISION_MASK & 0xFFFFFFFF

#: Mode of a sandbox-owned directory (workspace root / volume slice).
#:
#: ``0770 owner=<sandbox uid> group=<worker gid>`` -- fix round 1 (裁定 c1).
#: The worker is the **data-plane owner** of every workspace: the files API,
#: the watcher, the command-log writer, snapshots and the whole lifecycle run
#: in the worker process and must keep working while a sandbox is paused,
#: frozen or gone. Its access requirement is therefore not a capability it
#: borrows for one syscall, it is a *permission* on the tree. Membership in the
#: group grants it; a sandbox (uid Y, gid Y, ``setgroups([])`` -- the slot
#: broker and the userns path both clear supplementary groups) is never in
#: that group and the ``other`` bits are 0, so cross-sandbox isolation is still
#: a plain kernel DAC check.
WORKSPACE_MODE = 0o770


class PrivHelperError(RuntimeError):
    """A broker request (or the broker self-check) the worker must refuse."""


@dataclass(frozen=True)
class WalkEntry:
    """One line of ``e2b-maint walk`` output."""

    kind: str
    uid: int
    gid: int
    mode: int
    size: int
    path: str

    @classmethod
    def parse(cls, line: str) -> "WalkEntry":
        kind, uid, gid, mode, size, path = line.split(" ", 5)
        return cls(
            kind=kind,
            uid=int(uid),
            gid=int(gid),
            mode=int(mode, 8),
            size=int(size),
            path=path,
        )


@dataclass(frozen=True)
class FileCapabilities:
    """The ``security.capability`` xattr, decoded.

    ``effective`` is the *effective mask* (``permitted`` when the on-exec
    effective flag is set, else 0) -- not the flag itself.
    """

    permitted: int
    inheritable: int = 0
    effective: int = 0


def decode_file_capabilities(raw: bytes) -> FileCapabilities:
    """Decode a raw ``security.capability`` value (rev 1/2/3)."""
    if len(raw) < 4:
        raise PrivHelperError(f"security.capability xattr is truncated: {raw!r}")
    (magic_etc,) = struct.unpack_from("<I", raw, 0)
    revision = magic_etc & _VFS_CAP_REVISION_MASK
    flags = magic_etc & _VFS_CAP_FLAGS_MASK
    if revision == _VFS_CAP_REVISION_1:
        words = 1
    elif revision in (_VFS_CAP_REVISION_2, _VFS_CAP_REVISION_3):
        words = 2
    else:
        raise PrivHelperError(
            f"unsupported security.capability revision 0x{revision:08x}"
        )
    expected = 4 + 8 * words + (4 if revision == _VFS_CAP_REVISION_3 else 0)
    if len(raw) != expected:
        raise PrivHelperError(
            f"security.capability xattr has {len(raw)} bytes, expected {expected}"
        )
    permitted = 0
    inheritable = 0
    for index in range(words):
        per, inh = struct.unpack_from("<II", raw, 4 + index * 8)
        permitted |= per << (32 * index)
        inheritable |= inh << (32 * index)
    effective = permitted if flags & _VFS_CAP_FLAGS_EFFECTIVE else 0
    return FileCapabilities(
        permitted=permitted, inheritable=inheritable, effective=effective
    )


def encode_file_capabilities(
    *, permitted: int, effective: int = 0, inheritable: int = 0
) -> bytes:
    """Build a rev-2 ``security.capability`` value (tests + image tooling).

    The kernel's effective set is all-or-nothing, so ``effective`` only
    decides the flag; ``effective`` bits outside ``permitted`` are ignored
    the same way the kernel ignores them.
    """
    flags = _VFS_CAP_FLAGS_EFFECTIVE if effective else 0
    out = struct.pack("<I", _VFS_CAP_REVISION_2 | flags)
    for index in range(2):
        out += struct.pack(
            "<II",
            (permitted >> (32 * index)) & 0xFFFFFFFF,
            (inheritable >> (32 * index)) & 0xFFFFFFFF,
        )
    return out


def capability_names(mask: int) -> frozenset[str]:
    """The ``CAP_*`` names present in a capability mask."""
    return frozenset(
        name for bit, name in _CAP_NAMES.items() if mask >> bit & 1
    )


def read_file_capabilities(path: Path) -> FileCapabilities:
    """Read the file-capability xattr of ``path``.

    Raises :class:`PrivHelperError` when there is none -- an unmarked broker
    is exactly the "the image build lost the xattr" defect the self-check
    exists for.
    """
    try:
        raw = os.getxattr(path, "security.capability")
    except OSError as exc:
        raise PrivHelperError(
            f"{path} has no security.capability xattr ({exc.strerror})"
        ) from exc
    return decode_file_capabilities(raw)


@dataclass
class PrivHelpers:
    """The two brokers plus everything the worker validates against."""

    slot_spawn: Path
    maint: Path
    supervise_bin: Path
    uid_pool_start: int
    uid_pool_size: int
    workspace_base: Path
    #: N27: where the platform's own records live. ``None`` means "the
    #: workspace base", exactly like ``priv_state_base()`` in the C brokers.
    state_base: Path | None = None
    shared_volume_root: Path | None = None
    #: The image cache's whitelist root (``E2B_IMAGE_CACHE_DIR``), or ``None``
    #: when the deployment named no cache. Read from the environment at
    #: construction: see :func:`_image_cache_root`.
    image_cache_dir: Path | None = field(default_factory=_image_cache_root)

    def __post_init__(self) -> None:
        self.slot_spawn = Path(self.slot_spawn)
        self.maint = Path(self.maint)
        self.supervise_bin = Path(self.supervise_bin)
        self.workspace_base = Path(self.workspace_base)
        self.state_base = (
            self.workspace_base if self.state_base is None else Path(self.state_base)
        )
        if self.shared_volume_root is not None:
            self.shared_volume_root = Path(self.shared_volume_root)
        if self.image_cache_dir is not None:
            self.image_cache_dir = Path(self.image_cache_dir)
        if not self.supervise_bin.is_absolute():
            raise PrivHelperError(
                "the route-B supervise binary must be an absolute path "
                f"(got {str(self.supervise_bin)!r})"
            )
        if self.uid_pool_size < 1:
            raise PrivHelperError(
                f"the privileged helper uid pool needs a positive size "
                f"(got {self.uid_pool_size})"
            )

    # ------------------------------------------------------------- ranges

    @property
    def uid_end(self) -> int:
        return self.uid_pool_start + self.uid_pool_size - 1

    def validate_uid(self, uid: int) -> int:
        """The broker's uid-pool gate (mirrored by ``priv_common.c``)."""
        if not isinstance(uid, int) or isinstance(uid, bool):
            raise PrivHelperError(f"uid {uid!r} is not an integer")
        if uid < self.uid_pool_start or uid > self.uid_end:
            raise PrivHelperError(
                f"uid {uid} is outside the privileged helper uid pool "
                f"{self.uid_pool_start}..{self.uid_end}"
            )
        return uid

    def validate_chown_gid(self, gid: int) -> int:
        """The group a broker ``chown`` may name.

        Either a pooled sandbox uid (the legacy/root shape: ``X:X``) or the
        broker's **own** gid. The latter is the c1 model -- the workspace is
        ``0770`` owned by the sandbox with the worker's group -- and is not a
        widening: a process may always chgrp a file it owns to its own gid.
        """
        if not isinstance(gid, int) or isinstance(gid, bool):
            raise PrivHelperError(f"gid {gid!r} is not an integer")
        if gid == os.getegid():
            return gid
        try:
            return self.validate_uid(gid)
        except PrivHelperError:
            raise PrivHelperError(
                f"gid {gid} is neither the worker's own gid ({os.getegid()}) "
                f"nor a member of the privileged helper uid pool "
                f"{self.uid_pool_start}..{self.uid_end}"
            ) from None

    # -------------------------------------------------------------- paths

    def _root_paths(self) -> tuple[Path, ...]:
        roots = [self.workspace_base]
        # A second root only when the state base *is* one: with no
        # E2B_STATE_BASE the two are the same directory, and naming one
        # directory twice would misreport the shape.
        if self.state_base != self.workspace_base:
            roots.append(self.state_base)
        if self.shared_volume_root is not None:
            roots.append(self.shared_volume_root)
        # c1: the image cache, where the sandbox secrets live. Last, because a
        # deployment that keeps the cache inside the workspace base (the
        # legacy relative default does) must not have the *same* directory
        # named twice -- ``priv_root_paths()`` on the C side de-duplicates the
        # same way, and both sides must agree on the whitelist.
        if self.image_cache_dir is not None and self.image_cache_dir not in roots:
            roots.append(self.image_cache_dir)
        return tuple(roots)

    @property
    def roots_text(self) -> str:
        return ", ".join(str(p) for p in self._root_paths())

    def resolve_path(self, path: str | Path, *, strict: bool = False) -> Path:
        """``realpath`` + containment, exactly what ``priv_common.c`` does.

        The *raw* path is what the error names (it is what the caller asked
        for); the *resolved* path is what is returned. ``strict`` additionally
        refuses the roots themselves -- delete/chown must never target a whole
        managed root.
        """
        resolved = Path(os.path.realpath(Path(path)))
        for root in self._root_paths():
            root_resolved = Path(os.path.realpath(root))
            if resolved == root_resolved and not strict:
                return resolved
            if root_resolved in resolved.parents:
                return resolved
        raise PrivHelperError(
            f"path {path} is outside the privileged helper roots "
            f"({self.roots_text})"
        )

    # ------------------------------------------------------------ argv[0]

    def validate_spawn_program(self, program: str | Path) -> str:
        """``spawn`` launches the pinned supervise binary and nothing else."""
        if str(program) != str(self.supervise_bin):
            raise PrivHelperError(
                "the spawned program must be the absolute path "
                f"{self.supervise_bin} (got {str(program)!r}): "
                "e2b-slot-spawn is not a general run-as-uid-X launcher"
            )
        return str(self.supervise_bin)

    # -------------------------------------------------------------- argv

    def subprocess_env(self) -> dict[str, str]:
        """Environment the brokers read their policy from.

        Passed explicitly (not inherited) so the broker's pool range and root
        whitelist are the worker's own values by construction.
        """
        env = {
            "E2B_UID_POOL_START": str(self.uid_pool_start),
            "E2B_UID_POOL_SIZE": str(self.uid_pool_size),
            "E2B_WORKSPACE_BASE": str(self.workspace_base),
            # Unconditional: the broker reads one variable and falls back to
            # the workspace base only for want of a value, so "no state base
            # in this deployment" is spelled as the workspace base itself.
            "E2B_STATE_BASE": str(self.state_base),
            "E2B_SUPERVISE_BIN": str(self.supervise_bin),
            "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        }
        if self.shared_volume_root is not None:
            env["E2B_SHARED_VOLUME_ROOT"] = str(self.shared_volume_root)
        # Only when this shape has one: ``e2b-maint`` falls back to the image
        # cache it was built with, and naming a *different* cache here would
        # put a directory in the binary's whitelist that the worker does not
        # consider managed.
        if self.image_cache_dir is not None:
            env["E2B_IMAGE_CACHE_DIR"] = str(self.image_cache_dir)
        return env

    def spawn_argv(
        self,
        *,
        uid: int,
        gid: int | None = None,
        supervise_args: Sequence[str] = (),
    ) -> list[str]:
        self.validate_uid(uid)
        gid = uid if gid is None else gid
        if gid != uid:
            raise PrivHelperError(
                f"e2b-slot-spawn starts one host identity: uid {uid} and "
                f"gid {gid} must match"
            )
        self.validate_spawn_program(self.supervise_bin)
        return [
            str(self.slot_spawn),
            "spawn",
            "--uid",
            str(uid),
            "--gid",
            str(gid),
            "--",
            str(self.supervise_bin),
            *[str(a) for a in supervise_args],
        ]

    def chown_argv(
        self,
        *,
        uid: int,
        path: str | Path,
        recursive: bool = False,
        gid: int | None = None,
    ) -> list[str]:
        self.validate_uid(uid)
        gid = uid if gid is None else self.validate_chown_gid(gid)
        self.resolve_path(path, strict=True)
        argv = [
            str(self.maint),
            "chown",
            "--uid",
            str(uid),
            "--gid",
            str(gid),
        ]
        if recursive:
            argv.append("--recursive")
        argv += ["--path", str(path)]
        return argv

    def chown_worker_argv(
        self,
        *,
        path: str | Path,
        recursive: bool = False,
        gid: int | None = None,
    ) -> list[str]:
        """Hand a reclaimed orphan back to the worker's own identity.

        ``--worker`` (not ``--uid <worker uid>``): the broker chowns to *its
        own* uid/gid, so the request can never name root and needs no
        knowledge of the deployment's uid. An explicit ``gid`` scopes the
        *group* to a pooled uid instead (the slot documents are
        "worker-writable, readable by that one slot"); it gets the same pool
        gate a ``--uid`` would.
        """
        self.resolve_path(path, strict=True)
        argv = [str(self.maint), "chown", "--worker"]
        if gid is not None:
            self.validate_uid(gid)
            argv += ["--gid", str(gid)]
        if recursive:
            argv.append("--recursive")
        argv += ["--path", str(path)]
        return argv

    def rm_argv(self, *, path: str | Path) -> list[str]:
        self.resolve_path(path, strict=True)
        return [str(self.maint), "rm", "--path", str(path)]

    def walk_argv(self, *, path: str | Path) -> list[str]:
        self.resolve_path(path)
        return [str(self.maint), "walk", "--path", str(path)]

    # --------------------------------------------------------- operations

    def _run(self, argv: list[str], *, what: str) -> str:
        """Run one maintenance request through the file-capability binary.

        The argv is built by the validators above and only ever has one
        destination: ``e2b-maint`` (or ``e2b-slot-spawn``) in this process's
        own namespaces. C1's second destination -- a per-node daemon behind
        ``E2B_PRIV_HELPER_SOCKET`` -- is retired (C3 Task 7); the ``agent``
        shape never reaches this method at all.
        """
        return self._run_exec(argv, what=what)

    def _run_exec(self, argv: list[str], *, what: str) -> str:
        """Exec the local file-capability binary."""
        proc = subprocess.run(
            argv,
            env=self.subprocess_env(),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise PrivHelperError(
                f"{what} refused by {Path(argv[0]).name} "
                f"(exit {proc.returncode}): {detail}"
            )
        return proc.stdout or ""

    def chown(
        self,
        *,
        uid: int,
        path: str | Path,
        recursive: bool = False,
        gid: int | None = None,
    ) -> None:
        self._run(
            self.chown_argv(uid=uid, path=path, recursive=recursive, gid=gid),
            what=f"chown {path} to uid {uid}:gid {gid if gid is not None else uid}",
        )

    def chown_worker(
        self,
        *,
        path: str | Path,
        recursive: bool = False,
        gid: int | None = None,
    ) -> None:
        self._run(
            self.chown_worker_argv(path=path, recursive=recursive, gid=gid),
            what=f"reclaim {path} to the worker identity",
        )

    def remove(self, path: str | Path) -> None:
        self._run(self.rm_argv(path=path), what=f"remove {path}")

    def walk(self, path: str | Path) -> list["WalkEntry"]:
        """Every entry under ``path`` (size/owner metadata included)."""
        out = self._run(self.walk_argv(path=path), what=f"walk {path}")
        entries: list[WalkEntry] = []
        for line in out.splitlines():
            if not line:
                continue
            entries.append(WalkEntry.parse(line))
        return entries

    def dir_size(self, path: str | Path) -> int:
        """Bytes under ``path``: regular files plus every directory's own cost.

        The directory term is N31's fix 2.  A tree costs space for its *names*
        as well as its data: a sandbox that creates 2000 empty entries spends
        real space (measured on the cluster's NAS: 512 bytes of directory
        blocks per directory) without moving the old, file-only number at all
        (that run reported the platform number unchanged at 0).  ``walk``
        reports each directory once and carries its **allocated** size
        (``st_blocks x 512``; see
        :func:`envd_service.runtime.brief_stat.directory_cost` -- on this NAS a
        directory's ``st_size`` is 8-32x that and `du` agrees with the blocks),
        so the sum here is the same quantity :func:`dir_size` computes below
        and :func:`envd_service.runtime.dir_ledger.scan_subtree` maintains
        incrementally -- byte equality between them is the contract.

        Symlinks are the one place the two sides of this module still differ,
        and it is pre-existing: the in-process walk follows one (``entry_size``
        answers like ``os.path.getsize``) while this broker walk is
        ``FTS_PHYSICAL`` and never follows, so an ``l`` entry contributes
        nothing here.  N31's fix 2 does not change that either way; a tree
        without symlinks -- every tree measured for the quota work so far --
        sees the two agree exactly.
        """
        return sum(
            entry.size for entry in self.walk(path) if entry.kind in ("f", "d")
        )

    def slot_spawner(
        self,
        *,
        uid: int,
        policy_path: Path,
        program_path: Path,
        name: str,
        token: str,
        worker_uid: int,
        control_fd: int | None = None,
        events_fd: int | None = None,
    ) -> subprocess.Popen:
        """The ``W1SlotPool`` spawner: a slot started through the broker.

        Same shape as :func:`envd_service.route_b._spawn_slot` (the root
        worker's ``setpriv`` form) -- the two differ only in *who* performs
        ``setuid``: here it is the broker, because this worker is not root.

        ``events_fd`` is N25's one-way pushed-events descriptor. It has to be
        on this signature, not only on the root form: the pool passes it
        unconditionally, and a non-root worker (the production compose shape)
        reaches route B *through this method* -- without the parameter every
        slot start raised ``TypeError: slot_spawner() got an unexpected
        keyword argument 'events_fd'`` and the sandbox create failed with
        exit 127 (measured in the unprivileged lane, 2026-09-21).
        """
        env = self.subprocess_env()
        supervise_args = ["--policy", str(policy_path), "--uid", str(uid)]
        if control_fd is not None:
            supervise_args += [
                "--control-fd",
                str(control_fd),
            ]
            fd_list = [control_fd]
            if events_fd is not None:
                # Same handoff as the root form: a second descriptor, kept
                # open across the broker's execve, so the slot sees the same
                # number the worker wrote into its own fdinfo.
                supervise_args += ["--events-fd", str(events_fd)]
                fd_list.append(events_fd)
            supervise_args += [
                "--serve",
                "--program",
                str(program_path),
            ]
            argv = self.spawn_argv(uid=uid, supervise_args=supervise_args)
            return subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                pass_fds=tuple(fd_list),
            )
        supervise_args += [
            "--serve-path",
            name,
            "--token",
            token,
            "--peer-uid",
            str(worker_uid),
            "--program",
            str(program_path),
        ]
        argv = self.spawn_argv(uid=uid, supervise_args=supervise_args)
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )


def request_identity(
    pid: int,
    sandbox_id: str,
    *,
    control_plane_url: str,
    node_id: str,
    internal_key: str,
    timeout_s: float = 5.0,
    transport=None,
) -> dict:
    """Report a slot child's ``{sandbox_id, pid}`` to the control plane.

    C3 Task 3 (ruling D9.1): this is the worker's *whole* contribution to the
    identity hand-off. The signature is the shape -- there is deliberately **no
    uid here and no uid on the wire**: the worker names the sandbox and the pid
    it sees, the control plane looks the uid up in its own records, and the
    agent writes it. A worker that could name a uid would be an identity
    authority, which is exactly what C3 removes.

    It lives beside the privileged-helper plumbing because that is where the
    worker's identity hand-off already is, but it is *not* a privileged call:
    the
    only host it ever dials is the control plane's, and the only credential it
    ever sends is the worker's own internal key. There is no worker↔agent
    channel to reach from here (hard rule 5).

    Raises :class:`PrivHelperError` -- named, fail-closed -- when the control
    plane refuses the report or cannot be reached, so a slot that can never be
    granted an identity fails the create instead of polling forever.
    """
    import httpx

    url = (
        f"{str(control_plane_url).rstrip('/')}/internal/nodes/{node_id}"
        "/slot-identity"
    )
    try:
        with httpx.Client(timeout=float(timeout_s), transport=transport) as client:
            response = client.post(
                url,
                json={"sandbox_id": sandbox_id, "pid": int(pid)},
                headers={"X-Internal-Key": internal_key},
            )
    except httpx.HTTPError as exc:
        detail = str(exc) or type(exc).__name__
        raise PrivHelperError(
            "the control plane is unreachable for the slot-identity report of "
            f"sandbox {sandbox_id}: {detail}"
        ) from exc
    if response.status_code >= 300:
        detail = _error_detail(response)
        raise PrivHelperError(
            "the control plane refused the slot-identity report for sandbox "
            f"{sandbox_id} (HTTP {response.status_code}): {detail}"
        )
    try:
        answer = response.json()
    except ValueError as exc:
        raise PrivHelperError(
            "the control plane answered the slot-identity report for sandbox "
            f"{sandbox_id} with a non-JSON body"
        ) from exc
    return answer if isinstance(answer, dict) else {"answer": answer}


def _error_detail(response) -> str:
    """The refusal's own words: ``message`` (control plane) or ``error``."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(payload, dict):
        for key in ("message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text.strip()


# --------------------------------------------------------------- self-check


def _active() -> PrivHelpers | None:
    return _ACTIVE[0]


_ACTIVE: list[PrivHelpers | None] = [None]


def configure_priv_helpers(settings) -> PrivHelpers | None:
    """Resolve + install the singleton the worker wires itself to.

    Two singletons are resolved here, and exactly one of them is the live shape
    (``E2B_PRIV_HELPER_TRANSPORT``): the file-capability binaries (``exec`` /
    ``auto``) or C3's agent client (``agent``). ``agent`` installs **no**
    ``PrivHelpers`` -- the argv builders and their binaries are not part of that
    shape at all -- and :func:`file_steps_available` is what the call sites ask
    instead of "are there brokers".
    """
    from envd_service import agent_fileops

    if _transport_setting() == AGENT_TRANSPORT:
        agent_fileops.configure(settings)
        _ACTIVE[0] = None
        return None
    helpers = resolve_priv_helpers(settings)
    _ACTIVE[0] = helpers
    return helpers


def file_steps_available(settings=None) -> bool:
    """Whether *some* shape can perform this worker's privileged file steps.

    Either the file-capability binaries resolved (``auto`` / ``exec``)
    or C3's agent client did (``agent``). The call sites gate on this rather
    than on :func:`active_helpers` alone: a worker in the agent shape has no
    brokers and still has every step it needs -- routed elsewhere.
    """
    if _active() is not None:
        return True
    from envd_service import agent_fileops

    return agent_fileops.active() is not None


def active_helpers() -> PrivHelpers | None:
    """The brokers this worker resolved at startup (``None`` = in-process)."""
    return _active()


def _require_active(what: str) -> PrivHelpers:
    helpers = _active()
    if helpers is None:
        raise PrivHelperError(
            f"{what} needs the file-capability brokers, but this worker "
            "resolved none (E2B_PRIV_HELPERS=off, a root worker, or no "
            "brokers installed)"
        )
    return helpers


def broker_chown(
    uid: int,
    path: str | Path,
    *,
    recursive: bool = True,
    gid: int | None = None,
) -> None:
    """Chown a managed tree to a pooled uid (raises when no brokers)."""
    _require_active(f"chown {path} to uid {uid}").chown(
        uid=uid, path=path, recursive=recursive, gid=gid
    )


def broker_reclaim(
    path: str | Path, *, recursive: bool = True, gid: int | None = None
) -> None:
    """Hand a reclaimed orphan back to the worker's own identity."""
    _require_active(f"reclaim {path}").chown_worker(
        path=path, recursive=recursive, gid=gid
    )


def broker_remove(path: str | Path) -> None:
    """Delete a tree the worker's own DAC access cannot reach.

    Fix round 1 (c1): the worker is a member of every *tenant* tree's group,
    so ordinary teardown is an in-process ``rmtree`` again (see
    :func:`remove_tree`). The broker stays as the fallback for the cases group
    access does not cover: a sandbox-managed ``0700`` subdirectory, a
    ``1777`` volume root, or a leftover tree still owned by root from before
    the cut-over.
    """
    _require_active(f"remove {path}").remove(path)


def broker_dir_size(path: str | Path) -> int:
    """Size scan for trees the worker cannot walk itself (same c1 fallback)."""
    return _require_active(f"walk {path}").dir_size(path)


def helpers_cover(path: str | Path) -> bool:
    """Whether the active brokers' whitelist contains ``path``."""
    helpers = _active()
    if helpers is None:
        return False
    try:
        helpers.resolve_path(path)
    except PrivHelperError:
        return False
    return True


def remove_tree(path: str | Path, *, on_error: str = "ignore") -> None:
    """Delete a managed tree, in-process first and through the broker on EACCES.

    ``0770 owner=<sandbox uid> group=<worker gid>`` gives the worker group
    write on the tree, so teardown no longer needs the broker. A *nested*
    directory the sandbox itself made ``0700`` (or a pre-cut-over root-owned
    leftover) is still unreachable for the worker's own group access, and that
    is what ``e2b-maint rm`` remains for.
    """
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=False)
        return
    except FileNotFoundError:
        return
    except OSError as exc:
        if not helpers_cover(path):
            if on_error != "ignore":
                raise
            logger.debug("cannot remove %s in-process: %s", path, exc)
            return
        logger.info(
            "removing %s through e2b-maint (worker DAC could not: %s)", path, exc
        )
        broker_remove(path)


def dir_size(path: str | Path) -> int | None:
    """Bytes under ``path`` for ``/metrics``; ``None`` means "unknown".

    In-process first (the worker's group access reaches a ``0770`` tenant
    workspace), broker on EACCES (a ``0700`` subdirectory the sandbox made, or
    a ``1777`` volume root full of foreign-owned files).

    The quantity is *files plus directories*: each directory the walk visits
    contributes its allocated size (``st_blocks x 512``), which the file-only
    number never saw (N31: 2000 empty entries moved the platform number by 0
    bytes; measured 2026-09-21, that space is 512 bytes per directory on this
    NAS even at 2000 entries, while the directory's ``st_size`` -- 4096 to
    16384 -- is not what `du` reports). `DirLedger` maintains the same number
    incrementally and the byte equality between the two is a pinned contract
    (`tests/unit/test_dir_ledger.py`).

    The size comes from :func:`envd_service.runtime.brief_stat.entry_size`
    rather than ``os.path.getsize``: on NFS the latter flushes a file's dirty
    pages before answering (measured at 1405 ms for a file being written, where
    the size-only ``statx`` took 0.01 ms and returned the same number). This
    number is asked for every sandbox on a cadence, so it is the one place
    where paying the flush would be a permanent tax.
    """
    total = 0

    from envd_service.runtime.brief_stat import directory_cost, entry_size

    def _raise(exc: OSError) -> None:
        raise exc

    try:
        for root, _dirs, files in os.walk(path, onerror=_raise):
            try:
                total += directory_cost(root)
            except OSError:
                pass
            for name in files:
                try:
                    total += entry_size(os.path.join(root, name))
                except OSError:
                    continue
        return total
    except OSError:
        pass
    if not helpers_cover(path):
        return None
    return broker_dir_size(path)


def helpers_unavailable_reason(settings) -> str | None:
    """Why a non-root worker is *not* using the broker shape, or ``None``.

    Only the "no brokers at all" case: a *partial* install is a deployment
    defect and fails closed in :func:`resolve_priv_helpers` instead.

    C3's agent shape answers ``None``: a worker with no brokers is *not*
    degraded there -- its privileged file steps are served by the agent -- so
    the "keeping the in-process (E5.1) shape" warning would be wrong.
    """
    from envd_service import agent_fileops

    if agent_fileops.enabled(settings):
        return None
    mode = str(getattr(settings, "priv_helpers", "auto") or "auto").lower()
    if mode == "off" or os.geteuid() == 0:
        return None
    slot = DEFAULT_HELPER_DIR / SLOT_SPAWN_NAME
    maint = DEFAULT_HELPER_DIR / MAINT_NAME
    if slot.exists() or maint.exists():
        return None
    return (
        f"E2B_PRIV_HELPERS=auto on a non-root worker, but {slot} is missing: "
        "this worker keeps the in-process (E5.1) shape; ship the "
        "file-capability brokers to get per-sandbox host uids and route-B slots"
    )


def resolve_priv_helpers(settings) -> PrivHelpers | None:
    """The startup self-check (fail closed on a half-installed broker pair).

    ``None`` means "use the in-process shape": the mode is ``off``, the worker
    is root, or no brokers are installed at all (named by
    :func:`helpers_unavailable_reason`).

    There is exactly one installed shape left (C3 Task 7 retired C1's per-node
    ``socket`` daemon): this process execs the file-capability binaries, or --
    when ``E2B_PRIV_HELPER_TRANSPORT=agent`` -- :func:`configure_priv_helpers`
    never gets here because the file steps travel to the agent instead.
    """
    mode = str(getattr(settings, "priv_helpers", "auto") or "auto").lower()
    if mode not in ("auto", "off"):
        raise PrivHelperError(
            f"E2B_PRIV_HELPERS must be 'auto' or 'off' (got {mode!r})"
        )
    if mode == "off" or os.geteuid() == 0:
        return None
    # ``_transport_setting()`` is still consulted: it validates the value (an
    # unknown transport is a named refusal, not a shape guessed at), and
    # ``agent`` is the one value that must never resolve a local privileged
    # shape -- the caller handles that shape before reaching here, so seeing it
    # this far down means the two halves of the switch disagree.
    if _transport_setting() == AGENT_TRANSPORT:
        raise PrivHelperError(
            "E2B_PRIV_HELPER_TRANSPORT=agent must resolve through "
            "configure_priv_helpers(): refusing to build a local privileged "
            "shape for the agent transport"
        )
    slot = DEFAULT_HELPER_DIR / SLOT_SPAWN_NAME
    maint = DEFAULT_HELPER_DIR / MAINT_NAME
    # The exec branch's own entry condition: with no binary at all there is
    # nothing this worker could exec, so it keeps today's in-process (E5.1)
    # shape. Everything past this point -- including the "one binary missing"
    # refusal -- lives in :func:`_build_helpers`.
    if not slot.exists() and not maint.exists():
        return None
    return _build_helpers(settings)


def _build_helpers(settings) -> PrivHelpers:
    """Build (and self-check) the file-capability shape.

    The partial-install rule, the reachability/file-capability checks, the
    pool-plus-route-B shape, the worker-identity guard, every field of the
    shape and the route-B scratch root check are all asked here, once: this is
    the only installed shape left (C3 Task 7), so there is nothing left to
    keep two callers consistent with.
    """
    slot = DEFAULT_HELPER_DIR / SLOT_SPAWN_NAME
    maint = DEFAULT_HELPER_DIR / MAINT_NAME
    slot_present = slot.exists()
    maint_present = maint.exists()
    if slot_present != maint_present:
        present = slot if slot_present else maint
        missing = maint if slot_present else slot
        raise PrivHelperError(
            f"{missing} is missing while {present} is present: a partial "
            "broker install must not be guessed at"
        )
    if slot_present:
        # Present means the image shipped them, so they have to pass: mode
        # 0710 root-owned (executable by the worker's group, not by a sandbox
        # uid) with the file capability applied in the final image stage.
        _require_unreachable(slot, maint)
        _require_caps(slot, SLOT_SPAWN_CAPS)
        _require_caps(maint, MAINT_CAPS)
    _require_consistent_shape(settings)
    check_worker_identity_outside_pool(
        uid=os.geteuid(),
        gid=os.getegid(),
        start=int(getattr(settings, "uid_pool_start", 10000)),
        size=int(getattr(settings, "uid_pool_size", 1000)),
    )
    helpers = PrivHelpers(
        slot_spawn=slot,
        maint=maint,
        supervise_bin=_supervise_bin(),
        uid_pool_start=int(getattr(settings, "uid_pool_start", 10000)),
        uid_pool_size=int(getattr(settings, "uid_pool_size", 1000)),
        workspace_base=Path(getattr(settings, "workspace_base")),
        state_base=Path(
            getattr(settings, "state_base", None)
            or getattr(settings, "workspace_base")
        ),
        shared_volume_root=(
            Path(settings.shared_volume_root)
            if getattr(settings, "shared_volume_root", None)
            else None
        ),
    )
    _require_route_b_scratch_root(helpers, settings)
    return helpers


def _require_consistent_shape(settings) -> None:
    """The broker shape only works as per-sandbox uids **plus** route B.

    Two mechanical reasons, both measured:

    * without a pooled uid the sandbox would run as the worker's own identity
      -- the same gid that may exec ``e2b-slot-spawn`` (whoever can exec it
      holds ``cap_setuid``), so the broker would hand a sandbox the ability to
      become any other tenant;
    * without route B the sandbox would have to be remapped in-process, which
      is precisely what a non-root worker cannot do (the single-entry userns
      maps only its own euid, S1.2) -- the create would fail at
      ``sandlock_create`` instead of at startup with a reason.
    """
    if not bool(getattr(settings, "per_sandbox_uid", True)):
        raise PrivHelperError(
            "E2B_PRIV_HELPERS needs E2B_PER_SANDBOX_UID: without a pooled "
            "sandbox uid the sandbox would run as the worker's own identity, "
            "which is exactly the gid that can exec the brokers (whoever can "
            "exec e2b-slot-spawn holds cap_setuid)"
        )
    if str(getattr(settings, "route_b", "auto")).lower() == "off":
        raise PrivHelperError(
            "E2B_PRIV_HELPERS needs E2B_ROUTE_B != off: a non-root worker "
            "cannot remap a sandbox in-process (S1.2), so the broker-started "
            "slot is the only way to run as the pooled host uid"
        )


def check_worker_identity_outside_pool(
    *, uid: int, gid: int, start: int, size: int
) -> None:
    """The pool must never hand a sandbox the worker's own uid or gid (c1).

    Fix round 1 makes the worker a *member* of every sandbox tree's group
    (``0770 owner=<sandbox uid> group=<worker gid>``). If a sandbox were
    allocated that same uid or gid it would be inside the worker's trust
    boundary -- it could read and write other sandboxes' workspaces -- so the
    configuration is refused by name at startup rather than shipping a silent
    hole. Checked for every worker shape (a root worker included): the group
    model -- and therefore the guard -- is what makes the shared tree safe.
    """
    end = start + size - 1
    for kind, value in (("uid", uid), ("gid", gid)):
        if start <= value <= end:
            raise PrivHelperError(
                f"the sandbox {kind} pool {start}..{end} contains the worker's "
                f"own {kind} ({value}): a sandbox would share the worker's "
                "identity and could read every other sandbox's 0770 workspace "
                "(the group model relies on the sandbox gid differing from the "
                f"worker's); move E2B_UID_POOL_START/SIZE off {value}"
            )


def _supervise_bin() -> Path:
    from envd_service.route_b import default_supervise_bin

    return default_supervise_bin()


def _require_unreachable(slot: Path, maint: Path) -> None:
    """Root-owned + worker-group: the worker can exec, no sandbox uid can.

    ``mode 0710 / owner root / group <worker gid>``: a sandbox uid is a pool
    uid (10000+), never a member of the worker's gid, so it cannot traverse
    the directory or exec the binaries -- in every sandbox shape, unlike a
    Landlock-prefix argument (a pure-shape sandbox reads most of the
    filesystem outside the denied carve-outs).

    The check is on the *bits* (owner root, no access for "other") plus an
    access probe, not on "group == this process's gid": the worker may hold
    the gid as a supplementary group, and a unit test that simulates a
    non-root worker must not have to fake its gid too.
    """
    for path in (slot.parent, slot, maint):
        expected_mode = HELPER_DIR_MODE if path.is_dir() else HELPER_FILE_MODE
        try:
            st = path.stat()
        except OSError as exc:
            raise PrivHelperError(f"cannot stat the broker path {path}: {exc}") from exc
        mode = stat.S_IMODE(st.st_mode)
        if st.st_uid != 0 or mode != expected_mode:
            raise PrivHelperError(
                f"{path} must be root-owned mode {expected_mode:04o} so the "
                "worker can exec the brokers while no sandbox uid can: got "
                f"owner {st.st_uid}, group {st.st_gid}, mode {mode:04o}"
            )
    if not os.access(slot.parent, os.X_OK) or not os.access(slot, os.X_OK):
        raise PrivHelperError(
            f"{slot.parent} / {slot.name} are not executable by this worker: "
            "the brokers must be reachable by the worker's own group at mode "
            f"{HELPER_DIR_MODE:04o}/{HELPER_FILE_MODE:04o} (a plain 0700 "
            "root-owned directory is unexecutable for uid 65534)"
        )


def _require_caps(path: Path, required: Iterable[str]) -> None:
    caps = read_file_capabilities(path)
    found = capability_names(caps.permitted)
    missing = sorted(set(required) - found)
    if missing:
        raise PrivHelperError(
            f"{path} is missing the file capabilities {missing} "
            f"(found {sorted(found)}): run "
            f"`setcap {_SETCAP_HINT.get(path.name, '')}` in the final image "
            "stage (`COPY --from` does not preserve the xattr)"
        )


def _require_route_b_scratch_root(helpers: PrivHelpers, settings) -> None:
    """The slot's policy/program documents must be scoped through the broker.

    ``W1SlotPool`` writes the slot's ``policy.json``/``program.json`` under
    ``E2B_ROUTE_B_TMP_ROOT`` and scopes them to the slot's gid (``0440
    root:<uid>``). A non-root worker can only do that through ``e2b-maint``,
    and the broker touches whitelisted roots only -- elsewhere the policy
    (which carries egress-proxy credentials) would fall back to world-readable
    ``0444``. Refuse the shape by name instead of shipping that leak. Since
    N27 there are two legitimate bases to point it at: the workspace base and
    the state base (``E2B_STATE_BASE``), the latter being where ``.route-b``
    moves with the rest of the platform's own files.
    """
    if str(getattr(settings, "route_b", "auto")).lower() == "off":
        return
    tmp_root = Path(getattr(settings, "route_b_tmp_root", "/tmp/sandlock-route-b"))
    try:
        helpers.resolve_path(tmp_root)
    except PrivHelperError:
        raise PrivHelperError(
            f"route-B scratch root {tmp_root} is outside the privileged "
            f"helper roots ({helpers.roots_text}): the slot documents are "
            "group-scoped to the slot uid through e2b-maint, so point "
            "E2B_ROUTE_B_TMP_ROOT at the workspace base or the state base"
        ) from None
