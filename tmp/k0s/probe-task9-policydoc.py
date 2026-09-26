#!/usr/bin/env python3
"""Print the create document E2B sends for a shape (the slot's ceiling).

Run: python tmp/k0s/probe-task9-policydoc.py <n15|synth-emulated|synth-realroot>
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
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
    executor, workspace = route_b_sandbox(None, None)
    print(f"### shape={shape} ws={workspace}")
    try:
        ceiling = executor._policy_ceiling()
        print("### ceiling:")
        print(json.dumps(ceiling, indent=2, sort_keys=True))
        print("### sandbox document:")
        from envd_service.executors.sandlock import supervise_policy_document

        print(json.dumps(supervise_policy_document(ceiling), indent=2, sort_keys=True))
        code, out, err = await run_sh(executor, workspace, "echo hi")
        print(f"### echo hi rc={code} out={out!r} err={err!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"### FAILED {type(exc).__name__}: {exc}")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
