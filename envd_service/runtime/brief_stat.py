"""File sizes without waking the NFS writeback path (N25).

`os.stat()` is not a read on NFS. `nfs_getattr()` flushes the file's own dirty
pages out to the server before it answers, whenever the request asks for ctime
or mtime -- and the `fstatat` behind `os.stat()` always does:

    /* Flush out writes to the server in order to update c/mtime/version.  */
    if ((request_mask & (STATX_CTIME | STATX_MTIME | STATX_CHANGE_COOKIE)) &&
        S_ISREG(inode->i_mode)) {
            if (nfs_have_delegated_mtime(inode))
                    filemap_fdatawrite(inode->i_mapping);
            else
                    filemap_write_and_wait(inode->i_mapping);
    }

Measured on the cluster (2026-09-19), the same file at the same instant, on the
worker that owned the writer, while its sandbox rewrote an 800 MiB file in a
loop:

    os.stat()                       1405 ms   (p50 of 13 rounds)
    statx(AT_STATX_DONT_SYNC)          0.06 ms  -- the same size returned
    statx(mask=STATX_SIZE)             0.01 ms  -- the same size returned

and on the *other* worker, which holds no dirty pages for that file, all three
were 0.01-0.03 ms. So the cost is the flush, not the round trip and not the
NAS: the number a quota needs is the one the client already has.

Asking for `STATX_SIZE` alone avoids it, because the kernel schedules the flush
only for requests that include the time fields -- and it still revalidates the
size when the client's size cache is stale, so the answer is not "whatever was
cached at mount time". That is exactly the question an accounting walk asks, so
it is the one asked here. `os.stat` stays as the fallback for a platform with
no `statx` (or a libc that does not export it), because a slow number that is
right beats a fast one that is not.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import threading
from typing import Any

#: `statx(2)`: the directory-entry-relative form `os.stat` uses, a request mask
#: of `STATX_SIZE` only, and the two offsets this module needs out of the
#: 256-byte, architecture-independent `struct statx`.
_AT_FDCWD = -100
_STATX_SIZE = 0x0200
_STATX_BLOCKS = 0x0400
_STATX_STRUCT_SIZE = 256
_STATX_SIZE_OFFSET = 40
#: `stx_blocks` follows `stx_size` in `struct statx`, still in 512-byte units.
_STATX_BLOCKS_OFFSET = 48

#: `statx` that cannot work at all (no syscall, no libc export) must not cost a
#: failed call per file, so it is retired on the first such answer.
_UNAVAILABLE = (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP)

_local = threading.local()
_module_lock = threading.Lock()
_statx: Any = None
_statx_broken = False


def _load_statx() -> Any:
    """The libc `statx` wrapper, or ``None`` when this platform has none."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        statx = libc.statx
    except (OSError, AttributeError):
        return None
    statx.restype = ctypes.c_int
    statx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    return statx


def statx_available() -> bool:
    """Whether this process will use `statx` (for a caller that wants to say so)."""
    global _statx, _statx_broken
    with _module_lock:
        if _statx is None and not _statx_broken:
            _statx = _load_statx()
            _statx_broken = _statx is None
        return _statx is not None


def _retire_statx() -> None:
    global _statx, _statx_broken
    with _module_lock:
        _statx = None
        _statx_broken = True


def _brief_size(path: str | bytes) -> int:
    """`stx_size` from one `statx(STATX_SIZE)`, following symlinks like `stat`."""
    statx = _statx
    raw = os.fsencode(path)
    buf = getattr(_local, "buf", None)
    if buf is None:
        buf = _local.buf = ctypes.create_string_buffer(_STATX_STRUCT_SIZE)
    if statx(_AT_FDCWD, raw, 0, _STATX_SIZE, buf) != 0:
        err = ctypes.get_errno()
        if err in _UNAVAILABLE:
            _retire_statx()
            raise NotImplementedError("statx is not usable here")
        raise OSError(err, os.strerror(err), raw)
    return int.from_bytes(
        buf[_STATX_SIZE_OFFSET : _STATX_SIZE_OFFSET + 8], sys.byteorder
    )


def _brief_blocks(path: str | bytes) -> int:
    """`stx_blocks x 512` from one `statx(STATX_BLOCKS)`, following symlinks."""
    statx = _statx
    raw = os.fsencode(path)
    buf = getattr(_local, "buf", None)
    if buf is None:
        buf = _local.buf = ctypes.create_string_buffer(_STATX_STRUCT_SIZE)
    if statx(_AT_FDCWD, raw, 0, _STATX_BLOCKS, buf) != 0:
        err = ctypes.get_errno()
        if err in _UNAVAILABLE:
            _retire_statx()
            raise NotImplementedError("statx is not usable here")
        raise OSError(err, os.strerror(err), raw)
    blocks = int.from_bytes(
        buf[_STATX_BLOCKS_OFFSET : _STATX_BLOCKS_OFFSET + 8], sys.byteorder
    )
    return blocks * 512


def entry_size(path: str | os.PathLike[str]) -> int:
    """Bytes in ``path``, as `os.path.getsize` reports them, without the flush.

    Same value and same errors as `os.path.getsize`: a missing path raises
    `FileNotFoundError`, an unreadable one `PermissionError`, and a symlink is
    followed to its target. The difference on NFS is that a file with dirty
    pages in this client's page cache is not written out first, so asking for
    its size cannot stall behind its own write.
    """
    if not statx_available():
        return os.stat(path).st_size
    try:
        return _brief_size(path)
    except NotImplementedError:
        return os.stat(path).st_size


def directory_cost(path: str | os.PathLike[str]) -> int:
    """Bytes a **directory** is charged for the accounting walk: its allocation.

    Not `st_size`.  Measured on the cluster's NAS (2026-09-21), a directory's
    `st_size` is not the space it occupies: an empty directory reported 4096
    and a 2000-entry one reported 16384, while `st_blocks x 512` and `du -s`
    both stayed at **512** the whole way.  Charging `st_size` would therefore
    have moved the platform's number *away* from what the sandbox's own `du`
    reports (and from what the storage bills) -- the opposite of what the
    accounting is for -- so the directory term is the allocated size, and the
    files keep the pre-existing `entry_size` convention.

    Asking for `STATX_BLOCKS` alone is still cheap on NFS: the writeback flush
    is gated on the *time* fields (see the module docstring), and this request
    carries none of them.
    """
    if not statx_available():
        return os.stat(path).st_blocks * 512
    try:
        return _brief_blocks(path)
    except NotImplementedError:
        return os.stat(path).st_blocks * 512


__all__ = ["directory_cost", "entry_size", "statx_available"]
