#!/usr/bin/env python3
"""建箱延迟探针（API 边界，纯标准库）—— 区分"平台耗时"与"网络耗时"。

``POST /sandboxes`` 是建箱唯一的入口，SDK 也只是这条请求外面裹了一层。这个探针
只做这一条请求（外加 ``DELETE`` 收尾），因此**能在任何有 python3 的地方跑** ——
包括直接 ``kubectl exec -i`` 灌进控制面 pod，那时客户端到入口的网络只剩回环。
两次读数一减，就是"客户端到入口"这一段（``lightweight_metrics_probe.py`` 走 SDK，
只能从外面量，量不出这一段）。

    # ① 从本机（或任何能连到入口的机器）量：平台 + 网络
    python deploy/scripts/acceptance/create_latency_probe.py \
        --base http://172.18.78.49:3000 --key "$E2B_API_KEY" --n 10

    # ② 灌进控制面 pod，量"只有平台"的那一份
    kubectl -n sandlock exec -i deploy/control-plane-… -c control-plane -- \
        python3 - --base http://127.0.0.1:3000 --key "$E2B_API_KEY" --n 10 \
        < deploy/scripts/acceptance/create_latency_probe.py

每轮建完立刻 ``DELETE``，所以不占容量、也不与其他沙箱争资源。先跑一次预热建箱
（镜像要在 worker 本地已解包），那一行单列出来：**它就是"解析/解包基础镜像"的
代价** —— 2026-10-01 用它验 N54 的落盘 digest 缓存时，把 worker 上的
``<image cache>/.digests/*.json`` 删掉再跑，预热那一行从 238 ms 变 652 ms，
之后又回到 228 ms（见 ``docs/deploy-clusters.md`` §7.25）。

输出：每个样本一行 ``create i/n: … ms``，末尾一条便于抄进文档的
``METRIC create_warm base=… p50_ms=… p95_ms=… mean_ms=… n=…``。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile -- 样本量只有 10 上下，不做拟合。"""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def _create(base: str, key: str, template: str) -> tuple[str, float]:
    req = urllib.request.Request(
        f"{base}/sandboxes",
        data=json.dumps({"templateID": template}).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
    return body["sandboxID"], (time.monotonic() - started) * 1000


def _delete(base: str, key: str, sandbox_id: str) -> None:
    req = urllib.request.Request(
        f"{base}/sandboxes/{sandbox_id}",
        headers={"X-API-Key": key},
        method="DELETE",
    )
    urllib.request.urlopen(req, timeout=60).read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=os.environ.get("E2B_API_URL"))
    parser.add_argument("--key", default=os.environ.get("E2B_API_KEY"))
    parser.add_argument("--template", default="base")
    parser.add_argument("--n", type=int, default=10)
    args = parser.parse_args()

    # 预热：镜像若还没在 worker 上解包，第一发会把这笔一次性代价算进样本里。
    warm_id, warm_ms = _create(args.base, args.key, args.template)
    _delete(args.base, args.key, warm_id)
    print(f"warmup (image unpack) {warm_ms:.0f} ms", flush=True)

    samples: list[float] = []
    for i in range(args.n):
        sid, ms = _create(args.base, args.key, args.template)
        _delete(args.base, args.key, sid)
        samples.append(ms)
        print(f"create {i + 1}/{args.n}: {ms:.0f} ms", flush=True)
    print(
        f"METRIC create_warm base={args.base}"
        f" p50_ms={_pct(samples, 0.5):.0f}"
        f" p95_ms={_pct(samples, 0.95):.0f}"
        f" mean_ms={statistics.fmean(samples):.0f} n={len(samples)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
