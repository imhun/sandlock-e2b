#!/usr/bin/env python3
"""Which main program can the synth + emulated-root generation keep alive?

The production parking program is
``/bin/sh -c "trap '' TERM HUP INT QUIT USR1 USR2 PIPE; while :; do kill -STOP $$; done"``.
In the synth + emulated-root shape the generation collapses right after launch,
so this walks a few variants to see whether the failure is "the mediator cannot
exec /bin/sh at all" or something narrower.
"""
from __future__ import annotations

import asyncio
import os
import sys
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

VARIANTS: dict[str, list[str]] = {
    "production-park": ["/bin/sh", "-c", route_b.PARKING_SCRIPT],
    "sh-sleep": ["/bin/sh", "-c", "sleep 300"],
    "sh-true": ["/bin/sh", "-c", "true"],
    "sleep-binary": ["/usr/bin/sleep", "300"],
    "env": ["/usr/bin/env"],
}


async def try_program(name: str, argv: list[str]) -> None:
    route_b.PARKING_PROGRAM = {"argv": list(argv)}
    executor, workspace = route_b_sandbox(None, None)
    try:
        code, out, err = await run_sh(executor, workspace, "echo hi")
        print(f"main={name:16} argv={argv!r} -> rc={code} out={out!r} err={err!r}")
    except Exception as exc:  # noqa: BLE001 - the point is the failure class
        print(f"main={name:16} argv={argv!r} -> {type(exc).__name__}: {exc}")
    finally:
        executor.close()


async def main() -> int:
    for name, argv in VARIANTS.items():
        await try_program(name, argv)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
