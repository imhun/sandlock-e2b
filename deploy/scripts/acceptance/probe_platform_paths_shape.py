#!/usr/bin/env python3
"""Task 1: the platform paths' real mode/owner, read-only.

The control-plane uid relocation (docs/superpowers/plans/2026-10-09-control-plane-uid-relocation.md)
needs one fact this repo does not have: the *production* mode/owner of the eight subpaths the
control plane mounts read-write (docs/c3-privilege-relocation.md §13.8). Every "the migration can
just chown them" sentence rests on that reading, so it is taken, not assumed.

Read-only by construction: `os.lstat` only, nothing is opened for writing and nothing is created.

Usage (from a pod that has the export mounted, or from the host):

    python3 deploy/scripts/acceptance/probe_platform_paths_shape.py \
        [--root /var/lib/e2b-sandboxes] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

#: The eight subpaths the control plane's pod mounts RW (`control-plane.yaml`, §13.8). Order is
#: fixed: the pin in `tests/unit/test_probe_platform_paths_shape.py` compares it exactly.
PLATFORM_PATHS: tuple[str, ...] = (
    "_builds",
    "_images",
    "_secrets",
    "_snapshots",
    "_templates",
    "_volumes",
    "state",
    "workspaces/_migrate",
)


def platform_path_shape(root: str | os.PathLike[str]) -> list[dict[str, object]]:
    """``[{"path","mode","uid","gid"}]`` for the eight platform paths under ``root``.

    A path that is not there is reported as ``mode="missing"`` with ``uid``/``gid`` = ``-1``:
    "the directory does not exist yet" is a fact the migration needs, not an error.
    ``mode`` is the octal spelling (``"0o755"``) so it can be compared against the values the
    manifests and the docs use.
    """
    root = Path(root)
    entries: list[dict[str, object]] = []
    for rel in PLATFORM_PATHS:
        try:
            info = os.lstat(root / rel)
        except FileNotFoundError:
            entries.append({"path": rel, "mode": "missing", "uid": -1, "gid": -1})
            continue
        entries.append(
            {
                "path": rel,
                "mode": oct(stat.S_IMODE(info.st_mode)),
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
        )
    return entries


def mode_is_world_readable(entry: dict[str, object]) -> bool:
    """Whether ``entry`` (a row from :func:`platform_path_shape`) is other-readable.

    ``True`` iff ``stat.S_IROTH`` is set on the reported mode. A path whose ``mode`` is
    ``"missing"`` is ``False``: an absent path cannot be read by anybody, and the migration's
    criterion (Task 4) must not treat "not there yet" as "readable". The comparison goes through
    ``stat`` so the answer matches the kernel's bits rather than the string.
    """
    mode = entry.get("mode")
    if not isinstance(mode, str) or mode == "missing":
        return False
    return bool(int(mode, 8) & stat.S_IROTH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/var/lib/e2b-sandboxes")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    entries = platform_path_shape(args.root)
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    width = max(len(str(e["path"])) for e in entries)
    for e in entries:
        print(f"{e['path']:<{width}}  {e['mode']:>9}  {e['uid']}:{e['gid']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
