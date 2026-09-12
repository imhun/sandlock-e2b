"""Device-free XFS project-quota backend using fd-based kernel interfaces.

Why this exists (B1): the quota-agent runs as a container that only sees the
shared filesystem as a *bind mount*. ``xfs_quota`` (and ``xfs_info``/
``xfs_growfs``) resolve the mount to its backing device and open
``/dev/nvme0n1p2`` -- which such a container does not have, so every
subcommand fails with ``cannot setup path for mount ...: No such device or
address`` even with CAP_SYS_ADMIN (measured 2026-09-12). The kernel offers the
same operations without touching the device:

* ``quotactl_fd(2)`` (syscall 443, kernel >= 5.14) on an fd *inside* the mount:
  ``Q_XGETQSTATV`` / ``Q_XSETQLIM`` / ``Q_XGETQUOTA`` / ``Q_XGETNEXTQUOTA``;
* ``FS_IOC_FSGETXATTR`` / ``FS_IOC_FSSETXATTR`` on a path for the project id
  itself (plus ``XFS_XFLAG_PROJINHERIT`` so children inherit it).

Units: ``fs_disk_quota`` counts in **512-byte basic blocks**, while the
``xfs_quota report -p`` view this codebase already speaks is in **1 KiB
blocks**. ``mb_to_basic_blocks`` is the single conversion point; getting it
wrong is a silent 2x error (measured: 4 MiB written as 4,194,304 blocks =
2 GiB, so the limit never bit).

Everything here is fail-closed: an unsupported kernel interface, a struct that
does not read back, or an unexpected errno raises ``QuotactlError``/returns an
explicit "unknown" -- callers degrade with a named warning instead of
pretending quota works.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import shutil
import struct
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: quotactl_fd(2) -- generic syscall table number (x86_64 and arm64 alike).
_SYS_QUOTACTL_FD = 443
_PRJQUOTA = 2  # d_flags / quota type for project quota

_Q_XGETQUOTA = 0x5803
_Q_XSETQLIM = 0x5804
_Q_XGETQSTATV = 0x5808
_Q_XGETNEXTQUOTA = 0x5809

_FS_DQ_ISOFT = 1 << 0
_FS_DQ_IHARD = 1 << 1
_FS_DQ_BSOFT = 1 << 2
_FS_DQ_BHARD = 1 << 3

_XFS_QUOTA_PDQ_ACCT = 0x0010
_XFS_QUOTA_PDQ_ENFD = 0x0020

_FSXATTR_SIZE = 28
_FS_DISK_QUOTA_SIZE = 112
_FS_QUOTA_STATV_SIZE = 104
_FS_QSTATV_VERSION1 = 1
_FS_DQUOT_VERSION = 1

_XFS_XFLAG_PROJINHERIT = 0x00000200
_XFS_XFLAG_HASATTR = 0x80000000  # read-only flag; never write it back

_FS_IOC_FSGETXATTR = (2 << 30) | (_FSXATTR_SIZE << 16) | (0x58 << 8) | 31
_FS_IOC_FSSETXATTR = (1 << 30) | (_FSXATTR_SIZE << 16) | (0x58 << 8) | 32

_BASIC_BLOCK_BYTES = 512
_KIB = 1024

#: projid used by the projid32bit probe: > 0xFFFF, so a filesystem without
#: 32-bit project ids cannot store it verbatim.
_PROBID32BIT_PROBE_ID = 0x12345


class QuotactlError(RuntimeError):
    """A device-free quota operation failed; callers degrade with a warning."""


class QuotactlUnavailable(QuotactlError):
    """This kernel/container cannot do fd-based quota administration."""


def mb_to_basic_blocks(mb: int) -> int:
    """MiB -> 512-byte basic blocks (the single unit conversion point)."""
    if mb < 0:
        raise ValueError("mb must be non-negative")
    return mb * 1024 * 1024 // _BASIC_BLOCK_BYTES


def basic_blocks_to_kib(blocks: int) -> int:
    """512-byte basic blocks -> 1 KiB blocks (the unit of the report tables)."""
    return blocks * _BASIC_BLOCK_BYTES // _KIB


def _libc() -> Any:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


class _MountFds:
    """One cached directory fd per mount point (device-free quota handle)."""

    def __init__(self) -> None:
        self._fds: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, mount_point: str | Path) -> int:
        key = str(Path(mount_point))
        with self._lock:
            fd = self._fds.get(key)
            if fd is not None:
                try:
                    os.fstat(fd)
                    return fd
                except OSError:
                    self._fds.pop(key, None)
            try:
                fd = os.open(key, os.O_RDONLY | os.O_DIRECTORY)
            except OSError as exc:
                raise QuotactlError(f"cannot open mount {key}: {exc}") from exc
            self._fds[key] = fd
            return fd


_MOUNT_FDS = _MountFds()


def _qcmd(cmd: int) -> int:
    return (cmd << 8) | _PRJQUOTA


def _quotactl_fd(fd: int, cmd: int, qid: int, buf: Any) -> tuple[int, int]:
    libc = _libc()
    ctypes.set_errno(0)
    rc = libc.syscall(
        ctypes.c_long(_SYS_QUOTACTL_FD),
        ctypes.c_int(fd),
        ctypes.c_int(_qcmd(cmd)),
        ctypes.c_int(qid),
        ctypes.byref(buf),
    )
    return rc, ctypes.get_errno()


def _errno_name(err: int) -> str:
    return errno.errorcode.get(err, str(err))


def state(mount_point: str | Path) -> dict[str, bool]:
    """Project-quota accounting/enforcement state for ``mount_point``."""
    fd = _MOUNT_FDS.get(mount_point)
    buf = ctypes.create_string_buffer(_FS_QUOTA_STATV_SIZE)
    struct.pack_into("<b", buf, 0, _FS_QSTATV_VERSION1)
    rc, err = _quotactl_fd(fd, _Q_XGETQSTATV, 0, buf)
    if rc != 0:
        raise QuotactlError(
            f"Q_XGETQSTATV failed on {mount_point}: {_errno_name(err)}"
        )
    version = struct.unpack_from("<b", buf.raw, 0)[0]
    if version != _FS_QSTATV_VERSION1:
        raise QuotactlUnavailable(
            f"fs_quota_statv version {version} != {_FS_QSTATV_VERSION1}"
        )
    flags = struct.unpack_from("<H", buf.raw, 2)[0]
    return {
        "accounting": bool(flags & _XFS_QUOTA_PDQ_ACCT),
        "enforcement": bool(flags & _XFS_QUOTA_PDQ_ENFD),
    }


def available(mount_point: str | Path) -> bool:
    """Whether fd-based project-quota administration works here."""
    try:
        state(mount_point)
    except QuotactlError:
        return False
    return True


def set_limit(mount_point: str | Path, projid: int, disk_mb: int) -> None:
    """Set the project's block hard/soft limit (MiB) via Q_XSETQLIM."""
    fd = _MOUNT_FDS.get(mount_point)
    blocks = mb_to_basic_blocks(disk_mb)
    buf = ctypes.create_string_buffer(_FS_DISK_QUOTA_SIZE)
    struct.pack_into("<b", buf, 0, _FS_DQUOT_VERSION)
    struct.pack_into("<h", buf, 2, _FS_DQ_BHARD | _FS_DQ_BSOFT)
    struct.pack_into("<I", buf, 4, projid)
    struct.pack_into("<Q", buf, 8, blocks)   # d_blk_hardlimit
    struct.pack_into("<Q", buf, 16, blocks)  # d_blk_softlimit
    rc, err = _quotactl_fd(fd, _Q_XSETQLIM, projid, buf)
    if rc != 0:
        raise QuotactlError(
            f"Q_XSETQLIM({projid}, {disk_mb}MB={blocks} blocks) failed: "
            f"{_errno_name(err)}"
        )


def clear_limit(mount_point: str | Path, projid: int) -> None:
    """Zero a project's block limits (drops the dquot once usage is 0)."""
    fd = _MOUNT_FDS.get(mount_point)
    buf = ctypes.create_string_buffer(_FS_DISK_QUOTA_SIZE)
    struct.pack_into("<b", buf, 0, _FS_DQUOT_VERSION)
    struct.pack_into("<h", buf, 2, _FS_DQ_BHARD | _FS_DQ_BSOFT | _FS_DQ_IHARD | _FS_DQ_ISOFT)
    struct.pack_into("<I", buf, 4, projid)
    rc, err = _quotactl_fd(fd, _Q_XSETQLIM, projid, buf)
    if rc != 0:
        raise QuotactlError(
            f"Q_XSETQLIM(0) for {projid} failed: {_errno_name(err)}"
        )


def usage(mount_point: str | Path, projid: int) -> tuple[int, int, int] | None:
    """(used, soft, hard) in 1 KiB blocks, or None when no dquot exists."""
    fd = _MOUNT_FDS.get(mount_point)
    buf = ctypes.create_string_buffer(_FS_DISK_QUOTA_SIZE)
    struct.pack_into("<b", buf, 0, _FS_DQUOT_VERSION)
    rc, err = _quotactl_fd(fd, _Q_XGETQUOTA, projid, buf)
    if rc != 0:
        if err == errno.ENOENT:
            return None
        raise QuotactlError(
            f"Q_XGETQUOTA({projid}) failed: {_errno_name(err)}"
        )
    version = struct.unpack_from("<b", buf.raw, 0)[0]
    if version != _FS_DQUOT_VERSION:
        raise QuotactlUnavailable(
            f"fs_disk_quota version {version} != {_FS_DQUOT_VERSION}"
        )
    hard = struct.unpack_from("<Q", buf.raw, 8)[0]
    soft = struct.unpack_from("<Q", buf.raw, 16)[0]
    used = struct.unpack_from("<Q", buf.raw, 40)[0]
    return (
        basic_blocks_to_kib(used),
        basic_blocks_to_kib(soft),
        basic_blocks_to_kib(hard),
    )


def project_table(mount_point: str | Path) -> dict[int, tuple[int, int, int]]:
    """projid -> (used, soft, hard) in 1 KiB blocks.

    ``Q_XGETNEXTQUOTA`` reports dquots that carry **no limits** too: both the
    all-zero ghosts a release leaves behind and entries whose usage counter is
    still draining. The operator view (``xfs_quota report -p``) hides exactly
    those, so they are filtered here as well -- a phantom row would otherwise
    show up as a live project in the reconciliation/limit checks (measured on
    the target: an entry with hard=soft=0 and a stale ``used`` outlived the
    deleted slice while the host view showed only project 0).
    """
    fd = _MOUNT_FDS.get(mount_point)
    table: dict[int, tuple[int, int, int]] = {}
    qid = 1
    while True:
        buf = ctypes.create_string_buffer(_FS_DISK_QUOTA_SIZE)
        struct.pack_into("<b", buf, 0, _FS_DQUOT_VERSION)
        rc, err = _quotactl_fd(fd, _Q_XGETNEXTQUOTA, qid, buf)
        if rc != 0:
            if err == errno.ENOENT:
                break
            raise QuotactlError(
                f"Q_XGETNEXTQUOTA(>{qid}) failed: {_errno_name(err)}"
            )
        version = struct.unpack_from("<b", buf.raw, 0)[0]
        if version != _FS_DQUOT_VERSION:
            raise QuotactlUnavailable(
                f"fs_disk_quota version {version} != {_FS_DQUOT_VERSION}"
            )
        projid = struct.unpack_from("<I", buf.raw, 4)[0]
        hard = struct.unpack_from("<Q", buf.raw, 8)[0]
        soft = struct.unpack_from("<Q", buf.raw, 16)[0]
        used = struct.unpack_from("<Q", buf.raw, 40)[0]
        if projid and (hard or soft):
            table[projid] = (
                basic_blocks_to_kib(used),
                basic_blocks_to_kib(soft),
                basic_blocks_to_kib(hard),
            )
        qid = projid + 1 if projid else qid + 1
    return table


def _fsxattr(fd: int) -> list[int]:
    buf = ctypes.create_string_buffer(_FSXATTR_SIZE)
    libc = _libc()
    ctypes.set_errno(0)
    rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(_FS_IOC_FSGETXATTR), buf)
    err = ctypes.get_errno()
    if rc != 0:
        raise QuotactlError(f"FS_IOC_FSGETXATTR failed: {_errno_name(err)}")
    return list(struct.unpack_from("<7I", buf.raw))


def _set_fsxattr(fd: int, words: list[int]) -> None:
    buf = ctypes.create_string_buffer(_FSXATTR_SIZE)
    ctypes.memmove(buf, struct.pack("<7I", *words), _FSXATTR_SIZE)
    libc = _libc()
    ctypes.set_errno(0)
    rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(_FS_IOC_FSSETXATTR), buf)
    err = ctypes.get_errno()
    if rc != 0:
        raise QuotactlError(f"FS_IOC_FSSETXATTR failed: {_errno_name(err)}")


def projid_of(path: str | Path) -> int:
    """The project id currently stored on ``path`` (0 = none)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise QuotactlError(f"cannot open {path}: {exc}") from exc
    try:
        return _fsxattr(fd)[3]
    finally:
        os.close(fd)


def assign_projid(path: str | Path, projid: int) -> int:
    """Set ``projid`` on the directory and make children inherit it.

    ``XFS_XFLAG_PROJINHERIT`` is what makes files created *after* this call
    carry the project id; without it the directory alone is tagged (measured
    2026-09-12). The result is read back and a mismatch is an error -- never a
    silent "assigned".
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise QuotactlError(f"cannot open {path}: {exc}") from exc
    try:
        words = _fsxattr(fd)
        words[0] = (words[0] & ~_XFS_XFLAG_HASATTR) | _XFS_XFLAG_PROJINHERIT
        words[3] = projid
        _set_fsxattr(fd, words)
        stored = _fsxattr(fd)[3]
        if stored != projid:
            raise QuotactlError(
                f"project id did not stick on {path}: wrote {projid}, read {stored}"
            )
        return stored
    finally:
        os.close(fd)


def clear_projid(path: str | Path) -> None:
    """Remove the project id (and PROJINHERIT) from a directory."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise QuotactlError(f"cannot open {path}: {exc}") from exc
    try:
        words = _fsxattr(fd)
        words[0] = words[0] & ~_XFS_XFLAG_HASATTR & ~_XFS_XFLAG_PROJINHERIT
        words[3] = 0
        _set_fsxattr(fd, words)
    finally:
        os.close(fd)


# --- projid32bit ----------------------------------------------------------

_FACTS_LOCK = threading.Lock()
_FACTS_CACHE: dict[str, tuple[bool | None, str]] = {}


def _mount_key(mount_point: str | Path) -> str:
    try:
        st = os.stat(str(mount_point))
    except OSError:
        return str(mount_point)
    return f"{mount_point}|{st.st_dev}"


def projid32bit(mount_point: str | Path) -> tuple[bool | None, str]:
    """(supported, reason). ``None`` means "unknown -- degrade by name".

    ``XFS_IOC_FSGEOMETRY`` is the clean source but is ENOTTY in a container
    that only bind-mounts the filesystem (measured, all struct sizes), so the
    answer is obtained *functionally*: write a project id above 0xFFFF and
    read it back. That is the property the agent actually depends on (its
    hashed project ids exceed 16 bits).

    The probe mutates a scratch directory, so it is serialized by a module
    lock, cached per mount, and always reverted in a ``finally`` block.
    """
    key = _mount_key(mount_point)
    with _FACTS_LOCK:
        cached = _FACTS_CACHE.get(key)
        if cached is not None:
            return cached
        result = _probe_projid32bit(mount_point)
        if result[0] is not None:
            _FACTS_CACHE[key] = result
        return result


def _probe_projid32bit(mount_point: str | Path) -> tuple[bool | None, str]:
    mount = Path(mount_point)
    if not os.access(mount, os.W_OK):
        return None, f"mount {mount} is not writable: cannot probe projid32bit"
    scratch = mount / f"_quota_probe_{os.getpid()}"
    try:
        shutil.rmtree(scratch, ignore_errors=True)
        os.makedirs(scratch, exist_ok=True)
    except OSError as exc:
        return None, f"cannot create the projid32bit probe dir under {mount}: {exc}"
    try:
        assign_projid(scratch, _PROBID32BIT_PROBE_ID)
        return True, "probe: project id 0x12345 stored verbatim"
    except QuotactlError as exc:
        message = str(exc)
        if any(code in message for code in ("EINVAL", "ERANGE", "EOVERFLOW")):
            return False, f"probe rejected a 32-bit project id: {message}"
        return None, f"projid32bit probe failed: {message}"
    finally:
        try:
            if scratch.exists():
                clear_projid(scratch)
        except QuotactlError as exc:  # pragma: no cover - best effort
            logger.warning("could not clear the projid32bit probe dir: %s", exc)
        shutil.rmtree(scratch, ignore_errors=True)
