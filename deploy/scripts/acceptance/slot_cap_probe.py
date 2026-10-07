"""量一件事：route-B 槽位（=路径中介进程）与被 confine 的子进程各自持有哪些 cap。
worker 自己若持有 SYS_ADMIN，会不会顺着 setpriv 漏给沙箱侧。"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
from envd_service.executors.base import ExecConfig  # noqa: E402
from envd_service.executors.sandlock import SandlockExecutor  # noqa: E402
from envd_service.own_identity import OwnIdentityConfig, slot_pool_for  # noqa: E402
from tests.security.conftest import sandbox_tmpdir  # noqa: E402

UID = 21710
NAMES = {0: "CHOWN", 1: "DAC_OVERRIDE", 3: "FOWNER", 5: "KILL", 6: "SETGID", 7: "SETUID",
         8: "SETPCAP", 18: "SYS_CHROOT", 19: "SYS_PTRACE", 21: "SYS_ADMIN", 27: "MKNOD",
         36: "SETFCAP", 39: "SECCOMP"}


def caps_of(pid):
    try:
        txt = Path(f"/proc/{pid}/status").read_text()
    except OSError as exc:
        return f"unreadable({exc.errno})"
    eff = int([l for l in txt.splitlines() if l.startswith("CapEff:")][0].split()[1], 16)
    uid = [l for l in txt.splitlines() if l.startswith("Uid:")][0].split()[1]
    return {"uid": int(uid), "eff": [NAMES.get(b, f"bit{b}") for b in range(40) if (eff >> b) & 1]}


async def main():
    Path("/tmp/rb-probe-scratch").mkdir(exist_ok=True)
    os.chmod("/tmp/rb-probe-scratch", 0o755)
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    rootfs = resolve_image_rootfs("python:3.11-slim", str(sandbox_tmpdir(suffix="-cache", uid=UID)))
    ws = sandbox_tmpdir(suffix="-ws", uid=UID)
    print(json.dumps({"worker": {"euid": os.geteuid(), "caps": caps_of(os.getpid())}}))
    ex = SandlockExecutor(
        workspace_dir=str(ws), base_image="python:3.11-slim", image_rootfs=rootfs,
        host_uid=UID, per_sandbox_uid=True, memory_mb=512, cpu_percent=100, disk_mb=1024,
        max_processes=64, max_open_files=4096, allow_internet_access=False, enable_network=False,
        sandbox_id="sbx_slotcaps", own_identity=OwnIdentityConfig(
            mode="auto", uid_start=UID, uid_size=2, tmp_root=Path("/tmp/rb-probe-scratch")),
    )
    running = await ex.start(ExecConfig(cmd=["/bin/sh", "-c", "sleep 20 & echo $"], env={}, cwd=str(ws), stdin_enabled=False))
    out = b""
    async for kind, chunk in running.output():
        if kind == "stdout":
            out += chunk
    pool = slot_pool_for(ex._own_identity)
    slot = pool.slot("sbx_slotcaps")
    stats = await ex._ensure_instance_async()
    print(json.dumps({
        "slot_pid": slot.process.pid,
        "slot": caps_of(slot.process.pid),
        "instance": caps_of(stats.instance_pid) if getattr(stats, "instance_pid", None) else None,
        "guest_child": caps_of(int(out.decode().strip())) if out.strip().isdigit() else None,
    }))
    ex.close()

asyncio.run(main())
