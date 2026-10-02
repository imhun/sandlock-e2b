#!/usr/bin/env python3
"""Capacity account for the local-first tree (Task 1 Step 1 ②).

Answers "what does a node have to hold once the trees are local": the shared
export root's per-namespace bytes (what stays shared), the node-local disk's
headroom, and what one sandbox tree plus one snapshot of it actually costs. The
per-node arithmetic is in the design document; this script is where its inputs
come from, on the live volume, so the table is not a transcription.

It also reports the snapshot records' ``created_at`` values, because the plan
asks for a *daily increment* and a *retention window*: this deployment has no
TTL/GC for snapshots at all (records and payloads live until someone deletes
them), so the honest inputs are "bytes per snapshot" and "how many are retained",
not a decay curve the code does not implement.

Read-only. Run it where both mounts are visible (an agent's ``maint`` face has
the shared volume rw and the node-local image cache)::

    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec -i <agent-pod> -c maint -- python3 - \
        < deploy/scripts/acceptance/local_first_capacity_account.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

def _repo_root() -> Path | None:
    """The checkout root when this runs as a **file**, else ``None``.

    The documented invocation inside the pods is ``python3 - < <this file>``:
    there the interpreter starts in ``/app`` (the repo root) and puts the *cwd*
    on ``sys.path``, so nothing is needed. Running it as ``python3 <path>``
    puts the script's own directory on ``sys.path`` instead, which is why the
    repo root is added here.

    Both shapes have to be handled *and neither may raise*: on Python 3.12 a
    piped script has no ``__file__`` at all, and on 3.14 it is defined as the
    pseudo-name ``<stdin>`` -- where ``parents[3]`` raises ``IndexError``, and
    the probe then dies before printing anything (hit on the cluster
    2026-10-02, the first run of this probe after the guard was added).
    """
    try:
        here = Path(__file__).resolve()  # noqa: F821 - defined when run as a file
    except NameError:
        return None
    if here.name.startswith("<"):  # ``<stdin>``, ``<string>``, ``-c``
        return None
    try:
        return here.parents[3]
    except IndexError:
        return None


_ROOT = _repo_root()
if _ROOT is not None and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The payload's shape is decided in one place (``gateway_common/paths.py``);
# this probe runs inside the agent image's ``maint`` container, whose workdir is
# ``/app``. A second spelling of ``fs`` here counted every Task 2 snapshot
# (``fs.tar``) as having no payload at all.
from gateway_common.paths import snapshot_payload

KNOBS = (
    "E2B_SHARED_VOLUME_ROOT",
    "E2B_WORKSPACE_BASE",
    "E2B_STATE_BASE",
    "E2B_NODE_STATE_BASE",
    "E2B_IMAGE_CACHE_DIR",
    "E2B_IMAGE_CACHE_MAX_BYTES",
    "E2B_NODE_DISK_MB",
    "E2B_PLATFORM_DISK_MB",
    "E2B_DEFAULT_DISK_MB",
    "E2B_TREES_SHARED",
)


def measure(path: Path, max_files: int) -> dict[str, object]:
    """du-equivalent with a file cap, so a slow NAS walk cannot hang the probe."""
    started = time.perf_counter()
    files = dirs = 0
    total = 0
    truncated = False
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs += 1
                            stack.append(Path(entry.path))
                        else:
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
        if files + dirs > max_files:
            truncated = True
            break
    return {
        "path": str(path),
        "files": files,
        "dirs": dirs,
        "bytes": total,
        "walk_seconds": round(time.perf_counter() - started, 2),
        "truncated": truncated,
    }


def statvfs(path: Path) -> dict[str, object] | None:
    try:
        st = os.statvfs(str(path))
    except OSError:
        return None
    return {
        "path": str(path),
        "total_bytes": st.f_blocks * st.f_frsize,
        "free_bytes": st.f_bavail * st.f_frsize,
        "used_bytes": (st.f_blocks - st.f_bfree) * st.f_frsize,
    }


def snapshot_records(root: Path) -> list[dict[str, object]]:
    store = root / "_snapshots"
    out: list[dict[str, object]] = []
    if not store.is_dir():
        return out
    for entry in sorted(store.iterdir()):
        if not entry.is_dir():
            continue
        found = snapshot_payload(entry)
        shape, _payload = found if found is not None else (None, None)
        row: dict[str, object] = {
            "id": entry.name,
            "record": (entry / "snapshot.json").is_file(),
            "payload": shape is not None,
            "payload_shape": shape,
            "created_at": None,
            "status": None,
            "payload_bytes": None,
        }
        record = entry / "snapshot.json"
        if row["record"]:
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
                row["created_at"] = data.get("created_at")
                row["status"] = data.get("status")
            except (OSError, ValueError):
                pass
        if row["payload"] and not row["record"]:
            # A payload-only id has no created_at on the volume; mtime is all
            # there is, and it is worth having.
            row["payload_mtime"] = int(entry.stat().st_mtime)
        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.environ.get("E2B_SHARED_VOLUME_ROOT", "/var/lib/e2b-sandboxes"))
    parser.add_argument("--node-local", default=os.environ.get("E2B_IMAGE_CACHE_DIR", "/var/lib/e2b-images"))
    parser.add_argument("--workspaces", default=None, help="default: <root>/workspaces")
    parser.add_argument("--max-files", type=int, default=200000)
    args = parser.parse_args()

    root = Path(args.root)
    workspaces = Path(args.workspaces) if args.workspaces else root / "workspaces"
    node_local = Path(args.node_local)

    out: dict[str, object] = {
        "knobs": {k: os.environ.get(k) for k in KNOBS},
        "root": str(root),
        "namespaces": [],
        "node_local": str(node_local),
        "filesystems": [],
        "snapshot_records": snapshot_records(root),
        "trees": [],
    }
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                out["namespaces"].append(measure(entry, args.max_files))  # type: ignore[union-attr]
    if workspaces.is_dir():
        for entry in sorted(workspaces.iterdir()):
            if entry.is_dir():
                row = measure(entry, args.max_files)
                row["name"] = entry.name
                out["trees"].append(row)  # type: ignore[union-attr]
    out["filesystems"] = [v for v in (statvfs(root), statvfs(workspaces), statvfs(node_local), statvfs(Path("/"))) if v]

    # The per-node arithmetic needs "one tree" and "one snapshot of it" in the
    # same units. If no live tree exists right now, say so instead of inventing
    # one: the design document carries the measured 900 MiB sample instead.
    out["totals"] = {
        "namespace_bytes": sum(int(n["bytes"]) for n in out["namespaces"]),  # type: ignore[union-attr]
        "tree_bytes": sum(int(t["bytes"]) for t in out["trees"]),  # type: ignore[union-attr]
        "tree_count": len(out["trees"]),  # type: ignore[arg-type]
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
