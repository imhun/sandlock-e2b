#!/usr/bin/env python3
"""Why does the synth + emulated-root generation collapse?

`route_b.PARKING_PROGRAM` is the generation's *main* child; when it exits the
container collapses and every later verb answers "instance is closed". The main
child's stdio is wired to /dev/null by core, so this probe swaps the parking
script for one that writes its own stderr and uid into the sandbox's workspace
(writable, and a host path we can read afterwards).
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

import envd_service.route_b as route_b  # noqa: E402
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
OUT = base / f"parkdiag-{shape}"
OUT.mkdir(parents=True, exist_ok=True)
for stale in OUT.glob("park.*"):
    stale.unlink()

route_b.PARKING_PROGRAM = {
    "argv": [
        "/bin/sh",
        "-c",
        # stderr first, so a failure *before* the first line is also visible.
        'exec 2>/home/user/park.err; echo "ALIVE uid=$(id -u)" '
        '> /home/user/park.out; trap "" TERM HUP INT QUIT USR1 USR2 PIPE; '
        'while :; do kill -STOP $$; done',
    ]
}


async def main() -> int:
    executor, workspace = route_b_sandbox(None, None)
    print(f"shape={shape} chroot={executor._chroot_root} ws={workspace}")
    try:
        code, out, err = await run_sh(executor, workspace, "echo hi")
        print(f"[echo hi] rc={code} out={out!r} err={err!r}")
    except Exception:
        traceback.print_exc(limit=2)
    finally:
        executor.close()
    for name in ("park.out", "park.err"):
        path = Path(workspace) / name
        value = path.read_bytes() if path.exists() else None
        (OUT / name).write_bytes(value or b"")
        print(f"{name}: {value!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
