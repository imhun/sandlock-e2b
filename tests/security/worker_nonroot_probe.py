"""E5.1 non-root worker in-container probe.

Runs inside the built worker image (``deploy/docker/Dockerfile.envd``) as its
default USER (uid 65534) with the repository mounted read-only. Verifies the
supervisor identity and the three sandlock execution paths that must stay
green without root:

* plain sandbox creation + execution (no image rootfs);
* network-enabled sandbox creation (network policy plumbing);
* image-rootfs (chroot) sandbox creation + execution.

Usage: ``python worker_nonroot_probe.py <expected_uid> <workspace_root>
<rootfs>``. Exit code 0 only when every probe passes; each failure is
reported with exact evidence on stderr.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor

EXPECTED_UID = int(sys.argv[1])
WORKSPACE_ROOT = Path(sys.argv[2])
ROOTFS = Path(sys.argv[3])


def _executor(workspace: str, **kwargs) -> SandlockExecutor:
    return SandlockExecutor(
        workspace_dir=workspace,
        base_image=kwargs.get("base_image"),
        image_rootfs=kwargs.get("image_rootfs"),
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=kwargs.get("allow_internet_access", False),
        enable_network=kwargs.get("enable_network", False),
        network=kwargs.get("network"),
        notify_rate_limit=0,
    )


def _run(executor: SandlockExecutor, cmd: list[str]):
    return executor._build_sandbox(
        ExecConfig(
            cmd=cmd,
            env={},
            cwd=executor._workspace_dir,
            stdin_enabled=False,
        )
    ).run(cmd)


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        print(f"FAIL {name}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    print(f"PASS {name}")


def main() -> int:
    import sandlock

    _check(
        "supervisor euid",
        os.geteuid() == EXPECTED_UID,
        f"euid={os.geteuid()} expected={EXPECTED_UID}",
    )
    _check(
        "landlock abi",
        sandlock.landlock_abi_version() >= 6,
        f"abi={sandlock.landlock_abi_version()}",
    )

    base = WORKSPACE_ROOT / "probe"
    base.mkdir(parents=True, exist_ok=True)

    plain_ws = base / "plain"
    plain_ws.mkdir(exist_ok=True)
    result = _run(
        _executor(str(plain_ws)),
        ["/bin/echo", "plain-ok"],
    )
    _check(
        "plain sandbox",
        result.exit_code == 0 and result.stdout == b"plain-ok\n",
        f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    net_ws = base / "network"
    net_ws.mkdir(exist_ok=True)
    result = _run(
        _executor(
            str(net_ws),
            enable_network=True,
            allow_internet_access=True,
            network={"denyOut": ["10.0.0.0/8"]},
        ),
        ["/bin/echo", "net-ok"],
    )
    _check(
        "network sandbox",
        result.exit_code == 0 and result.stdout == b"net-ok\n",
        f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}",
    )

    rootfs_ws = base / "rootfs"
    rootfs_ws.mkdir(exist_ok=True)
    result = _run(
        _executor(
            str(rootfs_ws),
            base_image="probe-image",
            image_rootfs=ROOTFS,
        ),
        ["/bin/echo", "rootfs-ok"],
    )
    _check(
        "rootfs sandbox",
        result.exit_code == 0 and result.stdout == b"rootfs-ok\n",
        f"exit={result.exit_code} stdout={result.stdout!r} stderr={result.stderr!r}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
