"""Which namespaces and caps does a chroot-shaped sandbox actually have today?

This is the first thing the N14 question ("real root: mount ns + pivot_root
instead of the virtual root") needs. A real root has to be *made* inside a
mount namespace, and making one needs either CAP_SYS_ADMIN in the host
namespace (the production manifests dropped it) or an unprivileged user
namespace with CAP_SYS_ADMIN inside it. So: does today's sandbox already have
a user namespace, a mount namespace, and which caps?

Measured from the *outside* (/proc/<pid>/ns/* of the live sandbox processes),
because the guest's own /proc is synthesized by the mediator -- asking the guest
would measure the platform's claims, not the kernel's state.

Usage (inside the lane container):

    python3 -u tmp/k0s/probe_n35_ns.py [chroot|pure]
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from tests.security.conftest import (
    SANDBOX_UID,
    require_mediation_capable,
    resolve_test_rootfs,
    sandbox_tmpdir,
)

NS_KINDS = ("mnt", "user", "pid", "pid_for_children", "net", "ipc", "uts", "cgroup")


def build_executor(shape: str):
    from envd_service.executors.sandlock import SandlockExecutor
    from envd_service.route_b import RouteBConfig

    workspace = sandbox_tmpdir(suffix="-ws")
    chroot = shape == "chroot"
    image = "python:3.11-slim" if chroot else None
    rootfs = resolve_test_rootfs(image) if chroot else None
    host_uid = SANDBOX_UID if os.geteuid() == 0 else None
    # Same env knobs the executor's Settings read, so this probe can measure
    # either shape.
    real_root = os.environ.get("E2B_REAL_ROOT", "0").strip() == "1"
    pid_ns = os.environ.get("E2B_PID_NS", "0").strip() == "1"
    executor = SandlockExecutor(
        workspace_dir=str(workspace),
        base_image=image,
        image_rootfs=rootfs,
        host_uid=host_uid,
        per_sandbox_uid=True,
        real_root=real_root,
        pid_ns=pid_ns,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=f"sbx_ns_{uuid.uuid4().hex[:8]}",
        route_b=RouteBConfig(
            mode="auto",
            uid_start=host_uid if host_uid is not None else SANDBOX_UID,
            uid_size=2,
            tmp_root=sandbox_tmpdir(suffix="-route-b"),
        ),
    )
    return executor, workspace


def status_field(pid: str, field: str) -> str:
    try:
        text = Path("/proc/" + pid + "/status").read_text()
    except OSError as exc:
        return "<" + str(exc.strerror) + ">"
    for line in text.splitlines():
        if line.startswith(field):
            return line.split(":", 1)[1].strip()
    return "<absent>"


def describe(pid: str, label: str) -> None:
    roots = []
    for which in ("root", "cwd", "exe"):
        try:
            roots.append(which + "=" + os.readlink("/proc/" + pid + "/" + which))
        except OSError as exc:
            roots.append(which + "=<" + str(exc.strerror) + ">")
    print("--- " + label + " pid=" + pid)
    print("    " + " ".join(roots))
    links = []
    for kind in NS_KINDS:
        try:
            links.append(kind + "=" + os.readlink("/proc/" + pid + "/ns/" + kind))
        except OSError as exc:
            links.append(kind + "=<" + str(exc.strerror) + ">")
    cmd = "<gone>"
    try:
        cmd = Path("/proc/" + pid + "/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        pass
    print("--- " + label + " pid=" + pid)
    print("    cmd: " + cmd.replace("\0", " ").strip()[:170])
    print("    " + " ".join(links))
    fds = []
    fd_dir = Path("/proc/" + pid + "/fd")
    try:
        for entry in sorted(fd_dir.iterdir(), key=lambda e: int(e.name)):
            try:
                fds.append(f"{entry.name}->{os.readlink(entry)}")
            except OSError:
                pass
    except OSError:
        pass
    print("    fds: " + ", ".join(fds[:12]))
    print(
        "    CapEff=" + status_field(pid, "CapEff")
        + " Uid=" + status_field(pid, "Uid")
        + " NNP=" + status_field(pid, "NoNewPrivs")
        + " Seccomp=" + status_field(pid, "Seccomp")
        + " Landlock=" + status_field(pid, "Landlock")
    )
    sys.stdout.flush()


async def main_async() -> int:
    from envd_service.executors.base import ExecConfig

    shape = sys.argv[1] if len(sys.argv) > 1 else "chroot"
    executor, workspace = build_executor(shape)
    token = executor._sandbox_id
    try:
        require_mediation_capable(executor)
        print(
            "== shape=" + shape + " route_b_active=" + str(executor._route_b_active)
            + " real_root=" + str(getattr(executor, "_real_root", False))
            + " id=" + token
        )
        sys.stdout.flush()
        running = await executor.start(
            ExecConfig(
                cmd=["/bin/sh", "-c", "sleep 15"],
                env={},
                cwd=str(workspace),
                stdin_enabled=False,
            )
        )
        await asyncio.sleep(2)
        describe(str(os.getpid()), "lane container (baseline)")
        describe("1", "container init")
        found = []
        for entry in sorted(Path("/proc").glob("[0-9]*"), key=lambda p: int(p.name)):
            try:
                cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
            except OSError:
                continue
            if token in cmdline:
                found.append(entry.name)
        print("== pids carrying " + token + ": " + str(found))
        own = str(os.getpid())
        for entry in sorted(Path("/proc").glob("[0-9]*"), key=lambda p: int(p.name)):
            if status_field(entry.name, "PPid") == own:
                describe(entry.name, "direct child of the probe (in-process shape)")
        for pid in found:
            describe(pid, "mediator (carries the policy path)")
            for candidate in sorted(Path("/proc").glob("[0-9]*"), key=lambda p: int(p.name)):
                if status_field(candidate.name, "PPid") == pid:
                    describe(candidate.name, "child of mediator")
        try:
            await running.kill()
        except Exception:
            pass
        return 0
    finally:
        executor.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
