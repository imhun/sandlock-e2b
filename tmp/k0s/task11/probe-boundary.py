#!/usr/bin/env python3
"""Task 11 boundary probe: which "outside the allow-list" paths answer EACCES
and which answer ENOENT, in each of the two legal pure shapes.

The first pass (`probe-uid-and-errno.py`) measured the *specific* probes the
uid-isolation case and the brief's Step 5 use. This one walks the boundary:
skeleton directories that exist but are empty (`/etc`, `/var`), bound host
trees (`/usr`, `/dev`), the sandbox's own aliases (`/home/user`, `/workspace`),
a path that exists nowhere, and a host path *through* a skeleton directory.

Also dumps the synthesized root's on-disk listing (host side, from the probe
itself) so "inside the tree, outside the grant" is read off the tree, not
inferred.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.conftest import TMP_ROOT  # noqa: E402
from tests.security.conftest import route_b_sandbox, run_sh  # noqa: E402

SHAPES: dict[str, tuple[str, str]] = {
    "identity": ("off", "0"),
    "synth-realroot": ("synth", "1"),
}

SCRATCH = TMP_ROOT / "task11-boundary"
EVIDENCE = REPO_ROOT / "tmp/k0s/task11/probe-boundary.json"

TREE_PATHS = [
    "/",
    "/etc",
    "/etc/passwd",
    "/var",
    "/var/lib",
    "/var/lib/e2b-test-runtime",
    "/usr",
    "/usr/bin",
    "/dev",
    "/dev/null",
    "/home",
    "/home/user",
    "/home/user/workspace",
    "/workspace",
    "/workspace/workspace",
    "/src",
    "/src/host-only",
    "/src/host-only/SECRET",
    "/nonexistent-task11",
    "/home/user/nonexistent-task11",
]


async def measure(label: str, pure_rootfs: str, real_root: str) -> tuple[dict, dict]:
    os.environ["E2B_PURE_ROOTFS"] = pure_rootfs
    os.environ["E2B_REAL_ROOT"] = real_root
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    os.chmod(SCRATCH, 0o755)
    workspace = SCRATCH / "sbx_probe"
    (workspace / "workspace").mkdir(parents=True)
    (workspace / "workspace" / "own.txt").write_text("own\n", encoding="utf-8")

    executor, ws = route_b_sandbox(None, None, workspace=workspace)
    shape = {
        "has_sandbox_root": executor._has_sandbox_root,
        "chroot_root": executor._chroot_root,
        "synthetic_rootfs": (
            str(executor._synthetic_rootfs)
            if executor._synthetic_rootfs is not None
            else None
        ),
    }
    out: dict[str, list] = {}
    try:
        commands = ["pwd", "ls /"] + [
            f"stat {path}" for path in TREE_PATHS
        ] + [
            f"cat {path}" for path in ("/etc/passwd", "/src", "/nonexistent-task11")
        ] + [
            "cat workspace/own.txt",
            "stat ../../workspaces/sbx_other",
            "stat /var/lib/e2b-test-runtime/siblings",
            # The alias cwd contract: the aliases are *paths* in both shapes,
            # but only a rooted shape makes the kernel report one for `pwd`.
            "cd /home/user && pwd && pwd -P",
            "cd /workspace && pwd",
        ]
        for command in commands:
            code, stdout, stderr = await run_sh(executor, ws, command)
            out[command] = [
                code,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
            ]
    finally:
        executor.close()
    return out, shape


async def main() -> int:
    results: dict[str, dict] = {}
    shapes_seen: dict[str, dict] = {}
    listings: dict[str, str] = {}
    failures: list[str] = []
    for label, (pure_rootfs, real_root) in SHAPES.items():
        try:
            measured, shape = await measure(label, pure_rootfs, real_root)
            results[label] = measured
            shapes_seen[label] = shape
            root = shape["synthetic_rootfs"]
            if root:
                listings[label] = " ".join(sorted(os.listdir(root)))
        except Exception as exc:
            failures.append(f"MEASURE-FAIL [{label}] {type(exc).__name__}: {exc}")

    for label, seen in shapes_seen.items():
        print(
            f"shape [{label}] has_sandbox_root={seen['has_sandbox_root']} "
            f"chroot_root={seen['chroot_root']} root_listing={listings.get(label, '')!r}"
        )
    for line in failures:
        print(line)
    print()
    for command in results.get("identity", {}):
        left = results["identity"].get(command)
        right = results["synth-realroot"].get(command)
        mark = "DIFF" if left != right else "    "
        print(f"{mark} {command:<52} identity={left}")
        print(f"     {'':<52} synth   ={right}")
        if left == right:
            print()
    EVIDENCE.write_text(
        json.dumps({"shapes": shapes_seen, "results": results}, indent=2),
        encoding="utf-8",
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
