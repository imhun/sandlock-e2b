#!/usr/bin/env python3
"""Task 9 ask 2: *why* does synth + emulated root die at launch?

The 32 errors of `task9/pure-rootfs-sec-realroot0.log` are all
`SlotRefusal: instance is closed ...` at fixture setup, and the census fails
its whole `synth-emulated` shape the same way. So the question is not "which
test" but "why is the generation dead before the first exec".

This probe answers it from the pieces the shape actually consists of:

  * the shape triple (chroot root / has_sandbox_root / route-B active),
  * the **skeleton tree the E2B side materializes** (walked, not assumed),
  * the **mount table the policy declares** (sources are host paths),
  * the **child's own failure breadcrumb**: `SANLOCK_REALROOT_TRACE` makes
    `realroot::record_failure` / `init`'s `fail!` write the reason to a file
    (the slot shapes drop the child's stderr, so this is the only channel),
  * the slot's own stderr, and
  * whether anything at all is reachable *through* the skeleton (the
    translated spelling the emulated root resolves against).

Run inside the lane; see `tmp/k0s/task9-diag.sh`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.security.conftest import route_b_sandbox, run_sh  # noqa: E402

SHAPES = {
    "n15": ("off", "0"),
    "synth-emulated": ("synth", "0"),
    "synth-realroot": ("synth", "1"),
}


def walk(root: Path, limit: int = 60) -> list[str]:
    """`<mode> <type> <relative path> <size>` for the skeleton, breadth-first."""
    out: list[str] = []
    stack = [root]
    while stack and len(out) < limit:
        here = stack.pop(0)
        try:
            entries = sorted(here.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            out.append(f"  !! {here}: {exc}")
            continue
        for entry in entries:
            try:
                st = entry.lstat()
            except OSError as exc:
                out.append(f"  !! {entry}: {exc}")
                continue
            kind = "dir" if entry.is_dir() and not entry.is_symlink() else "other"
            if entry.is_symlink():
                kind = f"symlink->{os.readlink(entry)}"
            out.append(
                f"  {oct(st.st_mode & 0o7777)} {kind:>8} "
                f"{entry.relative_to(root)} ({st.st_size}B)"
            )
            if kind == "dir":
                stack.append(entry)
    if len(out) >= limit:
        out.append(f"  ... truncated at {limit}")
    return out


async def main() -> int:
    shape = sys.argv[1] if len(sys.argv) > 1 else "synth-emulated"
    print(f"### shape={shape}")
    pure_rootfs, real_root = SHAPES[shape]
    os.environ["E2B_PURE_ROOTFS"] = pure_rootfs
    os.environ["E2B_REAL_ROOT"] = real_root
    base = REPO_ROOT / "tmp/k0s/scratch/census"
    base.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("E2B_PURE_ROOTFS_DIR", str(base / "_pure_rootfs"))

    executor, workspace = route_b_sandbox(None, None)
    # The child's breadcrumb has to land *inside the sandbox's own grant*: the
    # trace open happens after the policy is in force, so a host path outside
    # the workspace is denied and the reason is lost (measured: an empty trace
    # file when it pointed at /workspace/tmp/...). The workspace is granted and
    # owned by the sandbox uid, and this probe runs as the container's root, so
    # it can copy the file out afterwards.
    child_trace = Path(workspace) / "realroot-trace.txt"
    os.environ["SANLOCK_REALROOT_TRACE"] = str(child_trace)
    shape_seen = {
        "E2B_PURE_ROOTFS": os.environ["E2B_PURE_ROOTFS"],
        "E2B_REAL_ROOT": os.environ["E2B_REAL_ROOT"],
        "has_sandbox_root": executor._has_sandbox_root,
        "chroot_root": executor._chroot_root,
        "route_b_active": executor._route_b_active,
        "route_b_decline": executor._route_b_decline,
        "workspace": str(workspace),
    }
    print("### shape: " + json.dumps(shape_seen))

    skeleton = Path(executor._chroot_root) if executor._chroot_root else None
    if skeleton is not None and skeleton.exists():
        print(f"### skeleton tree ({skeleton}):")
        print("\n".join(walk(skeleton)) or "  (empty)")

    # What the emulated root would resolve a virtual path against: the
    # translated spelling inside the skeleton for a binary the workload needs.
    for virtual in ("/bin/sh", "/usr/bin/python3", "/home/user", "/workspace"):
        candidate = skeleton.joinpath(virtual.lstrip("/")) if skeleton else None
        state = (
            "no-skeleton"
            if candidate is None
            else ("exists" if candidate.exists() else "MISSING")
        )
        print(f"### translated {virtual} -> {candidate} : {state}")

    requests = ["echo hi", "ls /", "cat /etc/passwd"]
    for command in requests:
        try:
            code, out, err = await run_sh(executor, workspace, command)
            print(f"[{command}] rc={code} out={out!r} err={err!r}")
        except Exception as exc:  # the shape refuses before/at the exec
            print(f"[{command}] RAISED {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
    try:
        executor.close()
    except Exception as exc:
        print(f"close() raised {type(exc).__name__}: {exc}")
    if child_trace.exists():
        text = child_trace.read_text(encoding="utf-8", errors="replace")
        print(f"### child breadcrumbs ({child_trace}):")
        print(text or "  (file exists, no lines)")
        # keep it on the host: the workspace is container-local /tmp, and the
        # container is --rm, while /workspace is the bind-mounted repo.
        out = REPO_ROOT / f"tmp/k0s/task9/trace-{shape}.from-child.txt"
        out.write_text(text, encoding="utf-8")
    else:
        print(f"### child breadcrumbs ({child_trace}): (no file)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
