#!/usr/bin/env python3
"""Does the synthesized root work once its ELF interpreter exists inside it?

`chroot/dispatch.rs::handle_chroot_exec` opens the workload's `PT_INTERP`
interpreter like this:

    // Open the image's interpreter from the chroot root (intentionally
    // NOT mount-aware -- the dynamic linker should come from the base
    // image, not from workspace mounts).
    openat2_in_root(ctx.root, &interp_path, ...)

and returns that open's errno to the child when it fails. For an *image* root
that is correct. The synthesized root is not an image: its `/lib64` is an empty
stub whose content comes from the mount table, so the open finds nothing and the
exec dies before `main` -- which collapses the generation (the parking program
*is* the generation's main child). The real-root state never takes this branch
(`ctx.child_is_pivoted` hands the exec back to the kernel).

This probe copies the host interpreter to `<root>/lib64/ld-linux-x86-64.so.2`
inside the synthesized tree right after `_policy_ceiling` materialized it. A
positive result makes the interpreter lookup the mechanism.

Usage: python tmp/k0s/probe-task9-interp.py <shape>
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from envd_service.executors.sandlock import SandlockExecutor  # noqa: E402
from tests.security.conftest import route_b_sandbox, run_sh  # noqa: E402

shape = sys.argv[1] if len(sys.argv) > 1 else "synth-emulated"
envs = {
    "n15": ("off", "0"),
    "synth-emulated": ("synth", "0"),
    "synth-realroot": ("synth", "1"),
}
pure_rootfs, real_root = envs[shape]
os.environ["E2B_PURE_ROOTFS"] = pure_rootfs
os.environ["E2B_REAL_ROOT"] = real_root
base = REPO_ROOT / "tmp/k0s/scratch/census"
base.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("E2B_PURE_ROOTFS_DIR", str(base / "_pure_rootfs"))

# The interpreter the sample binary names, read the way the mediator reads it.
INTERP = subprocess.run(
    ["readelf", "-l", "/usr/bin/printf"], capture_output=True, text=True
).stdout
interp_path = None
for line in INTERP.splitlines():
    if "interpreter:" in line:
        interp_path = line.split("interpreter:", 1)[1].strip().strip("[]")
print(f"interpreter of /usr/bin/printf: {interp_path!r}")

original = SandlockExecutor._policy_ceiling


def _plant_interpreter(self):
    ceiling = original(self)
    root = self._synthetic_rootfs
    if root is not None and interp_path:
        target = Path(str(root)) / interp_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(interp_path, target)
        target.chmod(0o755)
        print(f"planted interpreter at {target} ({target.stat().st_size} bytes)")
    return ceiling


SandlockExecutor._policy_ceiling = _plant_interpreter


async def main() -> int:
    executor, workspace = route_b_sandbox(None, None)
    print(f"shape={shape} chroot={executor._chroot_root}")
    try:
        for command in ("echo hi", "pwd"):
            code, out, err = await run_sh(executor, workspace, command)
            print(f"[{command!r}] rc={code} out={out!r} err={err!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED {type(exc).__name__}: {exc}")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
