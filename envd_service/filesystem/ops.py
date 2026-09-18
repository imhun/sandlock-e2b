"""Filesystem operations over a sandbox workspace root.

This module is the **read** side of the filesystem service plus the
*decisions* every write makes before it touches anything. It deliberately does
not perform the writes: since N28 the sandbox is the single writer of its own
tree (``envd_service.filesystem.writer``), and the worker only looks. The
guards live here rather than inside the writer's shell script because they are
what the API contract is written in terms of (``already_exists`` /
``not_found``), and because they need nothing but a read.
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path
from typing import Any

from gateway_common.errors import (
    ConnectError,
    already_exists,
    invalid_argument,
    not_found,
)
from gateway_common.paths import PathTraversalError, resolve_under_root


def _permissions(mode: int) -> str:
    """``rwxr-xr-x``-style permission string."""
    perms = ""
    for shift in (6, 3, 0):
        bits = (mode >> shift) & 0o7
        perms += "r" if bits & 0o4 else "-"
        perms += "w" if bits & 0o2 else "-"
        perms += "x" if bits & 0o1 else "-"
    return perms


def _file_type(st: os.stat_result) -> str:
    if stat_module.S_ISDIR(st.st_mode):
        return "FILE_TYPE_DIRECTORY"
    if stat_module.S_ISLNK(st.st_mode):
        return "FILE_TYPE_SYMLINK"
    return "FILE_TYPE_FILE"


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _entry(root: Path, path: Path, st: os.stat_result | None = None) -> dict[str, Any]:
    st = st or path.stat()
    rel = _relative(root, path)
    name = path.name or rel
    try:
        owner = str(st.st_uid)
        group = str(st.st_gid)
    except AttributeError:  # pragma: no cover
        owner = group = "0"
    entry: dict[str, Any] = {
        "name": name,
        "type": _file_type(st),
        "path": rel,
        # protobuf JSON maps uint64 to a string.
        "size": str(st.st_size),
        "mode": st.st_mode & 0o7777,
        "permissions": _permissions(st.st_mode),
        "owner": owner,
        "group": group,
        "modifiedTime": _iso_mtime(st),
        "symlinkTarget": os.readlink(path) if stat_module.S_ISLNK(st.st_mode) else None,
        "metadata": None,
    }
    return entry


def _iso_mtime(st: os.stat_result) -> str:
    import datetime

    dt = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _resolve(root: Path, path: str) -> Path:
    try:
        return resolve_under_root(root, path)
    except PathTraversalError as e:
        raise invalid_argument(str(e)) from e


class FilesystemOps:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def stat(self, path: str) -> dict[str, Any]:
        target = _resolve(self.root, path)
        try:
            st = target.stat()
        except FileNotFoundError:
            raise not_found(f"Path {path} not found")
        except NotADirectoryError:
            raise not_found(f"Path {path} not found")
        return _entry(self.root, target, st)

    # -- write guards ------------------------------------------------------
    #
    # Each guard answers "may this write happen, and if not, which documented
    # code does the caller get?" and returns the resolved target(s) the writer
    # is then told to operate on. The worker is allowed to answer them: it is
    # a read of the tree it just served the caller's request against.

    def require_creatable(self, path: str) -> Path:
        """Resolve ``path`` for ``MakeDir``, refusing an existing entry.

        The path arrives *resolved*, so a symlink is judged as its target --
        that is the semantics the request path has always had
        (``resolve_under_root``), and the helper then runs against the same
        resolved name.
        """
        target = _resolve(self.root, path)
        if target.exists():
            raise already_exists(f"Path {path} already exists")
        return target

    def require_movable(self, source: str, destination: str) -> tuple[Path, Path]:
        """Resolve both ends of ``Move``, refusing a missing/occupied pair."""
        src = _resolve(self.root, source)
        dst = _resolve(self.root, destination)
        if not src.exists():
            raise not_found(f"Path {source} not found")
        if dst.exists():
            raise ConnectError("already_exists", f"Path {destination} already exists", 409)
        return src, dst

    def require_removable(self, path: str) -> Path:
        """Resolve ``path`` for ``Remove``, refusing an absent entry."""
        target = _resolve(self.root, path)
        if not target.exists():
            raise not_found(f"Path {path} not found")
        return target

    def list_dir(self, path: str, depth: int) -> dict[str, Any]:
        target = _resolve(self.root, path)
        if not target.exists():
            raise not_found(f"Path {path} not found")
        if not target.is_dir():
            raise invalid_argument(f"Path {path} is not a directory")
        entries: list[dict[str, Any]] = []
        self._walk(target, 0, depth, entries)
        entries.sort(key=lambda e: e["path"])
        return {"entries": entries}

    def _walk(self, target: Path, current: int, depth: int, out: list[dict]) -> None:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                path = Path(entry.path)
                out.append(_entry(self.root, path, st))
                if (
                    entry.is_dir(follow_symlinks=False)
                    and (depth == 0 or current + 1 < depth)
                ):
                    self._walk(path, current + 1, depth, out)
