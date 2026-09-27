"""Control experiment: pure-shape exec over the direct executor, low fd table.

Same client-fd manipulation as `tmp/f11_fdcount_probe.py`, but without the
in-process control plane / SDK route: drive `SandlockExecutor` the way
`tmp/fup3_thread_probe.py` does.  If stdout arrives here at N=0, the fork's exec
stdio handoff is fine in the low-fd layout and only the gateway+SDK route fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/workspace")

N = int(sys.argv[1]) if len(sys.argv) > 1 else 0
held = [os.open("/dev/null", os.O_RDONLY) for _ in range(N)]

from envd_service.executors.base import ExecConfig  # noqa: E402
from envd_service.executors.sandlock import SandlockExecutor  # noqa: E402


async def drain(proc, wait_s=30):
    chunks = []

    async def _collect():
        async for item in proc.output():
            chunks.append(item)

    col = asyncio.create_task(_collect())
    try:
        code = await asyncio.wait_for(proc.exit_code(), timeout=wait_s)
    except asyncio.TimeoutError:
        code = "TIMEOUT"
    col.cancel()
    return {
        "exit": code,
        "stdout": b"".join(b for k, b in chunks if k == "stdout").decode(errors="replace"),
        "stderr": b"".join(b for k, b in chunks if k == "stderr").decode(errors="replace"),
    }


async def main() -> int:
    base = Path("/workspace/tmp/f11-direct-exec")
    base.mkdir(parents=True, exist_ok=True)
    ws = tempfile.mkdtemp(dir=str(base))
    ex = SandlockExecutor(
        workspace_dir=ws, base_image=None, image_rootfs=None, memory_mb=512,
        cpu_percent=100, disk_mb=1024, max_processes=256, max_open_files=4096,
        allow_internet_access=False, enable_network=True,
    )
    p = await ex.start(ExecConfig(
        cmd=["/usr/local/bin/python3", "-c", "print('direct-ok', flush=True)"],
        env={}, cwd=ws, stdin_enabled=False,
    ))
    print(f"held={len(held)} highest={max(held) if held else -1}", flush=True)
    print("result:", json.dumps(await drain(p)), flush=True)
    p.kill(9)
    ex.close()
    return 0


raise SystemExit(asyncio.run(main()))
