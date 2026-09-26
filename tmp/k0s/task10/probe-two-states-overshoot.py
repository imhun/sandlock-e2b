#!/usr/bin/env python3
"""越界路径在**两个合法形态**下的 (rc, stdout, stderr) 三元组。

口径（2026-09-26 裁定，`docs/superpowers/plans/2026-09-26-decisions.md`
「合成根 + 模拟根结构性不成立 ⇒ 配置守卫」）：纯形态的两态是

  ① identity            `E2B_PURE_ROOTFS` 不设 + `E2B_REAL_ROOT=0`（N15，根 = 宿主 /）
  ② synth-realroot      `E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT=1`（N16，合成根 + bind）

`synth` + `E2B_REAL_ROOT=0` 不在表里：那个组合生成期就死（Task 9 的 32-error 日志），
E2B 侧也已被配置守卫当场拒（d1c4922），不是可验收的形态。

量的是"平台状态 / 授权外路径"这一组：越界回答从 EACCES 变成 ENOENT，也就是
Task 11 的契约迁移要逐条接手的那份差异，以及新增用例
`test_the_synthetic_root_hides_the_platform_state` 的现场底稿。

用 `tests/security/conftest.py` 的 `route_b_sandbox` / `run_sh`：那是把形状接到真实
worker 形态上的唯一入口（镜像 `E2B_PURE_ROOTFS` / `E2B_REAL_ROOT`），所以这里量的
是产品形态，不是另搭的一套。

退出码：0 = 两态都量到、且自证是两个不同形态
        1 = 不成立（某形态没量完，见 `MEASURE-FAIL`）
        2 = VACUOUS（开关没落进 executor ⇒ 两条曲线是同一条，任何 diff 都无意义）
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

# This file lives one level deeper than the other `tmp/k0s/*.py` probes
# (`tmp/k0s/task10/`), so the repo root is parents[3] -- and the repo root is
# also the lane's bind mount point, which is what makes the scratch path below
# resolve to the same file from either side.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.security.conftest import (  # noqa: E402
    SANDBOX_UID,
    make_sandbox_visible,
    route_b_sandbox,
    run_sh,
)

#: label -> (E2B_PURE_ROOTFS, E2B_REAL_ROOT, expects its own root)
SHAPES: dict[str, tuple[str, str, bool]] = {
    "identity": ("off", "0", False),
    "synth-realroot": ("synth", "1", True),
}

#: cwd-relative wherever the answer differs between the two states (`../..` is the
#: export directory in the identity shape and the synthesized `/` in the other one),
#: absolute for the "still denied even with a root" control.
COMMANDS = [
    "pwd",
    # Control: an out-of-tree kernel path that stays denied in *both* states.
    "cat /proc/kcore",
    # N16's `_SYNTHETIC_ROOTFS_SYSTEM_DIRS` deliberately has no `/sys`.
    "ls /sys",
    # The N27 residue: in identity `..`/`../..` still hand out the platform
    # state's *names*; with a synthesized root the base is not in the tree at all.
    "ls ../..",
    "stat ../../state",
    "cat ../../state/sbx_probe/secret",
    # The uid-isolation case's shape (another sandbox's workspace, a sibling).
    "stat ../../workspaces/sbx_other",
]

# The tree has to live on the same kind of storage the suite's own `workspace`
# fixture uses: `E2B_TEST_TMP_ROOT` when the lane sets it (container-native,
# where chown is real), and the repo's `tmp/` on a Linux dev box. A tree under
# the repo *bind mount* is not merely a chown no-op -- the identity leg could not
# even `chdir` into it (`sandlock-init: chdir … failed (errno 2)`), which measures
# the bind mount rather than the shape. The JSON goes to the repo, which the lane
# does mount.
TMP_ROOT = Path(os.environ.get("E2B_TEST_TMP_ROOT") or REPO_ROOT / "tmp/test-runtime")
SCRATCH = TMP_ROOT / "task10-overshoot"
EVIDENCE = REPO_ROOT / "tmp/k0s/task10/two-states-overshoot.json"


def layout() -> tuple[Path, Path]:
    """`<export>/workspaces/<id>` + a platform state tree beside it, N27's shape.

    Rebuilt every run: the synthesized root only creates mount points and never
    cleans up leftovers (Task 9 self-review §6.4), so measuring against a tree
    from a previous round is not measuring this one. ``<export>`` is 0755 -- the
    sandbox uid has to be able to walk in, or the answer being measured is
    "cannot get in at all" rather than "how does an out-of-tree path answer".
    """
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    export = SCRATCH / "export"
    workspace = export / "workspaces" / "sbx_probe"
    workspace.mkdir(parents=True)
    (export / "state" / "sbx_probe").mkdir(parents=True)
    (export / "state" / "sbx_probe" / "secret").write_text("platform\n", encoding="utf-8")
    (export / "workspaces" / "sbx_other").mkdir()
    (export / "workspaces" / "sbx_other" / "secret.txt").write_text("other\n", encoding="utf-8")
    make_sandbox_visible(export)
    for path in (export, export / "workspaces", workspace):
        os.chmod(path, 0o755)
    if os.geteuid() == 0:
        for path in (export, export / "workspaces", workspace):
            os.chown(path, SANDBOX_UID, SANDBOX_UID)
    return export, workspace


async def run_state(label: str, workspace: Path) -> tuple[dict[str, list], dict]:
    executor, ws = route_b_sandbox(None, None, workspace=workspace)
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
            code, stdout, stderr = await run_sh(executor, ws, command)
            out[command] = [
                code,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
            ]
    finally:
        executor.close()
    return out, shape


async def main() -> int:
    export, workspace = layout()
    results: dict[str, dict] = {}
    shapes_seen: dict[str, dict] = {}
    failures: list[str] = []
    for label, (pure_rootfs, real_root, _expects) in SHAPES.items():
        os.environ["E2B_PURE_ROOTFS"] = pure_rootfs
        os.environ["E2B_REAL_ROOT"] = real_root
        try:
            results[label], shapes_seen[label] = await run_state(label, workspace)
        except Exception as exc:  # a state that cannot be built is a failure
            failures.append(f"MEASURE-FAIL [{label}] {type(exc).__name__}: {exc}")

    vacuous: list[str] = []
    for label, (_pure, _real, expects_root) in SHAPES.items():
        seen = shapes_seen.get(label)
        if seen is None:
            continue
        if bool(seen["has_sandbox_root"]) is not expects_root:
            vacuous.append(
                f"VACUOUS [{label}] has_sandbox_root={seen['has_sandbox_root']} "
                f"expected={expects_root} (E2B_PURE_ROOTFS={seen['E2B_PURE_ROOTFS']!r})"
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

    print(f"\nexport={export}\n")
    for command in COMMANDS:
        for label in SHAPES:
            triple = results.get(label, {}).get(command)
            if triple is not None:
                print(f"{label:<15} {command:<34} = {triple}")
        print()

    diffs = 0
    if len(results) == len(SHAPES) and not vacuous:
        reference = results["identity"]
        for command in COMMANDS:
            if results["synth-realroot"][command] != reference[command]:
                diffs += 1
    print(
        f"measured={','.join(sorted(results))} "
        f"failed={','.join(sorted(set(SHAPES) - set(results))) or '-'} "
        f"commands={len(COMMANDS)} states={len(SHAPES)} diffs={diffs}"
    )

    EVIDENCE.write_text(
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
