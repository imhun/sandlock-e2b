#!/usr/bin/env python3
"""恢复**指定 id** 的快照，检查里面的文件还在不在 —— 迁移之后的那一问。

`snapshot_create_probe.py` 证明的是"新写进去的快照能读回来"。这一条问的是另一件事：
一个**已经被搬动过**的快照（N58 把载荷从 `<workspaces>/_snapshots/<id>/` 合到
`<export>/_snapshots/<id>/`，同一份 inode）在新代码的路径推导下还找不找得到、解不解
得开。它也是"从快照建箱"这条路的端到端钉子 —— 控制面的 `copy_from` 一度还指向树根
下的老位置，而那种错只有真的建一个箱才会响（`502 partial-copy: the snapshot source
… is not a directory`，2026-10-02 实测）。

只恢复、不新建快照，所以对一个只读的历史 id 也成立：

    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/restore_snapshot_probe.py \
        --snapshot snap_46dc467759dbbfb7 --snapshot snap_ce90ef9852fc6809

每个 id 建一个箱、读 `--path`（默认 `workspace/kept.txt`，即生产形状里"文件在树根下的
`workspace/`"）、确认 `workspace/workspace/<name>` **不存在**（v1 的那个坑），然后
杀掉那个箱。快照本身不动。
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--snapshot",
        action="append",
        required=True,
        help="恢复哪个快照 id；可重复",
    )
    parser.add_argument("--path", default="workspace/kept.txt")
    parser.add_argument("--expect", default="kept\n")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    from e2b import Sandbox

    deep_path = "/".join(["workspace", args.path])
    failures = 0
    for snapshot_id in args.snapshot:
        created = None
        try:
            created = Sandbox.create(snapshot_id, timeout=args.timeout)
            got = created.files.read(args.path)
            try:
                created.files.read(deep_path)
                deep = "PRESENT (the v1 landmine)"
            except Exception as exc:  # noqa: BLE001 - the absence is the assertion
                deep = type(exc).__name__
            print(f"{snapshot_id}: {args.path}={got!r} {deep_path}={deep}", flush=True)
            if got != args.expect:
                print(f"FAIL: {snapshot_id}: {args.path} came back as {got!r}")
                failures += 1
            if "PRESENT" in deep:
                print(f"FAIL: {snapshot_id}: the snapshot landed one level too deep")
                failures += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"FAIL: {snapshot_id}: {type(exc).__name__}: {exc}")
            failures += 1
        finally:
            if created is not None:
                try:
                    created.kill()
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask
                    print(f"warning: could not kill the box made from {snapshot_id}: {exc}")

    print(
        "RESULT restored=%d failures=%d" % (len(args.snapshot), failures)
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
