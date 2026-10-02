#!/usr/bin/env python3
"""建箱 ``prepare`` 那一段单独有多贵（Task 4 的验收尺子）。

Task 1（``docs/create-local-first-design.md`` §3，活体实测）把一次建箱拆成
``prepare`` / ``finalize`` / ``prime`` / ``record`` 四段，**``prepare`` 是长杆
（72–76 ms）**。根因在 ``gateway_common/paths.py`` 的三个 path helper：``prepare``
要写的三样东西 —— ``.creating`` 标记、uid 认领、``disk-stats`` 种子 —— 当时都落在
**共享** ``E2B_STATE_BASE`` 上，而共享卷是 NFS，**每次元数据往返 ~13 ms**（同等
200 个 64 B 文件的读数，§1.1）。Task 4 把它们（连同 ``.route-b`` 与 uid 池自己的
``.uid_pool.lock`` / ``.uid_reservations/``）搬到**节点本地**的
``E2B_NODE_STATE_BASE``，记录（``_runtime/<id>/sandbox.json``）与 checkpoint 仓仍留
共享 —— 后者是舰队级 uid 账本的索引（``envd_service/uid_pool.py::_recorded_uids``）。

这个探针量**只有 ``prepare`` 那一跳**：直接打 worker 的 agent 口，``phase:
prepare``，立刻 ``phase: cancel`` 收回（标记、uid 保留、记账种子都撤掉，不落记录、
不建树）。这样：

* 不需要开 ``E2B_CREATE_TRACE``（那要改 statefulset 的 env，是一次集群写）；
* 也不再和 ``materialize``/``finalize`` 混在一起 —— 后者是另一条腿（Task 3/5 的
  事，建箱的 floor 是 ``max(materialize, prepare)``）。

判据只有一条：**同一个 worker 上 ``prepare`` 的 p50**。上线前它应当落在 Task 1
的 72–76 ms 里；上线后应当掉到 ~10 ms（剩下的是一次 mkdir + 两个小写 + Python
自己的开销，全在节点本地盘上）。探针跑在**控制面 pod 里**（``/internal/routes``
要 internal key，而且只有那里有稳定的入口地址），建箱/杀箱都走公开 API，**不留
沙箱、不留记录**。

```bash
deploy/scripts/open-cluster-tunnel.sh          # 通道 + 身份自检（2 节点 / arm64 / +k0s）
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
CP=$(kubectl -n sandlock get pod -l app=control-plane -o jsonpath='{.items[0].metadata.name}')
kubectl -n sandlock exec -i "$CP" -c control-plane -- \
    python3 - "$E2B_API_KEY" "$E2B_INTERNAL_API_KEY" --n 10 \
    < deploy/scripts/acceptance/prepare_phase_cost_probe.py

# 建箱整条的 p50（客户端边界）与逐段读数：
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/create_latency_probe.py --base http://172.18.78.49:3000 \
    --key "$E2B_API_KEY" --n 10
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE=1   # 跑完记得关
kubectl -n sandlock logs e2b-worker-0 --since=5m | grep "create trace:" | sort | uniq -c
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE-
```

2026-10-02 的基线（Task 1，``0.1.0-887-g7ef319b``，``E2B_CREATE_TRACE=1``）：
``prepare`` **72–76 ms**、``finalize`` 7.7–8.0、``prime`` 5.3–11.3、``record`` ≈51
（record 不在响应路径上）。本探针量的就是其中的 ``prepare`` 一段。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid


def _req(
    url: str,
    *,
    key: str,
    header: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 120.0,
) -> tuple[dict, float]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={header: key, "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # Named, not swallowed: a 500 from the agent is the answer this probe is
        # here to read, and a bare traceback would hide the body that says why.
        raise SystemExit(
            f"{method} {url} -> {exc.code}: {exc.read().decode(errors='replace')[:400]}"
        ) from exc
    return (json.loads(body) if body else {}), (time.monotonic() - started) * 1000


def _worker_address(api: str, api_key: str, internal_key: str) -> str:
    """One live sandbox's worker, then that sandbox is deleted again.

    ``/internal/routes/<id>`` is the only endpoint that answers "which node is
    this sandbox on, and where do I reach it" -- so the probe borrows one real
    create (a public-API sandbox, which ruling 5 allows) to learn the address,
    and gives it straight back.
    """
    created, _ = _req(
        f"{api}/sandboxes",
        key=api_key,
        header="X-API-Key",
        method="POST",
        payload={"templateID": "base"},
    )
    sandbox_id = created["sandboxID"]
    try:
        route, _ = _req(
            f"{api}/internal/routes/{sandbox_id}",
            key=internal_key,
            header="X-Internal-Key",
        )
        address = route["address"]
    finally:
        _req(
            f"{api}/sandboxes/{sandbox_id}",
            key=api_key,
            header="X-API-Key",
            method="DELETE",
        )
    return address


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("api_key", help="control-plane API key (X-API-Key)")
    parser.add_argument("internal_key", help="shared internal key (X-Internal-Key)")
    parser.add_argument(
        "--api",
        default=os.environ.get("API_URL", "http://127.0.0.1:3000"),
        help="control-plane base URL (default: in-cluster 127.0.0.1:3000)",
    )
    parser.add_argument(
        "--worker-url",
        default=None,
        help="skip the throwaway create and use this worker's agent address",
    )
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--disk-mb", type=int, default=1024)
    parser.add_argument(
        "--hold-s",
        type=float,
        default=0.0,
        help=(
            "sleep this long between each prepare and its cancel, so an operator "
            "can look at the node-local chips while they exist (the .creating "
            "marker only lives from prepare until the record is durable)"
        ),
    )
    args = parser.parse_args()

    worker = args.worker_url or _worker_address(
        args.api, args.api_key, args.internal_key
    )
    print(f"worker={worker}", flush=True)

    samples: list[float] = []
    for index in range(args.n):
        # A fresh id every sample: the pool's reservation marker and the marker
        # file are per-sandbox, and reusing one id would measure the *cancel*
        # of the previous sample instead of a clean prepare.
        sandbox_id = f"probe4_{uuid.uuid4().hex[:16]}"
        payload = {
            "sandboxID": sandbox_id,
            "phase": "prepare",
            "diskMB": args.disk_mb,
        }
        _, prepare_ms = _req(
            f"{worker}/agent/sandboxes",
            key=args.internal_key,
            header="X-Internal-Key",
            method="POST",
            payload=payload,
        )
        samples.append(prepare_ms)
        if args.hold_s:
            # Explicit, bounded pause: this is what makes the §7.31 step-③ check
            # re-runnable -- the marker exists exactly in this window, and the
            # cancel below is what takes it (and the seed, and the pool's own
            # files) back off. Use `--n 1 --hold-s 25` for the inspection.
            print(
                f"holding {args.hold_s:.0f}s before cancel "
                f"(sandboxID={sandbox_id})",
                flush=True,
            )
            time.sleep(args.hold_s)
        # Undo the prepared half: release the uid, drop the marker and the
        # accounting seed. No record was written and no tree was built, so the
        # fleet is left exactly as it was found.
        _req(
            f"{worker}/agent/sandboxes",
            key=args.internal_key,
            header="X-Internal-Key",
            method="POST",
            payload={"sandboxID": sandbox_id, "phase": "cancel"},
            timeout=30.0,
        )
        print(
            f"[{index + 1}/{args.n}] prepare {prepare_ms:.1f} ms "
            f"(marker+uid+disk-stats)",
            flush=True,
        )

    print(
        "METRIC prepare_phase p50_ms=%.1f mean_ms=%.1f min_ms=%.1f max_ms=%.1f n=%d"
        % (
            statistics.median(samples),
            statistics.fmean(samples),
            min(samples),
            max(samples),
            len(samples),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
