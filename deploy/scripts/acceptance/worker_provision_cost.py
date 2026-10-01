#!/usr/bin/env python3
"""建箱那一次 ``POST /agent/sandboxes`` 单独有多贵（控制面 vs worker 的切分）。

``create_latency_probe.py`` 量的是**客户端看到的**建箱；这个量的是其中
**worker 自己那一跳**，用来回答"优化该往哪边使劲"。做法是**幂等重放**：

1. 先在**控制面 pod 里**用真 API 建一个沙箱（于是控制面有记录、worker 认得这个
   id）；
2. 把同一份 provisioning payload **直接打给 worker 的 agent 端口**并计时——
   worker 认得这个 id（``runtime_registry.get(id)`` 非空），走的是幂等重放路径，
   **且这份 payload 不带 ``materialized``**（见下），所以它量到的正是
   "worker 自己建树 + 经控制面中继改属主"那条最慢的老路；
3. 用真 API 删掉，不留沙箱。

⚠ 从控制面直送（v2）之后，正常建箱的 payload 会带 ``"materialized": true``，
worker 那一跳里已经没有建树/拷贝/改属主——**这个探针量的不再是出厂建箱**，而是
它的对照面（老路 / 降级路 / 迁移与 fork 那几条调用点）。要量真实建箱，看
``create_latency_probe.py`` 与 ``E2B_CREATE_TRACE=1`` 的 ``materialize`` 段。

    # 灌进控制面 pod 跑（要有 python3，不需要 e2b SDK）
    kubectl -n sandlock exec -i <control-plane-pod> -c control-plane -- \
        python3 - "$E2B_API_KEY" "$E2B_INTERNAL_API_KEY" \
        < deploy/scripts/acceptance/worker_provision_cost.py

输出每个样本一行 ``create(api) … | worker replay …``，末尾一条
``METRIC worker_provision p50_ms=… n=…``。两边一比即得控制面的份额：
2026-10-01 在 `0.1.0-841` 上量到 **worker 203 ms / 整条 235 ms**（≈85%）。

要把 worker 内部再拆开，用 worker 自己的逐段开关
（``E2B_CREATE_TRACE=1``，见 ``gateway_common/create_trace.py``）：它按
``provision``/``prime``/``record``/``commit``/``fileop:*`` 各打一行 INFO。
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request


def _req(
    url: str,
    *,
    key: str,
    method: str = "GET",
    payload: dict | None = None,
    header: str = "X-API-Key",
) -> tuple[dict, float]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={header: key, "Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
    return (json.loads(body) if body else {}), (time.monotonic() - started) * 1000


def main() -> int:
    api_key, internal_key = sys.argv[1], sys.argv[2]
    api = os.environ.get("API_URL", "http://127.0.0.1:3000")
    n = int(os.environ.get("N", "6"))

    replays: list[float] = []
    for i in range(n):
        created, create_ms = _req(
            f"{api}/sandboxes",
            key=api_key,
            method="POST",
            payload={"templateID": "base"},
        )
        sandbox_id = created["sandboxID"]
        route, _ = _req(
            f"{api}/internal/routes/{sandbox_id}",
            key=internal_key,
            header="X-Internal-Key",
        )
        payload = {
            "sandboxID": sandbox_id,
            "accessToken": created["envdAccessToken"],
            "envVars": {},
            "baseImage": os.environ["E2B_BASE_IMAGE"],
            "memoryMB": 512,
            "cpuPercent": 100,
            "diskMB": 1024,
            "maxProcesses": 100,
            "allowInternetAccess": False,
            "allowPublicTraffic": False,
            "network": {},
            "maxCommandTimeout": 300,
            "volumeMounts": [],
            "mcp": None,
            "iamTokens": None,
            "snapshotTar": None,
            "snapshotID": None,
        }
        _, replay_ms = _req(
            f"{route['address']}/agent/sandboxes",
            key=internal_key,
            method="POST",
            payload=payload,
            header="X-Internal-Key",
        )
        replays.append(replay_ms)
        _req(f"{api}/sandboxes/{sandbox_id}", key=api_key, method="DELETE")
        print(
            f"[{i + 1}/{n}] create(api) {create_ms:.0f} ms | worker replay {replay_ms:.0f} ms",
            flush=True,
        )

    print(
        "METRIC worker_provision p50_ms=%.0f mean_ms=%.0f n=%d"
        % (statistics.median(replays), statistics.fmean(replays), len(replays))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
