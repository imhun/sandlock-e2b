#!/usr/bin/env python3
"""恢复**指定 id** 的快照，检查里面某个文件还在不在 —— 迁移之后的那一问。

`snapshot_create_probe.py` 证明的是"新写进去的快照能读回来"。这一条问的是另一件事：
一个**已经被搬动过**的快照（N58 把载荷从 `<workspaces>/_snapshots/<id>/` 合到
`<export>/_snapshots/<id>/`，同一份 inode）在新代码的路径推导下还找不找得到、解不解
得开。它也是"从快照建箱"这条路的端到端钉子 —— 控制面的 `copy_from` 一度还指向树根
下的老位置，而那种错只有真的建一个箱才会响：

    502: the agent for node e2b-worker-0 refused the materialization: partial-copy:
    the snapshot source /var/lib/e2b-sandboxes/workspaces/_snapshots/snap_…/fs
    is not a directory      （2026-10-02，N58 上线当场）

每条检查写成 `ID:PATH` 或 `ID:PATH:EXPECT`：前者只要求文件读得到（并打印大小与
sha256 头一段），后者还要求内容与 `EXPECT` 逐字相同（`EXPECT` 里可以写 `\\n`）。
每个 id 建一个箱、读那个文件、确认 `workspace/workspace/<name>` **不存在**（v1 把快照
多下沉一层的那个坑），然后杀掉那个箱。快照本身不动：

    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/restore_snapshot_probe.py \
        --check 'snap_46dc467759dbbfb7:workspace/kept.txt:kept\\n' \
        --check 'snap_ce90ef9852fc6809:lease/f0000.bin'

**每条检查跑在自己的进程里。** 不是为了隔离失败，是为了连接：一次复制 2000 条目要
70 秒上下，而 SDK 的 HTTP 连接是复用的 —— 长请求之后紧接着的第二次 `create` 会拿到
一个**空 body**（`json.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`），
单独重跑同一条却绿（2026-10-02 实测两次）。换进程就是换连接；"重试到绿"会把真正的
空响应一起藏掉。

⚠ 只有**同时有记录和载荷**的快照能这样建箱：记录是控制面在快照成功之后写的，所以
"载荷在、记录不在"的那些 id（线上有 `snap_2bb1…`、`snap_a6e1…`）会被注册表拒成
`400: Template <id> not found` —— 那是数据问题，不是这条路坏了。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys

ONE = "--_one"


def _parse_check(raw: str) -> tuple[str, str, str | None]:
    parts = raw.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise argparse.ArgumentTypeError(
            f"--check 要写成 ID:PATH 或 ID:PATH:EXPECT，收到 {raw!r}"
        )
    snapshot_id, path = parts[0], parts[1]
    expect = None
    if len(parts) == 3:
        # 命令行里写的是字面的 ``\n``；解成真换行之后按字节比较，避免"看着一样"。
        expect = parts[2].encode("utf-8").decode("unicode_escape")
    return snapshot_id, path, expect


def _one_check(snapshot_id: str, path: str, expect: str | None, timeout: int) -> int:
    from e2b import Sandbox

    created = None
    try:
        created = Sandbox.create(snapshot_id, timeout=timeout)
        got = created.files.read(path)
        digest = hashlib.sha256(got.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        deep_path = "workspace/" + path
        try:
            created.files.read(deep_path)
            deep = "PRESENT (the v1 landmine)"
        except Exception as exc:  # noqa: BLE001 - the absence is the assertion
            deep = type(exc).__name__
        print(
            f"{snapshot_id}: {path} bytes={len(got)} sha256={digest} {deep_path}={deep}",
            flush=True,
        )
        if expect is not None and got != expect:
            print(f"FAIL: {snapshot_id}: {path} came back as {got!r}, wanted {expect!r}")
            return 1
        if "PRESENT" in deep:
            print(f"FAIL: {snapshot_id}: the snapshot landed one level too deep")
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        print(f"FAIL: {snapshot_id}: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if created is not None:
            try:
                created.kill()
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask
                print(f"warning: could not kill the box made from {snapshot_id}: {exc}")


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == [ONE]:
        # 子进程那一半：一条检查，一个没被长请求用过的连接。
        child = argparse.ArgumentParser(prog="restore_snapshot_probe --_one")
        child.add_argument(ONE, dest="raw")
        child.add_argument("--timeout", type=int, default=600)
        child_args = child.parse_args(argv)
        snapshot_id, path, expect = _parse_check(child_args.raw)
        return _one_check(snapshot_id, path, expect, child_args.timeout)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="append",
        required=True,
        metavar="ID:PATH[:EXPECT]",
        help="恢复哪个快照、读哪个文件、期望什么内容；可重复（每条一个进程）",
    )
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    for raw in args.check:
        _parse_check(raw)  # 父进程先校验一遍：拼错立刻报，不用等子进程

    failures = 0
    for raw in args.check:
        proc = subprocess.run(
            [sys.executable, __file__, ONE, raw, "--timeout", str(args.timeout)]
        )
        failures += 1 if proc.returncode else 0
    print("RESULT restored=%d failures=%d" % (len(args.check), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
