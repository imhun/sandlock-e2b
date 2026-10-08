"""机制级探针：在给定 capset 的容器里，逐项问 route B / 进程内后端「还活着吗」。
只打印结论，不改任何状态。每行一个 JSON。"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
from envd_service.executors.base import ExecConfig  # noqa: E402
from envd_service.executors.sandlock import SandlockExecutor  # noqa: E402
from envd_service.own_identity import OwnIdentityConfig  # noqa: E402
from tests.security.conftest import SANDBOX_UID, sandbox_tmpdir  # noqa: E402

UID = 21700
PROBE = (
    "id -u; "
    "mknod blk b 8 0; echo mknod-rc=$?; "
    "echo mark > t.txt; echo wrote=$?; "
    "echo done"
)


def report(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def build(base_image, rootfs, host_uid, mode):
    # 工作区属主必须就是沙箱自己的 uid，否则 EACCES 会伪装成"权限不够"
    ws = sandbox_tmpdir(suffix="-ws", uid=host_uid if host_uid is not None else SANDBOX_UID)
    ex = SandlockExecutor(
        workspace_dir=str(ws),
        base_image=base_image,
        image_rootfs=rootfs,
        host_uid=host_uid,
        per_sandbox_uid=host_uid is not None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=f"sbx_probe_{mode}_{host_uid}",
        own_identity=OwnIdentityConfig(
            mode=mode,
            uid_start=host_uid or UID,
            uid_size=2,
            tmp_root=Path("/tmp/rb-probe-scratch"),
        ),
    )
    return ex, ws


async def run_case(label, ex, ws):
    """Create + probe. 返回 (是否建成, 客体内 uid, mknod 返回码, 节点是否留下, owner)"""
    try:
        running = await ex.start(
            ExecConfig(cmd=["/bin/sh", "-c", PROBE], env={}, cwd=str(ws), stdin_enabled=False)
        )
        out = {"stdout": [], "stderr": []}
        async for kind, chunk in running.output():
            if kind in out:
                out[kind].append(chunk)
        code = await running.exit_code()
        text = b"".join(out["stdout"]).decode(errors="replace")
        err = b"".join(out["stderr"]).decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        report(case=label, created=False, err=f"{type(exc).__name__}: {exc}"[:420])
        return
    lines = text.splitlines()
    blk = (ws / "blk").exists()
    owner = None
    try:
        owner = (ws / "t.txt").stat().st_uid
    except OSError:
        pass
    report(
        case=label,
        created=code == 0,
        exit=code,
        guest_uid=lines[0] if lines else None,
        mknod_rc=next((l for l in lines if l.startswith("mknod-rc=")), None),
        blk_node_left=blk,
        file_owner=owner,
        stderr=err[:120] or None,
    )


async def main():
    Path("/tmp/rb-probe-scratch").mkdir(exist_ok=True)
    os.chmod("/tmp/rb-probe-scratch", 0o755)
    cap = ""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                eff = int(line.split()[1], 16)
                cap = ",".join(
                    n
                    for n, b in (
                        ("CHOWN", 0),
                        ("DAC_OVERRIDE", 1),
                        ("FOWNER", 3),
                        ("KILL", 5),
                        ("SETGID", 6),
                        ("SETUID", 7),
                        ("SETPCAP", 8),
                        ("SYS_ADMIN", 21),
                        ("SYS_CHROOT", 18),
                        ("SYS_PTRACE", 19),
                        ("MKNOD", 27),
                    )
                    if (eff >> b) & 1
                )
    except OSError:
        pass
    report(case="caps", euid=os.geteuid(), effective=cap)

    rootfs = resolve = None
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    rootfs = resolve_image_rootfs(
        "python:3.11-slim", str(sandbox_tmpdir(suffix="-cache", uid=UID))
    )  # 缓存目录也要能被沙箱 uid 穿过，否则 EACCES 会伪装成权限不足

    # 1) route B + chroot（形态就是线上的形态）
    ex, ws = build("python:3.11-slim", rootfs, UID, "auto")
    report(case="routeb-selected", own_identity_active=ex._own_identity_active, decline=ex._own_identity_decline)
    await run_case("routeB+chroot", ex, ws)
    ex.close()

    # 2) 进程内后端 + chroot（legacy 共享 uid 1000）：这条走 RunAs 映射路径
    ex2, ws2 = build("python:3.11-slim", rootfs, None, "off")
    await run_case("inproc+chroot(legacy uid1000)", ex2, ws2)
    ex2.close()

    # 3) 进程内后端 + 指定 per-sandbox uid（E3.2 无 route B）
    ex3, ws3 = build("python:3.11-slim", rootfs, UID, "off")
    await run_case("inproc+chroot(uid=21700)", ex3, ws3)
    ex3.close()

    # 4) 纯形态（无中介）+ route B
    ex4, ws4 = build(None, None, UID, "auto")
    await run_case("routeB+pure", ex4, ws4)
    ex4.close()


asyncio.run(main())
