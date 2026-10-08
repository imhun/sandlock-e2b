#!/usr/bin/env python3
"""同一组命令在三种形态下的 (rc, stdout, stderr)，逐字节 diff。

形态：① pure + N15（E2B_PURE_ROOTFS=off，根 = 宿主 /）
      ② pure + 合成根 + E2B_REAL_ROOT=0（模拟根 + 中介）
      ③ pure + 合成根 + E2B_REAL_ROOT=1（真根）
每条差异都要能归因到"路径缺失"或"errno 变化"，归不到就是 bug。

用 `tests/security/conftest.py` 的 `own_identity_sandbox` / `run_sh`：那是本仓库把形状接到
真实 worker 形态上的唯一入口（它镜像 E2B_REAL_ROOT 与 E2B_PURE_ROOTFS），所以这个脚本
量的就是产品形态，不是另搭的一套。

退出码：0 = 普查成立（三形态都量到了，且三形态确实是三个形态）
        1 = 不成立（某个形态没量完，见 stdout 的 `MEASURE-FAIL`；量到的形态照常出 diff）
        2 = VACUOUS（形态开关没接上 ⇒ 三条曲线是同一条，任何 `diffs` 都无意义）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# `python deploy/scripts/acceptance/probe-pure-workload-census.py` puts *this* directory on
# sys.path[0], not the repo root, so `tests.…` is not importable below. The repo
# root is also the bind-mount point of the lane (`-v "$(pwd):/workspace"`), which
# is what makes the scratch path below resolve to the same file from either side.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.security.conftest import own_identity_sandbox, run_sh  # noqa: E402

COMMANDS = [
    "echo hi",
    "python3 -c 'print(1+1)'",
    "pwd && pwd -P",
    "ls /",
    "ls /usr/bin | head -3",
    "cat /etc/hosts",
    "cat /proc/version",
    "ls /proc",
    "nproc",
    "cat /etc/passwd",
    "stat -c %i /usr/bin/python3",
    "head -c 4 /dev/urandom | wc -c",
    "echo x > /dev/null && echo ok",
    "echo x > /tmp/n16-probe && cat /tmp/n16-probe",
    "ls /dev | head -3",
    # The four `/proc/self/fd` symlinks the whole-tree `/dev` bind preserves *in shape*.
    # The probe lane only ever measured the bare answer in a synthesized tree (they
    # dangle there, because the skeleton's `/proc` is an empty directory); whether they
    # resolve end-to-end is a `/proc`-synthesis question -- so it gets an accept command
    # here, in the census, instead of staying an open question in a report.
    "test -e /dev/fd; echo fd=$?",
    "test -e /dev/stdout; echo stdout=$?",
]

#: label -> (E2B_PURE_ROOTFS, E2B_REAL_ROOT, expects its own root)
SHAPES: dict[str, tuple[str, str, bool]] = {
    "n15": ("off", "0", False),
    "synth-emulated": ("synth", "0", True),
    "synth-realroot": ("synth", "1", True),
}


def scratch_dir() -> Path:
    """`<repo>/tmp/k0s/scratch/census`, taking the lane's spelling when it is real.

    The brief used ``$E2B_HOST_PROJECT``, but that variable is the *host* path
    (the container's docker CLI needs it to build bind mounts on the host
    daemon): inside the lane it names a directory that does not exist there, so
    `census.json` would be written into the container's own layer and thrown away
    with it. The repo root resolves to the same file from both sides, because
    the lane mounts the repo at ``/workspace``.
    """
    host_project = os.environ.get("E2B_HOST_PROJECT")
    if host_project and Path(host_project).is_dir():
        return Path(host_project) / "tmp/k0s/scratch/census"
    return REPO_ROOT / "tmp/k0s/scratch/census"


async def run_shape(label: str) -> tuple[dict[str, list], dict]:
    executor, workspace = own_identity_sandbox(None, None)
    shape = {
        "E2B_PURE_ROOTFS": os.environ.get("E2B_PURE_ROOTFS"),
        "E2B_REAL_ROOT": os.environ.get("E2B_REAL_ROOT"),
        "has_sandbox_root": executor._has_sandbox_root,
        "chroot_root": executor._chroot_root,
        "synthetic_rootfs": (
            str(executor._synthetic_rootfs)
            if executor._synthetic_rootfs is not None
            else None
        ),
    }
    out: dict[str, list] = {}
    try:
        for command in COMMANDS:
            code, stdout, stderr = await run_sh(executor, workspace, command)
            out[command] = [
                code,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
            ]
    finally:
        executor.close()
    return out, shape


async def main() -> int:
    base = scratch_dir()
    base.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("E2B_PURE_ROOTFS_DIR", str(base / "_pure_rootfs"))
    results: dict[str, dict] = {}
    shapes_seen: dict[str, dict] = {}
    failures: list[str] = []
    for label, (pure_rootfs, real_root, _expects_root) in SHAPES.items():
        os.environ["E2B_PURE_ROOTFS"] = pure_rootfs
        os.environ["E2B_REAL_ROOT"] = real_root
        try:
            results[label], shapes_seen[label] = await run_shape(label)
        except Exception as exc:  # a shape that cannot even be built is a failure
            failures.append(f"MEASURE-FAIL [{label}] {type(exc).__name__}: {exc}")

    vacuous: list[str] = []
    for label, (_pure_rootfs, _real_root, expects_root) in SHAPES.items():
        seen = shapes_seen.get(label)
        if seen is None:
            continue
        # The switch has to have reached the executor this helper builds: this is
        # exactly the N15-vs-N16 difference, and a lane whose `E2B_PURE_ROOTFS`
        # never lands measures the same shape three times (`diffs` would then be
        # a statement about nothing).
        if bool(seen["has_sandbox_root"]) is not expects_root:
            vacuous.append(
                f"VACUOUS [{label}] has_sandbox_root={seen['has_sandbox_root']} "
                f"expected={expects_root} (E2B_PURE_ROOTFS="
                f"{seen['E2B_PURE_ROOTFS']!r})"
            )
        if expects_root and seen["chroot_root"] == "/":
            vacuous.append(f"VACUOUS [{label}] chroot_root is still the host root")

    for line in failures:
        print(line)
    for label, seen in shapes_seen.items():
        print(
            f"shape [{label}] pure_rootfs={seen['E2B_PURE_ROOTFS']!r} "
            f"real_root={seen['E2B_REAL_ROOT']!r} "
            f"has_sandbox_root={seen['has_sandbox_root']} "
            f"chroot_root={seen['chroot_root']} "
            f"synthetic_rootfs={seen['synthetic_rootfs']}"
        )
    for line in vacuous:
        print(line)

    # A shape that could not be measured is reported above, not silently
    # dropped: the comparison still runs over the shapes that *did* answer, so a
    # blocker on one shape does not cost the classification of the others.
    diffs = 0
    if "n15" in results and not vacuous:
        reference = results["n15"]
        for label in ("synth-emulated", "synth-realroot"):
            for command, triple in results.get(label, {}).items():
                if triple != reference[command]:
                    diffs += 1
                    print(
                        f"DIFF [{label}] {command}\n  n15  = {reference[command]}\n"
                        f"  this = {triple}"
                    )
    print(
        f"measured={','.join(sorted(results))} "
        f"failed={','.join(sorted(set(SHAPES) - set(results))) or '-'} "
        f"commands={len(COMMANDS)} shapes={len(SHAPES)} diffs={diffs}"
    )

    (base / "census.json").write_text(
        json.dumps(
            {"shapes": shapes_seen, "results": results, "diffs": diffs},
            indent=2,
        ),
        encoding="utf-8",
    )
    if vacuous:
        return 2
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
