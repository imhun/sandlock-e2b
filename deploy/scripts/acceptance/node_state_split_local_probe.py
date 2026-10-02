#!/usr/bin/env python3
"""Task 4 的**本地**尺子：``prepare`` 在共享 base 上还剩几次路径操作。

Task 4 的机制论断是"``prepare`` 的长杆全是共享卷上的元数据往返"（Task 1 §1.1：
共享 NAS 上一次 ``open(create)``/``mkdir`` ≈ 13 ms，节点本地盘 ≈ 0.03 ms）。这个
探针不连集群、不改部署，把那个**机制**直接数出来：在真 app（``executor='local'``）
里跑一次 ``phase: prepare``，用一个 ``sys.addaudithook`` 记账，只数落在
``E2B_STATE_BASE`` 下的路径操作，然后同一个形状再开一个 ``E2B_NODE_STATE_BASE``
跑一遍对照。

2026-10-02（本机，``tmp/venv``，Python 3.12，worker 形状 = root-like + per-sandbox
uid 池打开）：

    BEFORE (no node base)  writes=14  reads=2   （16 次路径操作全在共享 base 上）
    AFTER  (node base)     writes=0   reads=2

* **writes** 是 Task 4 拿掉的东西：``.creating`` 的 mkdir+write、``.uid_pool.lock``
  的建/开、``.uid_reservations/`` 的 mkdir + 写 + rename、``disk-stats`` 的两个
  mkdir + write + rename + 两次 chmod。每一条在 NAS 上都是一次元数据往返，在节点
  本地盘上是微秒。它们现在一条都不落共享 base。
* **reads=2** 是**故意留着的**：``<state>/_runtime`` 的列举 + 那条邻居记录 —— 舰队
  级 uid 账本的索引（``uid_pool._recorded_uids``，Review Focus 3）。它必须共享，
  而且是只读的。

⚠ 这是**机制**的读数，不是延迟读数：一次 ``prepare`` 的实际 p50 由
``prepare_phase_cost_probe.py`` 在集群上量（Task 1 的基线 72–76 ms，期望 ~10 ms）。
探针里把 ``os.geteuid``/``os.getegid`` 报成 0，只是为了进到部署形态的那条代码路径
（没有特权文件步骤的 worker 根本不会分配 uid，也就测不到 uid 池那几条）——它做的
事只有写文件，不需要任何权限。

    PYTHONPATH=. tmp/venv/bin/python deploy/scripts/acceptance/node_state_split_local_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

# The deployed worker is the root-like shape (an agent is configured), which is
# what enables the per-sandbox uid pool -- the third chip `prepare` writes.
os.geteuid = lambda: 0  # type: ignore[assignment]
os.getegid = lambda: 0  # type: ignore[assignment]

from envd_service.app import create_app as create_envd_app  # noqa: E402
from envd_service.config import Settings as EnvdSettings  # noqa: E402
from envd_service.runtime.registry import RuntimeRegistry  # noqa: E402

KEY = "internal-key"
REPO = Path(__file__).resolve().parents[3]

#: Every path-touching audit event. ``open`` covers reads and writes (the mode/
#: flags say which); ``os.stat`` is deliberately *not* here -- the harness wants
#: a short, honest list of operations that cost a round trip, not every probe.
OP_EVENTS = {
    "open",
    "os.mkdir",
    "os.rename",
    "os.replace",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.chmod",
    "os.chown",
    "os.listdir",
    "os.scandir",
}
WRITE_EVENTS = {
    "os.mkdir",
    "os.rename",
    "os.replace",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.chmod",
    "os.chown",
}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

COUNTING = [False]
OPS: list[tuple[str, str]] = []
WATCH: list[Path] = []


def _is_write(event: str, args: tuple) -> bool:
    if event in WRITE_EVENTS:
        return True
    if event != "open":
        return False
    if len(args) >= 3 and isinstance(args[2], int):
        return bool(args[2] & WRITE_FLAGS) or (args[2] & os.O_ACCMODE) != os.O_RDONLY
    mode = args[1] if len(args) >= 2 and isinstance(args[1], str) else ""
    return any(ch in mode for ch in "wax+")


def _hook(event: str, args: tuple) -> None:
    if not COUNTING[0] or event not in OP_EVENTS or not args:
        return
    first = args[0]
    if not isinstance(first, str):
        return
    path = Path(first)
    for root in WATCH:
        if path == root or root in path.parents:
            OPS.append(("WRITE" if _is_write(event, args) else "read", f"{event}:{path}"))
            return


sys.addaudithook(_hook)


async def _prepare(settings: EnvdSettings, sandbox_id: str) -> None:
    registry = RuntimeRegistry(settings.workspace_base, state_base=settings.state_base)
    app = create_envd_app(
        settings=settings,
        runtime_registry=registry,
        workspace_base=settings.workspace_base,
    )
    OPS.clear()
    COUNTING[0] = True
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            resp = await client.post(
                "/agent/sandboxes",
                json={"sandboxID": sandbox_id, "phase": "prepare", "diskMB": 1024},
                headers={"X-Internal-Key": KEY},
            )
    finally:
        COUNTING[0] = False
    assert resp.status_code == 200, (resp.status_code, resp.text)


async def main() -> int:
    summary: dict[str, dict[str, int]] = {}
    for label, with_node_base in (("BEFORE (no node base)", False), ("AFTER", True)):
        root = Path(tempfile.mkdtemp(dir=REPO / "tmp"))
        workspace = root / "workspaces"
        state = root / "export" / "state"
        node = root / "node" / "state"
        WATCH[:] = [state]
        # A live deployment's shared record directory exists (the deployment's
        # init container creates it), so the ledger's read happens in *both*
        # shapes -- it is the one shared-base access Task 4 keeps.
        (state / "_runtime" / "sbx_neighbour").mkdir(parents=True)
        (state / "_runtime" / "sbx_neighbour" / "sandbox.json").write_text(
            json.dumps({"sandbox_id": "sbx_neighbour", "host_uid": 10000}),
            encoding="utf-8",
        )
        settings = EnvdSettings(
            executor="local",
            workspace_base=workspace,
            state_base=state,
            node_state_base=node if with_node_base else None,
            shared_volume_root=None,
            internal_api_key=KEY,
        )
        await _prepare(settings, "sbx_probe")
        writes = sum(1 for kind, _ in OPS if kind == "WRITE")
        reads = len(OPS) - writes
        summary[label] = {"writes": writes, "reads": reads}
        print(f"{label}: shared state base = {state}")
        for kind, op in OPS:
            print(f"  {kind}  {op}")
        print(f"  -> shared-base writes={writes} reads={reads}\n", flush=True)

    before, after = summary["BEFORE (no node base)"], summary["AFTER"]
    print(
        "METRIC prepare_shared_base_writes "
        f"before={before['writes']} after={after['writes']} "
        f"reads_before={before['reads']} reads_after={after['reads']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
