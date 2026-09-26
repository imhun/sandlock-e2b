#!/usr/bin/env python3
"""Does the synth + emulated-root shape start working if the allow-list covers
the mount points that are not in it today?

`landlock.rs` gives a chroot-mode mount *source* rights only when the policy
declares rights for the matching *mount point* (`path_rule_rights`), and
`chroot/dispatch.rs::can_read` waves a path through on `is_mounted` alone. The
synthesized table mounts `/usr /bin /sbin /lib /lib64 /opt /dev` while the
allow-list only names `/usr /lib /bin /opt`, so `/sbin`, `/lib64` and `/dev`
are mounted-but-undeclared. This probe adds those to `fs_readable` and reports
whether the generation survives -- a positive result makes that asymmetry the
mechanism, a negative one rules it out.

Usage: python tmp/k0s/probe-task9-allowlist.py <n15|synth-emulated|synth-realroot> [extra,extra]
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from envd_service.executors.sandlock import SandlockExecutor  # noqa: E402
from tests.security.conftest import route_b_sandbox, run_sh  # noqa: E402

shape = sys.argv[1] if len(sys.argv) > 1 else "synth-emulated"
extra = [p for p in (sys.argv[2].split(",") if len(sys.argv) > 2 else []) if p]
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

if extra:
    original = SandlockExecutor._policy_ceiling

    def _widened(self):
        ceiling = original(self)
        readable = list(ceiling.get("fs_readable") or [])
        for path in extra:
            if path not in readable:
                readable.append(path)
        ceiling["fs_readable"] = readable
        return ceiling

    SandlockExecutor._policy_ceiling = _widened


async def main() -> int:
    executor, workspace = route_b_sandbox(None, None)
    print(f"shape={shape} extra={extra} chroot={executor._chroot_root}")
    try:
        for command in ("echo hi", "ls /", "pwd"):
            code, out, err = await run_sh(executor, workspace, command)
            print(f"[{command!r}] rc={code} out={out!r} err={err!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED {type(exc).__name__}: {exc}")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
