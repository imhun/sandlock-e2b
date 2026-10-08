"""Mechanism probe: why does the chroot volume view fail without SYS_ADMIN?

Backlog #25 says the shared-volume view degrades to the workspace symlink
when `mount --bind` is unavailable, and that the degraded path is broken
(EACCES) for cross-uid access. This probe isolates *which* step breaks:

  A. resolution of the sandbox-visible path (`/workspace/mnt/data` -> host)
  B. DAC on the host-side volume path as the sandbox's own uid
  C. the physical workspace entry (symlink vs real dir vs nothing)

Runs the same command against several workspace layouts and reports the
per-step exit codes. Read-only with respect to the repo; scratch under tmp.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
from envd_service.executors.base import ExecConfig  # noqa: E402
from envd_service.executors.sandlock import SandlockExecutor  # noqa: E402
from envd_service.own_identity import OwnIdentityConfig  # noqa: E402
from envd_service.runtime.image_resolver import resolve_image_rootfs  # noqa: E402
from tests.security.conftest import SANDBOX_UID, make_sandbox_visible  # noqa: E402

UID = 21700
SCRATCH = Path("/tmp/vol-probe-scratch")

# Every step is a separate exit code so a failure names its own stage.
CMD = (
    "id -u; "
    "echo --- cwd1; pwd; readlink /proc/self/cwd; "
    "echo --- cwd2; cd /workspace; pwd; readlink /proc/self/cwd; "
    "echo --- rel-read; cat mnt/data/data.txt; echo rel-read=$?; "
    "echo --- proccwd-read; cat /proc/self/cwd/mnt/data/data.txt; echo proccwd-read=$?; "
    "echo --- rel-write; echo new > mnt/data/new.txt; echo rel-write=$?; "
    "echo --- abs-read; cat /workspace/mnt/data/data.txt; echo abs-read=$?; "
    "echo --- dirfd; cat mnt/data/../data/data.txt; echo dirfd-read=$?; "
    "echo --- list; ls -a mnt; echo list=$?; "
    "echo --- stat; test -f mnt/data/data.txt; echo stat=$?"
)


def report(**kw):
    print(json.dumps(kw, ensure_ascii=False, sort_keys=True), flush=True)


def caps() -> str:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                eff = int(line.split()[1], 16)
                names = (
                    ("CHOWN", 0), ("DAC_OVERRIDE", 1), ("FOWNER", 3), ("KILL", 5),
                    ("SETGID", 6), ("SETUID", 7), ("SETPCAP", 8), ("SYS_ADMIN", 21),
                    ("SYS_CHROOT", 18), ("SYS_PTRACE", 19), ("MKNOD", 27),
                )
                return ",".join(n for n, b in names if (eff >> b) & 1)
    except OSError:
        pass
    return "?"


def build_workspace(layout: str, root: Path, vol: Path, name: str | None = None) -> Path:
    """Workspace owned by UID, with the volume entry shaped per `layout`."""
    ws = root / f"ws-{name or layout}"
    (ws / "mnt").mkdir(parents=True, exist_ok=True)
    make_sandbox_visible(ws)
    if layout == "symlink":
        (ws / "mnt" / "data").symlink_to(vol, target_is_directory=True)
    elif layout == "placeholder":
        (ws / "mnt" / "data").mkdir(exist_ok=True)
    elif layout == "none":
        pass
    else:
        raise ValueError(layout)
    if os.geteuid() == 0:
        os.chown(ws, UID, UID)
        for p in (ws / "mnt",):
            os.chown(p, UID, UID)
    os.chmod(ws, 0o700)
    return ws


async def run_case(
    label: str, ws: Path, vol: Path, rootfs: Path, *, perms: str, alias: bool = False
) -> None:
    mounts = {"/workspace/mnt/data": str(vol)}
    if alias:
        # Candidate fix: register the same host view under the second workspace
        # alias too, so a cwd-derived path finds the sub-mount either way.
        mounts["/home/user/mnt/data"] = str(vol)
    ex = SandlockExecutor(
        workspace_dir=str(ws),
        base_image="python:3.11-slim",
        image_rootfs=rootfs,
        host_uid=UID,
        per_sandbox_uid=True,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=f"sbx_volprobe_{label}",
        extra_fs_writable=[str(vol)],
        fs_mounts=mounts,
        own_identity=OwnIdentityConfig(mode="auto", uid_start=UID, uid_size=2, tmp_root=SCRATCH),
    )
    try:
        running = await ex.start(
            ExecConfig(cmd=["/bin/sh", "-c", CMD], env={}, cwd=str(ws), stdin_enabled=False)
        )
        out = {"stdout": [], "stderr": []}
        async for kind, chunk in running.output():
            if kind in out:
                out[kind].append(chunk)
        code = await running.exit_code()
        text = b"".join(out["stdout"]).decode(errors="replace")
        err = b"".join(out["stderr"]).decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        report(case=label, perms=perms, created=False,
               err=f"{type(exc).__name__}: {exc}"[:400])
        ex.close()
        return
    finally:
        pass
    steps = {}
    for line in text.splitlines():
        if line.startswith("/") or line in ("/workspace",):
            steps.setdefault("cwd-lines", []).append(line) if isinstance(steps.get("cwd-lines"), list) else None
        if "=" in line and not line.startswith("---"):
            k, _, v = line.partition("=")
            steps[k.strip()] = v.strip()
    report(
        case=label,
        perms=perms,
        own_identity_active=ex._own_identity_active,
        alias=alias,
        exit=code,
        steps=steps,
        stdout_head=text.splitlines()[:8],
        stderr=err.strip()[:200] or None,
    )
    ex.close()


async def main() -> None:
    report(caps=caps(), euid=os.geteuid())
    root = Path("/tmp/vol-probe")
    for d in (root, SCRATCH):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o755)

    vol = root / "vol"
    vol.mkdir(parents=True, exist_ok=True)
    (vol / "data.txt").write_text("hello\n")
    os.chmod(vol / "data.txt", 0o644)
    os.chmod(vol, 0o1777)
    make_sandbox_visible(vol)

    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    make_sandbox_visible(cache)
    rootfs = resolve_image_rootfs("python:3.11-slim", str(cache))
    report(rootfs=str(rootfs))

    for layout in ("symlink", "placeholder", "none"):
        ws = build_workspace(layout, root, vol)
        await run_case(f"chroot+fs_mount/{layout}", ws, vol, rootfs, perms="0755")
        if layout == "symlink":
            ws_alias = build_workspace("symlink", root, vol, name="symlink-alias")
            await run_case(
                "chroot+fs_mount/symlink+home-alias",
                ws_alias,
                vol,
                rootfs,
                perms="0755",
                alias=True,
            )

    # Same, but the volume's ancestors are 0700 (traverse denied to the uid).
    tight = root / "tight"
    (tight / "vol").mkdir(parents=True, exist_ok=True)
    (tight / "vol" / "data.txt").write_text("hello\n")
    os.chmod(tight / "vol", 0o1777)
    make_sandbox_visible(tight)
    os.chmod(tight, 0o700)
    ws_tight = build_workspace("symlink", root / "tight-ws", tight / "vol")
    await run_case("chroot+fs_mount/symlink-tight-ancestor", ws_tight, tight / "vol",
                   rootfs, perms="0700")

    report(case="done", uid=SANDBOX_UID)


asyncio.run(main())
