#!/usr/bin/env python3
"""N57 的跨节点尺子：两个节点会不会把同一个 uid 发两次。

**这是什么**：``UidPool.acquire``（回落分配器）的临界区是"读共享记录索引 →
挑一个空闲 uid → 写预约标记"。这个探针用**两个 ``UidPool`` 实例**摆出两个节点
的形状，各 ``acquire`` 一次，看它们会不会挑中同一个 uid。

**为什么**：Task 4 把池自己的 ``.uid_pool.lock`` 与 ``.uid_reservations/`` 从共享
``E2B_STATE_BASE`` 挪到了**节点本地** ``E2B_NODE_STATE_BASE``，于是那段窗口只在
同一节点内串行 —— 两个节点可以各自读到同一份共享记录、先后挑中同一个 uid，而
在任一方把记录落盘之前谁都不知道它被挑走了（N57）。共享对象因此互相可读（卷切片、
``_snapshots`` 载荷、``<image cache>/secrets/<id>``），E3.2 的隔离墙没了。修法
（裁定 A，``12d8c43``+``bfab008``+``6b4b349``）把这两个文件挪回共享 base：锁是
跨节点互斥的串行点，标记是共享的预约账；``claim``（出厂常态路径）不碰它们，所以
放回共享盘不付 ``prepare`` 的代价。

**怎么跑**（本机，``.venv``，不连集群）::

    PYTHONPATH=. .venv/bin/python deploy/scripts/acceptance/uid_pool_cross_node_probe.py

**期望读数**：

* 修复后：``pool_a -> 10000``、``pool_b -> 10001``、``no collision`` —— 两个池同
  ``state_base``、异 ``node_state_base`` ⇒ **不碰撞**；
* 控制档：两个池同 ``node_state_base`` ⇒ 也 ``no collision``（同节点形状不回归，
  既有 ``test_acquire_across_pool_instances_does_not_collide`` 钉的是这一档）。

判据不是恒真：修复前第一档会打 ``COLLISION`` —— 控制者 2026-10-02 的原始读数是
两边都 ``10000``、标记各自落在 ``node-0-state`` / ``node-1-state`` 下。临时目录落
在项目内 ``tmp/``，不碰系统 ``/tmp``。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from envd_service.uid_pool import UidPool  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="uid-pool-cross-node-", dir=REPO / "tmp"))
# 出厂形状里共享 state base 与两个节点本地基址都存在（后者由 init container
# 建，hostPath），所以探针自己建。
(ROOT / "shared-workspaces").mkdir(parents=True)
(ROOT / "state").mkdir(parents=True)
(ROOT / "node-0-state").mkdir(parents=True)
(ROOT / "node-1-state").mkdir(parents=True)

pool_a = UidPool(
    ROOT / "shared-workspaces",
    start=10000,
    size=4,
    state_base=ROOT / "state",
    node_state_base=ROOT / "node-0-state",
)
pool_b = UidPool(
    ROOT / "shared-workspaces",
    start=10000,
    size=4,
    state_base=ROOT / "state",
    node_state_base=ROOT / "node-1-state",
)

a = pool_a.acquire("sbx_a")
b = pool_b.acquire("sbx_b")
print(f"pool_a -> {a}")
print(f"pool_b -> {b}")
print("COLLISION" if a == b else "no collision")

markers = sorted(
    str(p.relative_to(ROOT)) for p in ROOT.glob("*/.uid_reservations/*")
)
print("reservation markers:", markers)

# 反例档（判据不是恒真）：两个池共用同一个 node_state_base 时不得碰撞。
CONTROL = ROOT / "control"
shutil.rmtree(CONTROL, ignore_errors=True)
(CONTROL / "shared-workspaces").mkdir(parents=True)
(CONTROL / "state").mkdir(parents=True)
(CONTROL / "node-state").mkdir(parents=True)
ctl_a = UidPool(
    CONTROL / "shared-workspaces",
    start=10000,
    size=4,
    state_base=CONTROL / "state",
    node_state_base=CONTROL / "node-state",
)
ctl_b = UidPool(
    CONTROL / "shared-workspaces",
    start=10000,
    size=4,
    state_base=CONTROL / "state",
    node_state_base=CONTROL / "node-state",
)
ca = ctl_a.acquire("sbx_a")
cb = ctl_b.acquire("sbx_b")
print(f"control: {ca} / {cb} ->", "COLLISION" if ca == cb else "no collision")
