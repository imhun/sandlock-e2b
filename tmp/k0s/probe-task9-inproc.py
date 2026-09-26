#!/usr/bin/env python3
"""Shape variant with the *in-process* backend, so the fork's own error surfaces.

Route B answers with one opaque code ("instance is closed") for everything that
goes wrong inside the slot. Without a slot the FFI returns a typed error, which
is the only way to read the reason from outside. The in-process mediated shape
is refused when the mediator would run as root and the sandbox as another uid
(SL-1), so this asks for the worker's own identity.

Usage: python tmp/k0s/probe-task9-inproc.py <shape>
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

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
    executor, workspace = route_b_sandbox(
        None, None, with_route_b=False, host_uid=None, per_sandbox_uid=False
    )
    print(
        f"shape={shape} route_b_active={executor._route_b_active} "
        f"decline={executor._route_b_decline} chroot={executor._chroot_root}"
    )
    try:
        for command in ("echo hi", "pwd"):
            code, out, err = await run_sh(executor, workspace, command)
            print(f"[{command!r}] rc={code} out={out!r} err={err!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
