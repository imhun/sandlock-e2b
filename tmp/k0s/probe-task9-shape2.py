#!/usr/bin/env python3
"""Focused repro: the synth + emulated-root shape (E2B_REAL_ROOT=0) alone.

Run inside the lane container:
  python tmp/k0s/probe-task9-shape2.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="LOG %(levelname)s %(name)s: %(message)s")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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


async def main() -> int:
    executor, workspace = route_b_sandbox(None, None)
    print(
        f"shape={shape} has_root={executor._has_sandbox_root} "
        f"chroot={executor._chroot_root} ws={workspace}"
    )
    print(f"route_b_active={executor._route_b_active} decline={executor._route_b_decline}")
    try:
        for cmd in ("echo hi", "pwd", "ls /", "ls /proc"):
            code, out, err = await run_sh(executor, workspace, cmd)
            print(f"[{cmd!r}] rc={code} out={out!r} err={err!r}")
    except Exception:
        traceback.print_exc()
        inst = getattr(executor, "_instance", None)
        print(f"instance={inst!r}")
        reader = getattr(inst, "slot_stderr", None)
        if reader is not None:
            print(f"SLOT-STDERR: {reader()!r}")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
