#!/usr/bin/env python3
"""轻量化指标探针：建箱延迟 / 命令往返 / 活动沙箱的内存足迹。

对着一个已经预热好的部署跑（镜像在 worker 本地缓存里，见
``deploy/scripts/warm_base_image.py``）：

    export E2B_API_URL=http://<入口>:3000 E2B_SANDBOX_URL=http://<入口>:3000
    export E2B_API_KEY=... E2B_INTERNAL_API_KEY=...
    python deploy/scripts/acceptance/lightweight_metrics_probe.py

输出全部是 ``METRIC <名字> <键值对...>`` 一行一条，便于抄进文档 / 与上一版对比：

* ``METRIC create_warm p50_ms=… p95_ms=… n=…``  —— 镜像已缓存前提下的建箱耗时
  （每轮建完立刻 kill，所以不占容量，也不与其他沙箱争资源）；
* ``METRIC command_rtt p50_ms=… p95_ms=… n=…`` —— 一条 ``echo`` 命令的 SDK 往返
  （建箱 → 派发 → 读流），走的是和 SDK 完全一样的网关路径；
* ``METRIC sandbox_stat_us per_call_us=… n=…`` —— 沙箱内一次 ``os.stat`` 的耗时
  （记账用的 statx 会让这个数偏小，量的是"中介 + 文件系统"的实际手感）；
* ``METRIC active_sandbox_memory …`` —— ``--memory``（默认开）时在 worker pod 里
  按 uid 汇总 ``/proc/*/status`` 的 ``VmRSS``：**每个活动沙箱（一条常驻命令在跑）
  自己那组进程的常驻内存**，不含页缓存、不含 worker 自己的进程。

    --no-memory       跳过内存那一段（没有 kubectl / 不是 k8s 部署时用）
    --namespace …     worker pod 所在 namespace（默认 sandlock）
    --sandboxes N     内存段同时在跑的沙箱数（默认 4，受节点容量限制）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time

import httpx

#: 沙箱进程在宿主上的 uid 从这个池开始（``E2B_UID_POOL_START``，出厂 10000）。
POOL_START = int(os.environ.get("E2B_UID_POOL_START", "10000"))
#: 池大小（``E2B_UID_POOL_SIZE``，出厂 1000）—— 用来把 worker 自己的 uid 排除在外。
POOL_SIZE = int(os.environ.get("E2B_UID_POOL_SIZE", "1000"))

#: 在 worker pod 里按 uid 汇总 RSS。沙箱自己的进程（supervisor + 沙箱内进程）
#: 都以**池范围**内的 uid 运行（``E2B_UID_POOL_START`` 起 ``E2B_UID_POOL_SIZE`` 个），
#: worker 自己的进程是 65534（比池起点大，所以必须用**区间**而不是">="）。
RSS_SCAN = r"""
import json, os
lo = int(os.environ['POOL_START'])
hi = lo + int(os.environ['POOL_SIZE'])
totals = {}
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    try:
        with open('/proc/%s/status' % pid, encoding='utf-8') as fh:
            status = fh.read()
    except OSError:
        continue
    uid = next((int(l.split()[1]) for l in status.splitlines()
                if l.startswith('Uid:')), None)
    if uid is None or not (lo <= uid < hi):
        continue
    rss = next((int(l.split()[1]) for l in status.splitlines()
                if l.startswith('VmRSS:')), 0)
    totals[str(uid)] = totals.get(str(uid), 0) + rss
print(json.dumps(totals))
"""


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile -- the sample size here is 10–20, so no fitting."""
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


#: 每建一个都记下来：探针中途失败也不留沙箱占容量（``__main__`` 的 finally 收尾）。
_CREATED: list = []


def _create(sandbox_cls):
    box = sandbox_cls.create()
    _CREATED.append(box)
    return box


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--creates", type=int, default=10)
    parser.add_argument("--commands", type=int, default=20)
    parser.add_argument("--sandboxes", type=int, default=4)
    parser.add_argument("--namespace", default="sandlock")
    parser.add_argument("--no-memory", action="store_true")
    args = parser.parse_args()

    api_url = os.environ["E2B_API_URL"].rstrip("/")
    sandbox_url = os.environ["E2B_SANDBOX_URL"].rstrip("/")
    api_key = os.environ.get("E2B_API_KEY", "local-key")
    internal_key = os.environ.get("E2B_INTERNAL_API_KEY", "internal-key")
    os.environ["E2B_API_KEY"] = api_key

    def route_of(sandbox_id: str) -> str:
        resp = httpx.get(
            f"{api_url}/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": internal_key},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["address"]

    from e2b import Sandbox

    # 0. 预热：确保 base image 已在 worker 本地缓存里（否则第一次建箱要解包）。
    warm = _create(Sandbox)
    warm.commands.run("/bin/echo warm")
    warm.kill()

    # 1. 建箱延迟（镜像已缓存）。每轮立刻 kill：不占容量、互不干扰。
    create_ms: list[float] = []
    for i in range(args.creates):
        started = time.monotonic()
        box = _create(Sandbox)
        create_ms.append((time.monotonic() - started) * 1000)
        box.commands.run("/bin/echo ok")
        box.kill()
        print(f"create {i + 1}/{args.creates}: {create_ms[-1]:.0f} ms", flush=True)
    print(
        "METRIC create_warm"
        f" p50_ms={_pct(create_ms, 0.5):.0f}"
        f" p95_ms={_pct(create_ms, 0.95):.0f}"
        f" mean_ms={statistics.fmean(create_ms):.0f}"
        f" n={len(create_ms)}"
    )

    # 2. 命令往返：SDK 一次 commands.run 的端到端耗时。
    box = _create(Sandbox)
    print(f"command probe sandbox -> {route_of(box.sandbox_id)}")
    # 丢弃第一条：它包含 route-B 槽位的租用（第一个 exec 才建实例），
    # 那是"沙箱启动"的成本，不是"命令往返"的。
    box.commands.run("/bin/echo warm-up")
    rtt_ms: list[float] = []
    for _ in range(args.commands):
        started = time.monotonic()
        res = box.commands.run("/bin/echo ok")
        rtt_ms.append((time.monotonic() - started) * 1000)
        assert (res.stdout or "").strip() == "ok", res
    print("  rtt samples ms:", " ".join(f"{v:.0f}" for v in rtt_ms))
    print(
        "METRIC command_rtt"
        f" p50_ms={_pct(rtt_ms, 0.5):.1f}"
        f" p95_ms={_pct(rtt_ms, 0.95):.1f}"
        f" mean_ms={statistics.fmean(rtt_ms):.1f}"
        f" n={len(rtt_ms)}"
    )

    # 3. 沙箱内一次 stat 的成本：两个位置各量一次，把"中介成本"和"NAS 往返"分开。
    #    路径用相对名：命令的 cwd 就是沙箱自己的根，/tmp 在 chroot 形态下不可写。
    def stat_cost(path_expr: str, label: str) -> float:
        cmd = (
            "python3 -c \"import os,time;"
            f"p={path_expr};"
            "open(p,'w') if not os.path.exists(p) else None;"
            "os.stat(p);"  # 预热：别把首次目录项查询算进去
            "n=2000;t=time.perf_counter();"
            "[os.stat(p) for _ in range(n)];"
            "print('PER_CALL_US', (time.perf_counter()-t)/n*1e6)\""
        )
        out = box.commands.run(cmd)
        match = re.search(r"PER_CALL_US\s+([0-9.]+)", out.stdout or "")
        assert match, out
        value = float(match.group(1))
        print(f"METRIC sandbox_stat_us {label} per_call_us={value:.1f} n=2000")
        return value

    stat_cost("'stat-probe'", "workspace_file_nas")
    stat_cost("'/etc/os-release'", "image_rootfs_local")
    box.kill()

    # 4. 活动沙箱的内存足迹：每个沙箱跑一条常驻命令，然后在 worker pod 里按 uid 汇总 RSS。
    if args.no_memory:
        print("METRIC active_sandbox_memory skipped=--no-memory")
        return 0
    boxes: list[tuple[Sandbox, str]] = []
    handles = []
    try:
        for i in range(args.sandboxes):
            held = _create(Sandbox)
            # 常驻命令：沙箱"活着且在干活"时它自己的进程组是什么样。
            handles.append(held.commands.run("/bin/sleep 600", background=True))
            boxes.append((held, route_of(held.sandbox_id)))
            print(f"active sandbox {i + 1}/{args.sandboxes} -> {boxes[-1][1]}", flush=True)
        time.sleep(2)

        pods = subprocess.run(
            [
                "kubectl", "-n", args.namespace, "get", "pods",
                "-l", "app=e2b-worker", "--no-headers", "-o", "name",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if pods.returncode != 0 or not pods.stdout.strip():
            print(f"METRIC active_sandbox_memory skipped=kubectl ({pods.stderr.strip()[:80]})")
            return 0
        per_uid: dict[str, int] = {}
        for pod in pods.stdout.split():
            probe = subprocess.run(
                [
                    "kubectl", "-n", args.namespace, "exec", "-i", pod, "--",
                    "env", f"POOL_START={POOL_START}", f"POOL_SIZE={POOL_SIZE}",
                    "python3", "-",
                ],
                input=RSS_SCAN, capture_output=True, text=True, timeout=120,
            )
            if probe.returncode != 0:
                print(f"METRIC active_sandbox_memory skipped=exec ({probe.stderr.strip()[:80]})")
                return 0
            for uid, kb in json.loads(probe.stdout.strip().splitlines()[-1]).items():
                per_uid[uid] = per_uid.get(uid, 0) + kb
        if not per_uid:
            print("METRIC active_sandbox_memory skipped=no-sandbox-processes")
            return 0
        mib = sorted(v / 1024 for v in per_uid.values())
        print(
            "METRIC active_sandbox_memory"
            f" sandboxes={len(per_uid)}"
            f" min_mib={mib[0]:.0f}"
            f" max_mib={mib[-1]:.0f}"
            f" total_mib={sum(mib):.0f}"
        )
        print("  per-uid MiB:", ", ".join(f"{u}:{v / 1024:.0f}" for u, v in per_uid.items()))
    finally:
        for held, _ in boxes:
            try:
                held.kill()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for _box in _CREATED:
            try:
                _box.kill()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
    sys.exit(code)
