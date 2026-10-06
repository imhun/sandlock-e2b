# Task 7 报告 —— N83 Phase 1：验收脚本 + 本地车道验收 + 文档 + 回退杆

工作树：`/Users/polus/project/ai/sandlock-e2b.wt/task7`（branch `n83/task7`，起点 = 集成分支
`n83-phase1-cgroup` 的 `95907a1`）。**没有 merge / rebase / push。**

## 0. 结论

* 验收脚本 **`deploy/scripts/acceptance/cgroup_acceptance.py`** 写好并跑通：**五条检查全绿**
  （`"ok": true`，退出码 0）。
* 验收在**本地 compose 多节点栈**（自己的项目名 `n83acc`、宿主端口 3200）上跑；**k0s 集群一个 pod
  都没碰**（这是 `AGENTS.md` 的顺序：本地 lane 绿了才发线上）。
* 同一条车道、同一支脚本做了 **RED 档**（`E2B_SANDBOX_CGROUP=off`）：`measuredCpuPercent=400.47`、
  沙箱没有 cgroup、洪泛的 0.86 核记在 **worker 自己容器的 cgroup** 上 —— N82 的症状在本地复现。
* 文档四处更新完（env-vars 三个新变量、open-issues N83 行、deploy-clusters §7.48、resource-contention §6），
  回退杆写死在文档里（k8s = `worker-capacity.patch.yaml` 那行翻回 `off`）。

## 1. 改了什么

| 文件 | 动作 |
|---|---|
| `deploy/scripts/acceptance/cgroup_acceptance.py` | 新增（五条检查、一条 JSON、退出码） |
| `docs/env-vars.md` | 新增 `E2B_SANDBOX_CGROUP` / `E2B_CGROUP_MOUNT` / `E2B_CGROUP_DELEGATE_WAIT_S` 三行 |
| `docs/open-issues.md` | N83 行状态：Phase 1 已实施 + 本地车道验收读数 + 回退杆 |
| `docs/deploy-clusters.md` | 新增 §7.48（本地车道验收；**不是**上线记录） |
| `docs/resource-contention.md` | §6 追加"cgroup 落地后的口径"一段 |
| `docs/reports/n83-task-7-cgroup-acceptance.md` | 本报告的仓库内副本（`docs/reports/` 的固化规矩） |
| `docs/reports/README.md` | 固化清单加一行 |

`tmp/` 下（gitignored，不进仓库）：三份 override（含 RED 档）、验收原始 JSON、构建日志、RED 的补充探针。

## 2. 车道、override 与"不让用户环境受影响"

* 起的是 `deploy/compose/docker-compose.multinode.yml`（3 worker + 控制面 + redis + agent 两面），
  **`-p n83acc`** ⇒ 容器名 `n83acc-*`、卷/网络都是独立命名空间。
* **用户那套 live 栈（project `compose`、宿主 3100）全程没动**：没有对它跑过 `up/down/build`，
  没有 stop/rm 它的容器，也没有覆盖 `compose-*` 镜像（我的镜像是 `n83acc-*` + `e2b-sandlock-agent:n83acc`）。
* 从用户 `tmp/` 抄的东西（**只读复制，没改用户文件**）：
  * `tmp/n80/compose-override.yml` 的三处 `E2B_NODE_DISK_MB: "400000"` / `E2B_NODE_PROCESSES: "1024"`
    （Docker VM 整盘已用 ~111 GB，清单里的 `4096` 是准入配额 ⇒ 不覆盖就 `503 No resources available`），
    以及同一文件里的 `E2B_EXECUTOR: sandlock` / `E2B_PER_SANDBOX_UID: "true"`；
  * `wheels/fork/`（gitignored 的构建产物，worker 镜像的 sandlock wheel；本地 VM 是 x86_64，用的是
    `sandlock-0.9.0b0-cp314-cp314-manylinux_2_34_x86_64.whl`）。
* 我自己的 `tmp/n83-acc-override.yml` 在上面基础上加：`ports: !override ["3200:3000"]`（compose 对
  `ports` 是**追加**合并，只写新端口会同时绑 3100 ⇒ 撞车）、`E2B_SANDBOX_CGROUP: "required"`、
  `E2B_SANDBOX_NOTIFY_RATE_LIMIT: "0"`（**只在这次验收里**关掉通知限流，量完随栈拆掉 = 撤回）。
  `tmp/n83-acc-override-off.yml` 是同一份的 `off` 档（RED）。
* `deploy/compose/.env` 从 `.env.example` 复制（本 worktree 原本没有），末尾把 `AGENT_IMAGE` 指到
  **本 worktree 现构建**的 `e2b-sandlock-agent:n83acc`（否则 agent 是旧 registry 镜像，`delegate-cgroup`
  这个 op 根本不存在，worker 的启动自检会一直 `delegation-timeout`）。
* 拆栈：`docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml down`
  （镜像留着）。收尾时容器 0 个、沙箱 0 个、`sbx_*` cgroup 0 个（见 §9）。
* **一次自己的操作事故（已完整回退，如实记）**：中途我用 patchloom MCP 的一次 `replace_text` 改
  `docs/open-issues.md`，而那个 MCP 的工作区根是**主仓库** `/Users/polus/project/ai/sandlock-e2b`
  （不是这个 worktree）⇒ 那一次改动落在了用户的库里。发现后我按原字符串把用户库的那一行**逐字改回**，
  现在用户库 `git status` 里 `docs/open-issues.md` **不再出现**（那个库里另一条
  `tests/unit/test_docs_only_point_at_repo_artifacts.py` 的改动**不是我动的**，也没被我碰过）。
  本仓库的所有正文改动一律走 worktree 内的编辑，后续没有再使用该 MCP。

## 3. 执行过的命令（按时间顺序）

```bash
# 0) 工作树与基线
cd /Users/polus/project/ai/sandlock-e2b.wt/task7 && pwd && git status
git log --oneline -5                     # HEAD = 95907a1 = n83-phase1-cgroup

# 1) 依赖与构建产物
uv venv --python 3.14 tmp/venv
uv pip install --python tmp/venv/bin/python "e2b==2.46.0" httpx
mkdir -p wheels && cp -a /Users/polus/project/ai/sandlock-e2b/wheels/fork wheels/fork
cp deploy/compose/.env.example deploy/compose/.env   # 追加 AGENT_IMAGE=e2b-sandlock-agent:n83acc
docker build -f deploy/docker/Dockerfile.agent -t e2b-sandlock-agent:n83acc .
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml build \
    control-plane worker-1 worker-2 worker-3
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml config   # 校验 !override 生效

# 2) 起栈 + 等委派
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml up -d
docker logs n83acc-worker-{1,2,3}-1 | grep 'cgroup lane ready'   # 三台各一条
# 例（worker-1）：cgroup lane ready (attempt 4): cgroup ready parent=/pod-cgroup/docker/<container-id>
#   worker_uid=65534 drained=1 subtree_control=cpu
#   同一条链的委派回答：cgroup delegation answer: nodeID=worker-1 containerCgroup=/host-cgroup/docker/<id>
#   delegated=['.', 'cgroup.procs', 'cgroup.subtree_control'] workerAnchor=<12-hex>

# 3) 验收（GREEN）
E2B_API_KEY=local-key tmp/venv/bin/python deploy/scripts/acceptance/cgroup_acceptance.py \
    --api-url http://127.0.0.1:3200 --api-key local-key --internal-key internal-key \
    --internal-url http://control-plane:3000 --nodes worker-1,worker-2,worker-3 \
    --worker-exec-template 'docker exec -i n83acc-{node}-1 bash -lc' \
    --out tmp/n83-acceptance-green.json

# 4) 验收（RED：把开关翻回 off，重建 worker）
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override-off.yml up -d
# 同一条命令 → tmp/n83-acceptance-red2.json（exit 1，五条全 FAIL，理由逐条具名）
tmp/venv/bin/python tmp/n83-red-neighbour-cpu.py > tmp/n83-red-neighbour-cpu.json   # RED 的"邻居付账"读数

# 5) 恢复 GREEN 档并复跑最终读数
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml up -d
# 同一条验收命令 → tmp/n83-acceptance-green.json（exit 0）

# 6) 单元/文档钉子（见 §8）
python -m pytest tests/unit/test_docs_only_point_at_repo_artifacts.py -q

# 7) 拆栈
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml down
```

## 4. GREEN 原始读数（脚本 stdout，逐字）

```json
{
  "lane": {
    "api_url": "http://127.0.0.1:3200",
    "internal_url": "http://control-plane:3000",
    "worker_exec_template": "docker exec -i n83acc-{node}-1 bash -lc",
    "nodes": [
      "worker-1",
      "worker-2",
      "worker-3"
    ],
    "cgroup_mount": "/pod-cgroup",
    "template": "base",
    "flood_seconds": 40.0,
    "sandbox_cgroup_env": "required (the caller's override; see the report)",
    "sandbox_notify_rate_limit_env": "0 (the caller's override; only this acceptance)"
  },
  "n82_baseline": {
    "ops_per_s": 18149,
    "cores_on_worker_pod": 1.02
  },
  "checks": {
    "1_quota_is_real": {
      "pass": true,
      "declared_cpu_percent": 100.0,
      "measured_cpu_percent": 99.97929048357675,
      "cpu_max_readback": "100000 100000",
      "sandbox_cgroup": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_fc73ff9407750d0a",
      "spinner_node": "worker-3",
      "first_sandbox_rtt_quiet": {
        "samples_ms": [
          361.81,
          32.39,
          33.64,
          31.48,
          31.93
        ],
        "min_ms": 31.48,
        "median_ms": 32.39
      },
      "second_sandbox_rtt": {
        "samples_ms": [
          183.57,
          37.38,
          32.02,
          32.94,
          42.83
        ],
        "min_ms": 32.02,
        "median_ms": 37.38
      },
      "round_trip_criterion": "min-of-5, within 2x of the quiet baseline (>=200ms floor)",
      "second_sandbox_node": "worker-3",
      "second_sandbox_same_node": true
    },
    "2_kernel_enforces": {
      "pass": true,
      "cgroup": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_fc73ff9407750d0a",
      "cpu_max": "100000 100000",
      "window_s": 3.124,
      "usage_usec_delta": 3107035,
      "nr_throttled_delta": 32,
      "throttled_usec_delta": 9612556,
      "observed_cores": 0.995,
      "quota_cores": 1.0
    },
    "3_flood_spends_own_quota": {
      "pass": true,
      "probe": "probe_n82_traced_syscall_costs.py",
      "n82_baseline": {
        "ops_per_s": 18149,
        "booked_on": "the worker pod"
      },
      "quota_cores": 1.0,
      "flood_alone": {
        "label": "the N82 probe (openclose) alone in its own sandbox",
        "probe_output": "DONE op=openclose stalls=0 rounds=195 elapsed_s=40.1 ops_per_s=9737",
        "ops_per_s": 9737,
        "elapsed_s": 43.3,
        "sandbox_id": "sbx_56dcecb92abfe77b",
        "node_id": "worker-3",
        "cgroup": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
        "cgroup_samples": [
          {
            "at_s": 2.77,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 1596250,
              "user_usec": 591420,
              "system_usec": 1004830,
              "nice_usec": 0,
              "nr_periods": 20,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 4.9,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 3358351,
              "user_usec": 1171767,
              "system_usec": 2186584,
              "nice_usec": 0,
              "nr_periods": 41,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 7.03,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 5118884,
              "user_usec": 1684757,
              "system_usec": 3434127,
              "nice_usec": 0,
              "nr_periods": 62,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 9.16,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 6892710,
              "user_usec": 2215959,
              "system_usec": 4676750,
              "nice_usec": 0,
              "nr_periods": 84,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 11.31,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 8659095,
              "user_usec": 2755383,
              "system_usec": 5903712,
              "nice_usec": 0,
              "nr_periods": 105,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 13.44,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 10421453,
              "user_usec": 3288484,
              "system_usec": 7132969,
              "nice_usec": 0,
              "nr_periods": 126,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 15.56,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 12159922,
              "user_usec": 3786771,
              "system_usec": 8373151,
              "nice_usec": 0,
              "nr_periods": 148,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 17.69,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 13908186,
              "user_usec": 4275661,
              "system_usec": 9632525,
              "nice_usec": 0,
              "nr_periods": 169,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 19.82,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 15666163,
              "user_usec": 4801773,
              "system_usec": 10864390,
              "nice_usec": 0,
              "nr_periods": 190,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 21.94,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 17429964,
              "user_usec": 5321767,
              "system_usec": 12108197,
              "nice_usec": 0,
              "nr_periods": 211,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 24.07,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 19195084,
              "user_usec": 5889122,
              "system_usec": 13305962,
              "nice_usec": 0,
              "nr_periods": 233,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 26.2,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 20950737,
              "user_usec": 6407328,
              "system_usec": 14543409,
              "nice_usec": 0,
              "nr_periods": 254,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 28.33,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 22710123,
              "user_usec": 6904398,
              "system_usec": 15805725,
              "nice_usec": 0,
              "nr_periods": 275,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 30.45,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 24454478,
              "user_usec": 7426169,
              "system_usec": 17028308,
              "nice_usec": 0,
              "nr_periods": 296,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 32.58,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 26200696,
              "user_usec": 7915303,
              "system_usec": 18285393,
              "nice_usec": 0,
              "nr_periods": 318,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 34.71,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 27981457,
              "user_usec": 8468813,
              "system_usec": 19512643,
              "nice_usec": 0,
              "nr_periods": 339,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 36.84,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 29727906,
              "user_usec": 8972320,
              "system_usec": 20755585,
              "nice_usec": 0,
              "nr_periods": 360,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 38.96,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 31486556,
              "user_usec": 9529579,
              "system_usec": 21956976,
              "nice_usec": 0,
              "nr_periods": 381,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 41.08,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_56dcecb92abfe77b",
            "cpu_stat": {
              "usage_usec": 33171557,
              "user_usec": 10024268,
              "system_usec": 23147288,
              "nice_usec": 0,
              "nr_periods": 403,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          }
        ],
        "interval_cores": [
          0.827,
          0.827,
          0.833,
          0.822,
          0.827,
          0.82,
          0.821,
          0.825,
          0.832,
          0.829,
          0.824,
          0.826,
          0.823,
          0.82,
          0.836,
          0.82,
          0.83,
          0.795
        ],
        "peak_cores": 0.836,
        "nr_throttled_delta": 0
      },
      "flood_under_own_spinners": {
        "label": "4 concurrent copies of the probe's openclose program, one sandbox",
        "harness": "probe_n82_traced_syscall_costs.INNER imported verbatim, run in a sandbox we own",
        "sandbox_id": "sbx_c2da98f31f321d54",
        "node_id": "worker-3",
        "cgroup": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
        "ops_per_s": 11887,
        "per_client_ops_per_s": [
          2971,
          2973,
          2972,
          2971
        ],
        "output": "STALL wall=1791275867.472 op_us=22047 mean_us=360.5 round=0\nDONE op=openclose stalls=1 rounds=30 elapsed_s=20.2 ops_per_s=2971\nSTALL wall=1791275867.472 op_us=22082 mean_us=360.4 round=0\nDONE op=openclose stalls=1 rounds=30 elapsed_s=20.2 ops_per_s=2973\nSTALL wall=1791275867.472 op_us=22037 mean_us=359.8 round=0\nDONE op=openclose stalls=1 rounds=30 elapsed_s=20.2 ops_per_s=2972\nSTALL wall=1791275867.472 op_us=22085 mean_us=359.8 round=0\nDONE op=openclose stalls=1 rounds=30 elapsed_s=20.2 ops_per_s=2971",
        "cgroup_samples": [
          {
            "at_s": 2.01,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 2102727,
              "user_usec": 813586,
              "system_usec": 1289141,
              "nice_usec": 0,
              "nr_periods": 26,
              "nr_throttled": 3,
              "throttled_usec": 22038,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 4.13,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 4191601,
              "user_usec": 1704208,
              "system_usec": 2487393,
              "nice_usec": 0,
              "nr_periods": 47,
              "nr_throttled": 5,
              "throttled_usec": 22188,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 6.26,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 6281167,
              "user_usec": 2523320,
              "system_usec": 3757847,
              "nice_usec": 0,
              "nr_periods": 68,
              "nr_throttled": 5,
              "throttled_usec": 22188,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 8.39,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 8374297,
              "user_usec": 3411288,
              "system_usec": 4963008,
              "nice_usec": 0,
              "nr_periods": 89,
              "nr_throttled": 5,
              "throttled_usec": 22188,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 10.52,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 10468534,
              "user_usec": 4322176,
              "system_usec": 6146357,
              "nice_usec": 0,
              "nr_periods": 111,
              "nr_throttled": 8,
              "throttled_usec": 23359,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 12.65,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 12571001,
              "user_usec": 5120518,
              "system_usec": 7450483,
              "nice_usec": 0,
              "nr_periods": 132,
              "nr_throttled": 9,
              "throttled_usec": 23563,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 14.79,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 14657097,
              "user_usec": 5909784,
              "system_usec": 8747312,
              "nice_usec": 0,
              "nr_periods": 153,
              "nr_throttled": 9,
              "throttled_usec": 23563,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 16.91,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 16759066,
              "user_usec": 6794749,
              "system_usec": 9964317,
              "nice_usec": 0,
              "nr_periods": 175,
              "nr_throttled": 10,
              "throttled_usec": 23563,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 19.04,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 18846888,
              "user_usec": 7611443,
              "system_usec": 11235445,
              "nice_usec": 0,
              "nr_periods": 196,
              "nr_throttled": 10,
              "throttled_usec": 23563,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 21.17,
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_c2da98f31f321d54",
            "cpu_stat": {
              "usage_usec": 19973094,
              "user_usec": 8080019,
              "system_usec": 11893074,
              "nice_usec": 0,
              "nr_periods": 211,
              "nr_throttled": 11,
              "throttled_usec": 26146,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          }
        ],
        "interval_cores": [
          0.985,
          0.981,
          0.983,
          0.983,
          0.987,
          0.975,
          0.991,
          0.98,
          0.529
        ],
        "peak_cores": 0.991,
        "nr_throttled_delta": 8
      }
    },
    "4_narrowing_view_shape": {
      "pass": true,
      "workers": {
        "worker-1": {
          "mount": "/pod-cgroup",
          "ls_mount": [
            ".lxc",
            "cgroup.controllers",
            "cgroup.events",
            "cgroup.freeze",
            "cgroup.kill",
            "cgroup.max.depth",
            "cgroup.max.descendants",
            "cgroup.procs",
            "cgroup.stat",
            "cgroup.stat.local",
            "cgroup.subtree_control",
            "cgroup.threads",
            "cgroup.type",
            "cpu.idle",
            "cpu.max",
            "cpu.max.burst",
            "cpu.stat",
            "cpu.stat.local",
            "cpu.weight",
            "cpu.weight.nice",
            "cpuset.cpus",
            "cpuset.cpus.effective",
            "cpuset.cpus.exclusive",
            "cpuset.cpus.exclusive.effective",
            "cpuset.cpus.partition",
            "cpuset.mems",
            "cpuset.mems.effective",
            "docker",
            "init.scope",
            "io.max",
            "io.stat",
            "memory.current",
            "memory.events",
            "memory.events.local",
            "memory.high",
            "memory.low",
            "memory.max",
            "memory.min",
            "memory.oom.group",
            "memory.peak",
            "memory.reclaim",
            "memory.stat",
            "memory.swap.current",
            "memory.swap.events",
            "memory.swap.high",
            "memory.swap.max",
            "memory.swap.peak",
            "pids.current",
            "pids.events",
            "pids.events.local",
            "pids.max",
            "pids.peak"
          ],
          "ls_mount_count": 52,
          "proc_self_cgroup": "0::/worker",
          "hostname": "fefc14f5a4ca",
          "own": {
            "path": "/pod-cgroup/docker/fefc14f5a4cae814f94e01f804912980a75ffc6c41081318059f5e509e85d877",
            "owner": {
              "uid": 65534,
              "gid": 65534,
              "mode": "0o755"
            },
            "cgroup_procs": "WRITABLE",
            "subtree_control": "WRITABLE",
            "cpu_max": "EACCES",
            "cpu_max_value": "max 100000",
            "subtree_control_value": "cpu",
            "worker_dir_owner": {
              "uid": 65534,
              "gid": 65534,
              "mode": "0o755"
            }
          },
          "foreign": [
            {
              "path": "/pod-cgroup",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "mkdir": "EACCES"
            }
          ],
          "own_subtree": {
            "own": "/pod-cgroup/docker/fefc14f5a4cae814f94e01f804912980a75ffc6c41081318059f5e509e85d877",
            "children": [
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "foreign_closed": true,
          "foreign_visible": true,
          "mount_looks_narrowed": false,
          "cpu_max_closed": true
        },
        "worker-2": {
          "mount": "/pod-cgroup",
          "ls_mount": [
            ".lxc",
            "cgroup.controllers",
            "cgroup.events",
            "cgroup.freeze",
            "cgroup.kill",
            "cgroup.max.depth",
            "cgroup.max.descendants",
            "cgroup.procs",
            "cgroup.stat",
            "cgroup.stat.local",
            "cgroup.subtree_control",
            "cgroup.threads",
            "cgroup.type",
            "cpu.idle",
            "cpu.max",
            "cpu.max.burst",
            "cpu.stat",
            "cpu.stat.local",
            "cpu.weight",
            "cpu.weight.nice",
            "cpuset.cpus",
            "cpuset.cpus.effective",
            "cpuset.cpus.exclusive",
            "cpuset.cpus.exclusive.effective",
            "cpuset.cpus.partition",
            "cpuset.mems",
            "cpuset.mems.effective",
            "docker",
            "init.scope",
            "io.max",
            "io.stat",
            "memory.current",
            "memory.events",
            "memory.events.local",
            "memory.high",
            "memory.low",
            "memory.max",
            "memory.min",
            "memory.oom.group",
            "memory.peak",
            "memory.reclaim",
            "memory.stat",
            "memory.swap.current",
            "memory.swap.events",
            "memory.swap.high",
            "memory.swap.max",
            "memory.swap.peak",
            "pids.current",
            "pids.events",
            "pids.events.local",
            "pids.max",
            "pids.peak"
          ],
          "ls_mount_count": 52,
          "proc_self_cgroup": "0::/worker",
          "hostname": "0ab62dc79c05",
          "own": {
            "path": "/pod-cgroup/docker/0ab62dc79c055514df4538ef3a51d0f57c1bb7065e893e3ca0b8d7956d0331a0",
            "owner": {
              "uid": 65534,
              "gid": 65534,
              "mode": "0o755"
            },
            "cgroup_procs": "WRITABLE",
            "subtree_control": "WRITABLE",
            "cpu_max": "EACCES",
            "cpu_max_value": "max 100000",
            "subtree_control_value": "cpu",
            "worker_dir_owner": {
              "uid": 65534,
              "gid": 65534,
              "mode": "0o755"
            }
          },
          "foreign": [
            {
              "path": "/pod-cgroup",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "mkdir": "EACCES"
            }
          ],
          "own_subtree": {
            "own": "/pod-cgroup/docker/0ab62dc79c055514df4538ef3a51d0f57c1bb7065e893e3ca0b8d7956d0331a0",
            "children": [
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "foreign_closed": true,
          "foreign_visible": true,
          "mount_looks_narrowed": false,
          "cpu_max_closed": true
        },
        "worker-3": {
          "mount": "/pod-cgroup",
          "ls_mount": [
            ".lxc",
            "cgroup.controllers",
            "cgroup.events",
            "cgroup.freeze",
            "cgroup.kill",
            "cgroup.max.depth",
            "cgroup.max.descendants",
            "cgroup.procs",
            "cgroup.stat",
            "cgroup.stat.local",
            "cgroup.subtree_control",
            "cgroup.threads",
            "cgroup.type",
            "cpu.idle",
            "cpu.max",
            "cpu.max.burst",
            "cpu.stat",
            "cpu.stat.local",
            "cpu.weight",
            "cpu.weight.nice",
            "cpuset.cpus",
            "cpuset.cpus.effective",
            "cpuset.cpus.exclusive",
            "cpuset.cpus.exclusive.effective",
            "cpuset.cpus.partition",
            "cpuset.mems",
            "cpuset.mems.effective",
            "docker",
            "init.scope",
            "io.max",
            "io.stat",
            "memory.current",
            "memory.events",
            "memory.events.local",
            "memory.high",
            "memory.low",
            "memory.max",
            "memory.min",
            "memory.oom.group",
            "memory.peak",
            "memory.reclaim",
            "memory.stat",
            "memory.swap.current",
            "memory.swap.events",
            "memory.swap.high",
            "memory.swap.max",
            "memory.swap.peak",
            "pids.current",
            "pids.events",
            "pids.events.local",
            "pids.max",
            "pids.peak"
          ],
          "ls_mount_count": 52,
          "proc_self_cgroup": "0::/worker",
          "hostname": "6d71515dfeec",
          "own": {
            "path": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721",
            "owner": {
              "uid": 65534,
              "gid": 65534,
              "mode": "0o755"
            },
            "cgroup_procs": "WRITABLE",
            "subtree_control": "WRITABLE",
            "cpu_max": "EACCES",
            "cpu_max_value": "max 100000",
            "subtree_control_value": "cpu",
            "worker_dir_owner": {
              "uid": 65534,
              "gid": 65534,
              "mode": "0o755"
            }
          },
          "foreign": [
            {
              "path": "/pod-cgroup",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "mkdir": "EACCES"
            }
          ],
          "own_subtree": {
            "own": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721",
            "children": [
              "sbx_sbx_c2da98f31f321d54",
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "foreign_closed": true,
          "foreign_visible": true,
          "mount_looks_narrowed": false,
          "cpu_max_closed": true
        }
      }
    },
    "5_own_cpu_max_eacces": {
      "pass": true,
      "workers": {
        "worker-1": "EACCES",
        "worker-2": "EACCES",
        "worker-3": "EACCES"
      }
    }
  },
  "sandboxes": [
    {
      "sandbox_id": "sbx_fc73ff9407750d0a",
      "node_id": "worker-3",
      "cgroup": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_fc73ff9407750d0a"
    },
    {
      "sandbox_id": "sbx_c123b23c280442d2",
      "node_id": "worker-2",
      "cgroup": "/pod-cgroup/docker/0ab62dc79c055514df4538ef3a51d0f57c1bb7065e893e3ca0b8d7956d0331a0/sbx_sbx_c123b23c280442d2"
    },
    {
      "sandbox_id": "sbx_4cd0a38095989050",
      "node_id": "worker-1",
      "cgroup": "/pod-cgroup/docker/fefc14f5a4cae814f94e01f804912980a75ffc6c41081318059f5e509e85d877/sbx_sbx_4cd0a38095989050"
    },
    {
      "sandbox_id": "sbx_2eeb1b95284940da",
      "node_id": "worker-3",
      "cgroup": "/pod-cgroup/docker/6d71515dfeecb9ae17b81802edd1fdfbfff8d0bcf6fed10d07951d46465b0721/sbx_sbx_2eeb1b95284940da"
    }
  ],
  "started_at": "2026-10-06T16:36:28+0800",
  "elapsed_s": 101.3,
  "ok": true
}
```

## 5. RED 原始读数（`E2B_SANDBOX_CGROUP=off`）

```json
{
  "lane": {
    "api_url": "http://127.0.0.1:3200",
    "internal_url": "http://control-plane:3000",
    "worker_exec_template": "docker exec -i n83acc-{node}-1 bash -lc",
    "nodes": [
      "worker-1",
      "worker-2",
      "worker-3"
    ],
    "cgroup_mount": "/pod-cgroup",
    "template": "base",
    "flood_seconds": 40.0,
    "sandbox_cgroup_env": "required (the caller's override; see the report)",
    "sandbox_notify_rate_limit_env": "0 (the caller's override; only this acceptance)"
  },
  "n82_baseline": {
    "ops_per_s": 18149,
    "cores_on_worker_pod": 1.02
  },
  "checks": {
    "1_quota_is_real": {
      "pass": false,
      "declared_cpu_percent": 100.0,
      "measured_cpu_percent": 400.46644447743057,
      "cpu_max_readback": null,
      "sandbox_cgroup": null,
      "spinner_node": "worker-3",
      "first_sandbox_rtt_quiet": {
        "samples_ms": [
          322.98,
          44.56,
          31.53,
          34.37,
          42.92
        ],
        "min_ms": 31.53,
        "median_ms": 42.92
      },
      "second_sandbox_rtt": {
        "samples_ms": [
          249.74,
          35.69,
          47.7,
          49.86,
          27.39
        ],
        "min_ms": 27.39,
        "median_ms": 47.7
      },
      "round_trip_criterion": "min-of-5, within 2x of the quiet baseline (>=200ms floor)",
      "second_sandbox_node": "worker-3",
      "second_sandbox_same_node": true
    },
    "2_kernel_enforces": {
      "pass": false,
      "reason": "there is no sbx_<id> cgroup under the hosting worker's own delegated cgroup, so there is nothing to throttle (E2B_SANDBOX_CGROUP off?)",
      "cgroup": null,
      "quota_cores": 1.0
    },
    "3_flood_spends_own_quota": {
      "pass": false,
      "probe": "probe_n82_traced_syscall_costs.py",
      "n82_baseline": {
        "ops_per_s": 18149,
        "booked_on": "the worker pod"
      },
      "quota_cores": 1.0,
      "flood_alone": {
        "label": "the N82 probe (openclose) alone in its own sandbox",
        "probe_output": "DONE op=openclose stalls=0 rounds=179 elapsed_s=40.1 ops_per_s=8937",
        "ops_per_s": 8937,
        "elapsed_s": 42.8,
        "sandbox_id": "sbx_2f716edfc5f5db3d",
        "node_id": "worker-3",
        "cgroup": null,
        "cgroup_samples": [],
        "interval_cores": [],
        "peak_cores": null,
        "nr_throttled_delta": null
      },
      "flood_under_own_spinners": {
        "refusal": "the binding harness found no sbx_<id> cgroup under the hosting worker's own delegated cgroup (E2B_SANDBOX_CGROUP off?)"
      }
    },
    "4_narrowing_view_shape": {
      "pass": false,
      "workers": {
        "worker-1": {
          "error": "worker command on worker-1 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname be668b6091cb: []"
        },
        "worker-2": {
          "error": "worker command on worker-2 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname a5660a4fe746: []"
        },
        "worker-3": {
          "error": "worker command on worker-3 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname 07712507c33e: []"
        }
      }
    },
    "5_own_cpu_max_eacces": {
      "pass": false,
      "workers": {
        "worker-1": null,
        "worker-2": null,
        "worker-3": null
      }
    }
  },
  "sandboxes": [
    {
      "sandbox_id": "sbx_ce11b9bab69dead2",
      "node_id": "worker-3",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_90b789bf14f23cd6",
      "node_id": "worker-2",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_423b430431d3a8b4",
      "node_id": "worker-1",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_89c4146a1834f916",
      "node_id": "worker-3",
      "cgroup": null
    }
  ],
  "started_at": "2026-10-06T16:38:44+0800",
  "elapsed_s": 74.5,
  "ok": false
}
```

## 6. RED 的补充读数：这笔 CPU 记在**谁**头上

同一条 `openclose` 洪泛（限流关、**没有**每沙箱 cgroup）期间，每 2 s 采三台 worker **自己容器
cgroup** 的 `cpu.stat`（一次性探针 `tmp/n83-red-neighbour-cpu.py`，未入库）：

```
worker-1: peak 0.041 cores, median 0.029 cores
worker-2: peak 0.038 cores, median 0.026 cores
worker-3: peak 0.872 cores, median 0.857 cores
```

即：洪泛那 ~0.86 核**记在 hosting worker 的容器 cgroup 上**，租户账上是 0 —— 与 N82
（`docs/open-issues.md` N82：18149 op/s、1.02 核记在 worker pod）同形。GREEN 档里同一笔 CPU
落在 `sbx_<id>` 子树里。

原始 JSON（含每个采样点的 `usage_usec`）：``worker-2: peak 0.038 cores, median 0.026 cores
worker-3: peak 0.872 cores, median 0.857 cores`（完整 JSON 见工作树 `tmp/n83-red-neighbour-cpu.json`）`

## 7. 五条检查逐条判定

| # | 判据（脚本里的实际条件） | GREEN 读数 | 判定 |
|---|---|---|---|
| ① 额度是真的 | `measuredCpuPercent ∈ [50,150]`（声明 100）且同节点第二箱往返 min-of-5 ≤ 2× 静默基线 | `99.98`；`cpu_max=100000 100000`；第二箱 32.0 ms vs 静默 31.5 ms | PASS |
| ② 内核在强制 | 3 s 自旋窗口内 `nr_throttled_delta > 0` 且 `observed_cores ∈ [0.5,1.5]×quota` | `usage_usec +3,107,035`、`nr_throttled +32`、`throttled_usec +9,612,556`、`0.995 核` | PASS |
| ③ 洪泛花自己的额度 | 探针单跑 `peak ≤ 1.15×quota` **且** 四路并发 `peak ≤ 1.15×quota` **且** `nr_throttled > 0` **且**两次都真拿到 `ops_per_s` | 单独 `9737 op/s / 0.836 核 / nr_throttled 0`；四路 `11,887 op/s（2971×4）/ 0.991 核 / nr_throttled +8` | PASS |
| ④ 收窄/视图形状 | 三台 worker：自家容器 cgroup 属主 65534 且 `cgroup.procs`/`subtree_control` 可写；**别的容器** cgroup 的 `cpu.max`/`cgroup.procs` 不可写 | 三台全满足；`/proc/self/cgroup = 0::/worker`；视野内的**别的容器** cgroup `cpu.max`/`cgroup.procs`/`mkdir` 全 `EACCES`（GREEN JSON 里 `foreign_visible: true`） | PASS |
| ⑤ 负例 fail-closed | 三台 worker 自家 `cpu.max` 的 `open(O_WRONLY)` == `EACCES` | worker-1/2/3 全 `EACCES` | PASS |

RED 档同一条脚本的判定（**这就是"不弱化"的证据**：RED 五条全 FAIL，且每条都给了具名理由）：

| # | RED 读数 | 判定 |
|---|---|---|
| ① | `measuredCpuPercent = 400.47`、`sandbox_cgroup = null` | FAIL |
| ② | `reason: there is no sbx_<id> cgroup … (E2B_SANDBOX_CGROUP off?)` | FAIL |
| ③ | 探针 `8935 op/s`（RED 档位那一次），`peak_cores = null`；binding harness 具名拒绝（找不到 `sbx_<id>`） | FAIL |
| ④ | 没有"自家被委派目录"（视图里没有 `worker/` 子树） | FAIL |
| ⑤ | 没有被委派的容器目录 ⇒ 三台都判不出"自家 cgroup" | FAIL |

## 8. 没做 / 没验证的（如实说）

* **线上（k0s）没滚**：Task 7 的 Step 3（两次 apply）与线上复验（k8s 版 ④ + 端到端冒烟）
  **没做**，因为这个任务的范围到"本地 lane 全绿 + 文档 + 回退杆"。上线剧本写在
  `docs/deploy-clusters.md` §7.48，回退杆也在那里。
* **内存/进程数**（`memory.max` / `pids.max`）是 Phase 2，本任务不涉及（计划 D6）。
* **多节点跨 worker 的邻居保护**（同一节点上另一个沙箱被别箱拖慢）只量了"第二箱往返不掉速"
  这一条；节点级公平（CFS 层次带宽）没有单独测量。
* **`E2B_SANDBOX_NOTIFY_RATE_LIMIT=0` 只在这次验收的栈里**：生产清单没改，N83 Phase 1 的
  Task 8（限流器降级）还没有做。
* 本机 Docker VM 是 **cgroup v2 / x86_64**（`docker info`：`CgroupVersion 2`、`Architecture x86_64`、
  6 核 / 20 GB）。k0s 节点是 arm64 的 4 核，**镜像形状与 cgroup 行为在那边仍需按 Step 3 复验**。
* 一次"额度先被自家进程占满、再往这个沙箱排新命令"的读数：命令队列 **30 s 超时**
  （RPC `RateLimitException: command queue timed out after 30s`）。这被我在脚本里如实处理
  （binding harness 改成"一条命令里起四个客户端"），并在 §7.48 的坑 3 里记下。

## 9. 收尾

### 9.1 单元 / 钉子测试（本机 macOS）

`python -m pytest tests/unit -q` ⇒ **2562 passed, 13 skipped, 15 failed**。这 15 条与我的改动无关：
把同一个 venv 指向 `95907a1` 的干净 worktree（`git worktree add --detach tmp/head-check 95907a1`）
重跑同一组用例，**失败集合逐条相同**（`test_apply_requires_cluster_guard` 5、
`test_c2_p0_probe` 2、`test_docs_only_point_at_repo_artifacts` 2、`test_migrate_state_base_script` 3、
`test_real_root_gate` 1、`test_xfs_quotactl_backend` 2 —— 就是 HANDOFF 记的那批 macOS 环境红）。
与 Task 7 直接相关的两条也单独跑过并绿：`tests/unit/test_worker_manifest_permissions.py`（68 passed 那批）、
`tests/unit/test_worker_env_key_sets.py` + `test_compose_base_image_shape.py` + `test_cluster_guard.py` +
`test_secret_scripts_require_cluster_guard.py`（27 passed）。

* 拆栈后核对：`docker compose -p n83acc ps` 空、`GET /sandboxes` = `[]`、
  `ls -d /pod-cgroup/docker/*/sbx_*`（三台 worker）= 无输出。
* 用户的 live 栈（project `compose`、3100）自始至终是 `Up`，镜像/容器未被触碰。

## 10. 与计划的偏差（都要有人知道）

1. **执行者与立项文本不同**：计划 §3.2 定的是"worker 自管、agent 只做一次性委派"（形态 W），
   N83 行里那段"由 agent 建子 cgroup"是**更早的 D2/D3 版本**；我按 §3.2 实现与验收，并把
   N83 行的那段标成"原始立项文本"。
2. **check ③ 加了第二条读数**：计划只要求"洪泛仍落在额度内"（= 单独跑 ≤ 额度），照做是
   `0.84 核 ≤ 1 核`。但那条**没有**证明额度 binding（负载自限在 1 核以下）。所以我在**同一条检查里**
   追加"四路并发"读数（用**探针自己的 INNER 程序**，import 而非抄写）—— 0.99 核 + `nr_throttled +11`
   才真正说明"沙箱自己的额度在 bound 它"。判据里两条都要过。
3. **check ④ 的本地版判据**：计划的写法是"本地 lane = 只看得到自己容器那棵子树"，但 compose 没有
   `subPathExpr`、挂载的是**整棵 VM 树**（§1.4 的探针也这么量过）。所以本地版 ④ 判的是
   "**可写/被委派**的范围只有自家容器 cgroup"（自家可写、兄弟容器全 EACCES），这与 §3.5
   的收窄意图一致；k8s 那版（挂载本身收窄、`ls /pod-cgroup` 看不到 `kubepods/`）留给 Step 3 复验。
