#!/usr/bin/env python3
"""N58 迁移的**本机彩排**：把 N27 之后、N58 之前的形状手工搭出来，跑一遍迁移。

`deploy/scripts/migrate-state-base.sh --root DIR` 是同一个引擎的离线入口；这个脚本
只负责**造那份形状**并把它跑成一个前后对照，因为它要证明的四件事不靠集群：

1. dry-run 一个字节都不写；
2. apply 之后每一条都是 `rename(2)`（inode 不变，逐条 `VERIFY … same_inode=yes`）；
3. 再跑一遍是"没有可搬的条目"而不是猜（`REFUSE(3)` 具名拒绝）；
4. `--rollback --apply` 把两个命名空间按 journal 原路退回。

形状（`docs/create-local-first-layout.md` §2.1 的线上实测）：

```
<export>/_snapshots/snap_aaa/snapshot.json              控制面的记录
<export>/workspaces/_snapshots/snap_aaa/{.complete,fs}  agent 的载荷（要合一）
<export>/workspaces/_snapshots/snap_bbb/{.complete,fs}  只有载荷（整条 rename）
<export>/workspaces/_migrate/                           迁移暂存（要上浮）
<export>/workspaces/sbx_aaa/…                           一棵沙箱树（不许被碰）
<export>/state/_runtime/sbx_aaa/…                       平台状态（不许被触碰）
```

    python3 deploy/scripts/acceptance/migrate_state_base_rehearsal.py

它不连集群、不读 `KUBECONFIG`，工作目录建在 `tmp/` 下并自己清理。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = "deploy/scripts/migrate-state-base.sh"
STAY = ("_builds", "_images", "_secrets", "_templates", "_snapshots", "_volumes")


def build(root: pathlib.Path) -> None:
    """N27 之后、N58 之前的 export 根（线上那份的缩小版）。"""
    root.mkdir(parents=True)
    for name in STAY:
        (root / name).mkdir()
    (root / "_volumes" / "vol_1").mkdir()
    (root / "_volumes" / "vol_1" / "data.bin").write_bytes(b"volume-data")
    record = root / "state" / "_runtime" / "sbx_aaa"
    record.mkdir(parents=True)
    (record / "sandbox.json").write_text('{"sandbox_id": "sbx_aaa"}\n')
    (root / "state" / ".route-b" / "10000").mkdir(parents=True)
    (root / "workspaces" / "sbx_aaa" / "workspace").mkdir(parents=True)
    (root / "workspaces" / "sbx_aaa" / "workspace" / "hello.txt").write_text("hi\n")
    (root / "workspaces" / "_migrate").mkdir()
    (root / "workspaces" / "_migrate").chmod(0o1777)
    # 有记录、也有载荷的那个 id：逐条合一。
    both = root / "workspaces" / "_snapshots" / "snap_aaa"
    (both / "fs" / "workspace").mkdir(parents=True)
    (both / "fs" / "workspace" / "kept.txt").write_text("kept\n")
    (both / "fs" / "sandbox.json").write_text('{"sandbox_id": "snap_aaa"}\n')
    (both / ".complete").write_text("")
    both.chmod(0o755)
    both.parent.chmod(0o755)
    (root / "_snapshots" / "snap_aaa").mkdir()
    (root / "_snapshots" / "snap_aaa" / "snapshot.json").write_text(
        '{"snapshot_id": "snap_aaa"}\n'
    )
    # 只有载荷的那个 id：整条 id 目录一次 rename。
    only = root / "workspaces" / "_snapshots" / "snap_bbb"
    (only / "fs").mkdir(parents=True)
    (only / ".complete").write_text("")
    (only / "fs" / "sandbox.json").write_text('{"sandbox_id": "snap_bbb"}\n')
    only.chmod(0o755)
    only.parent.chmod(0o755)


def run(root: pathlib.Path, *args: str) -> int:
    env = dict(os.environ)
    # 彩排不许碰集群：把 KUBECONFIG 摘掉，脚本自己就会走 `--root` 那条路。
    env.pop("KUBECONFIG", None)
    proc = subprocess.run(
        ["bash", SCRIPT, "--root", str(root), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
    )
    print("$ %s --root <root> %s   => rc=%d" % (SCRIPT, " ".join(args), proc.returncode))
    print(proc.stdout, end="")
    if proc.stderr:
        print("stderr: %s" % proc.stderr, end="")
    return proc.returncode


def main() -> int:
    scratch = REPO / "tmp" / "migrate-state-base-rehearsal"
    scratch.mkdir(parents=True, exist_ok=True)
    failures = 0
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        root = pathlib.Path(tmp) / "export"
        build(root)
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))

        if run(root) != 0:  # dry-run
            failures += 1
        after_dry = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        if after_dry != before:
            print("FAIL: the dry run wrote something")
            failures += 1

        if run(root, "--apply") != 0:  # 迁移
            failures += 1
        merged = root / "_snapshots" / "snap_aaa" / "fs" / "workspace" / "kept.txt"
        if not merged.is_file() or merged.read_text() != "kept\n":
            print("FAIL: the merged payload is not where the record is")
            failures += 1
        if (root / "workspaces" / "_snapshots").exists():
            print("FAIL: the emptied tree-root namespace is still there")
            failures += 1
        if not (root / "_migrate").is_dir():
            print("FAIL: the staging directory did not move up")
            failures += 1

        if run(root, "--apply") != 3:  # 再跑一遍必须是具名拒绝
            print("FAIL: a second apply did not refuse by name (expected rc=3)")
            failures += 1

        if run(root, "--apply", "--rollback") != 0:
            failures += 1
        rolled = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        expected = sorted(before + [".state-base-migration.journal.rolled-back"])
        if rolled != expected:
            print("FAIL: the rollback did not restore the original shape")
            print("  missing: %s" % sorted(set(expected) - set(rolled)))
            print("  extra:   %s" % sorted(set(rolled) - set(expected)))
            failures += 1

    print("RESULT rehearsal failures=%d" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
