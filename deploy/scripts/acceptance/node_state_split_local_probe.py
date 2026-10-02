#!/usr/bin/env python3
"""Task 4 / N57 的**本地**尺子：``prepare`` 在共享 base 上还剩几次路径操作。

Task 4 的机制论断是"``prepare`` 的长杆全是共享卷上的元数据往返"（Task 1 §1.1：
共享 NAS 上一次 ``open(create)``/``mkdir`` ≈ 13 ms，节点本地盘 ≈ 0.03 ms）。这个
探针不连集群、不改部署，把那个**机制**直接数出来：在真 app（``executor='local'``）
里跑一次 ``phase: prepare``，用一个 ``sys.addaudithook`` 记账，只数落在
``E2B_STATE_BASE`` 下的路径操作，然后同一个形状再开一个 ``E2B_NODE_STATE_BASE``
跑一遍对照；最后第三档是**出厂常态**——payload 带着控制面分配的 ``hostUID``，走
``UidPool.claim``（OBS-9）。

2026-10-03 实测（本机，``.venv``，Python 3.12，worker 形状 = root-like + per-sandbox
uid 池打开；``pool-own-file writes`` 只数 ``.uid_pool.lock`` 与 ``.uid_reservations/``
两处的写）：

    BEFORE (no node base, fallback)  writes=14  reads=2  pool-own writes=4
    AFTER  (node base, fallback)     writes=4   reads=2  pool-own writes=4
    AFTER  (node base, claim)        writes=0   reads=0  pool-own writes=0

* **Task 4 的收益原样保留**：``.creating`` 的 mkdir+write 与 ``disk-stats`` 的两次
  mkdir + write + rename + 两次 chmod —— AFTER 的两档里它们一条都不落共享 base
  （14 笔写里属于它俩的 10 笔：BEFORE 与 AFTER 的差）。
* **N57 把池自己的那一件挪回了共享 base**：``.uid_pool.lock``（建/开）与
  ``.uid_reservations/``（mkdir + 写 + rename），共 **4 笔写**。这是裁定接受的代价，
  而且**只在回落形状**上付：payload 里没有 ``hostUID``（老控制面、老记录、别的调用
  方）时走 ``UidPool.acquire``，它"读共享记录索引 → 挑空闲 uid → 写预约标记"的临界
  区必须跨节点互斥，锁与标记就是那个串行点。**出厂常态走 ``claim``（第三档）：既不
  写也不锁这两个文件，``pool-own writes=0``**；唯一剩下的一次共享 base 访问是
  ``commit`` 里那次标记存在性 ``stat``（本探针只统计 ``open``/``read``/``listdir``
  这类事件，**不含 ``os.stat``**，所以 ``reads=0`` 是"没有 open/read 级访问"的口径，
  不是"没有任何磁盘询问"）。
* **reads** 是**故意留着的**：回落档的 2 笔是 ``<state>/_runtime`` 的列举 + 那条邻居
  记录 —— 舰队级 uid 账本的索引（``uid_pool._recorded_uids``，Review Focus 3），它
  必须共享而且是只读的；``claim`` 档连这个索引都不用读（控制面已经点好 uid），所以
  ``reads=0``。

⚠ 这是**机制**的读数，不是延迟读数：一次 ``prepare`` 的实际 p50 由
``prepare_phase_cost_probe.py`` 在集群上量（Task 1 的基线 72–76 ms，期望 ~10 ms）。
探针里把 ``os.geteuid``/``os.getegid`` 报成 0，只是为了进到部署形态的那条代码路径
（没有特权文件步骤的 worker 根本不会分配 uid，也就测不到 uid 池那几条）——它做的
事只有写文件，不需要任何权限。

    PYTHONPATH=. .venv/bin/python deploy/scripts/acceptance/node_state_split_local_probe.py
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

#: The uid the control plane would hand down in the ``claim`` shape (OBS-9).
#: Inside the pool's default range (``Settings.uid_pool_start`` = 10000,
#: ``uid_pool_size`` = 1000), so the shape is the deployed one and not a
#: refusal path.
CLAIM_UID = 10050

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


async def _prepare(
    settings: EnvdSettings, sandbox_id: str, *, host_uid: int | None = None
) -> RuntimeRegistry:
    registry = RuntimeRegistry(settings.workspace_base, state_base=settings.state_base)
    app = create_envd_app(
        settings=settings,
        runtime_registry=registry,
        workspace_base=settings.workspace_base,
    )
    payload = {"sandboxID": sandbox_id, "phase": "prepare", "diskMB": 1024}
    if host_uid is not None:
        # The common deployment path: the control plane allocated the uid and
        # the worker only records it (``UidPool.claim``).
        payload["hostUID"] = host_uid
    OPS.clear()
    COUNTING[0] = True
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            resp = await client.post(
                "/agent/sandboxes",
                json=payload,
                headers={"X-Internal-Key": KEY},
            )
    finally:
        COUNTING[0] = False
    assert resp.status_code == 200, (resp.status_code, resp.text)
    return registry


def _is_pool_own_write(kind: str, op: str, state: Path) -> bool:
    """Whether one counted operation is on the pool's own files.

    Those are the two the N57 ruling moved back to the shared base: the flock
    file and the reservation markers. They are the *only* shared-base writes
    ``acquire`` (the fallback shape) adds over the ``claim`` shape.
    """
    if kind != "WRITE":
        return False
    _, _, raw = op.partition(":")
    path = Path(raw)
    reservations = state / ".uid_reservations"
    return (
        path == state / ".uid_pool.lock"
        or path == reservations
        or reservations in path.parents
    )


async def main() -> int:
    summary: dict[str, dict[str, int]] = {}
    shapes = (
        ("BEFORE (no node base)", False, None),
        ("AFTER (node base, fallback)", True, None),
        ("AFTER (node base, claim)", True, CLAIM_UID),
    )
    for label, with_node_base, claim_uid in shapes:
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
        registry = await _prepare(settings, "sbx_probe", host_uid=claim_uid)
        # The numbers below are only the shapes they claim to be if the pool
        # really did take the uid: ``claim`` in the third shape, the fallback
        # ``acquire`` (which is the one that writes the marker) in the others.
        assert registry.uid_pool is not None, "this probe needs the uid pool on"
        held = registry.uid_pool.held_uid("sbx_probe")
        assert held is not None, "the pool must have taken a uid for this shape"
        marker_dir = state / ".uid_reservations"
        markers = (
            sorted(path.name for path in marker_dir.iterdir())
            if marker_dir.is_dir()
            else []
        )
        if claim_uid is not None:
            assert held == claim_uid
            assert markers == []
        else:
            assert markers == ["sbx_probe"]
            assert (marker_dir / "sbx_probe").read_text(
                encoding="utf-8"
            ) == f"{held}\n"
        writes = sum(1 for kind, _ in OPS if kind == "WRITE")
        reads = len(OPS) - writes
        pool_own = sum(
            1 for kind, op in OPS if _is_pool_own_write(kind, op, state)
        )
        summary[label] = {"writes": writes, "reads": reads, "pool_own": pool_own}
        print(f"{label}: shared state base = {state}")
        for kind, op in OPS:
            print(f"  {kind}  {op}")
        print(
            f"  -> shared-base writes={writes} reads={reads} "
            f"pool-own-file writes={pool_own}\n",
            flush=True,
        )

    before = summary["BEFORE (no node base)"]
    after = summary["AFTER (node base, fallback)"]
    claim = summary["AFTER (node base, claim)"]
    print(
        "METRIC prepare_shared_base_writes "
        f"before={before['writes']} after={after['writes']} "
        f"reads_before={before['reads']} reads_after={after['reads']}"
    )
    print(
        "METRIC prepare_pool_own_file_writes "
        f"fallback={after['pool_own']} claim={claim['pool_own']} "
        f"(claim shape: payload carries hostUID)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
